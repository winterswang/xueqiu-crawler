from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from crawl_gateway.config import SiteConfig
from crawl_gateway.health import cooldown_seconds_for
from crawl_gateway.storage import GatewayStore


def _check(name: str, ok: bool, detail: str = "") -> dict[str, object]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def opencli_doctor() -> tuple[bool, str]:
    if not shutil.which("opencli"):
        return False, "opencli not found in PATH"
    try:
        completed = subprocess.run(
            ["opencli", "doctor"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"opencli doctor failed: {exc}"
    output = (completed.stdout or completed.stderr or "").strip()
    if "[OK] Extension: connected" in completed.stdout:
        return True, "extension connected"
    return False, output[:200] or "extension not connected"


def run_verify(
    *,
    site_config: SiteConfig,
    store: GatewayStore,
    data_dir: str | Path,
    doctor: Callable[[], tuple[bool, str]] | None = None,
    now: float | None = None,
) -> dict[str, object]:
    """不发真实站点请求的运行前预检。"""
    checks: list[dict[str, object]] = []

    checks.append(
        _check("site_enabled", site_config.enabled, f"site {site_config.code}")
    )

    try:
        store.ping()
        checks.append(_check("database_writable", True, "write lock acquired"))
    except Exception as exc:
        checks.append(_check("database_writable", False, str(exc)))

    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        checks.append(_check("data_dir_writable", True, str(data_dir)))
    except OSError as exc:
        checks.append(_check("data_dir_writable", False, str(exc)))

    # 冷却**已过期**的 open 不算不健康：下一次运行就会做半开探测，把它当失败
    # 会让 run_daily.sh（set -e）在爬取前直接中止，而唯一能写回健康状态的就是
    # 那次爬取 —— 形成「不跑就清不掉、不清掉就跑不了」的死锁。
    current = time.time() if now is None else now
    snapshot = store.load_health(site_config.code)
    if snapshot is None:
        checks.append(_check("circuit_state", True, "no health record; closed"))
    elif snapshot.state == "open" and current < (
        snapshot.opened_at
        + cooldown_seconds_for(site_config.circuit_breaker, snapshot.open_count)
    ):
        remaining = (
            snapshot.opened_at
            + cooldown_seconds_for(site_config.circuit_breaker, snapshot.open_count)
            - current
        )
        checks.append(
            _check(
                "circuit_state",
                False,
                f"circuit open, 约 {remaining / 60:.0f} 分钟后可半开探测"
                f"（第 {snapshot.open_count} 次跳闸）",
            )
        )
    else:
        detail = (
            f"state={snapshot.state} score={snapshot.score} opens={snapshot.open_count}"
        )
        if snapshot.state == "open":
            detail += "（冷却已过期，下次运行先做半开探测）"
        checks.append(_check("circuit_state", True, detail))

    doctor_result = (doctor or opencli_doctor)()
    checks.append(_check("opencli_available", doctor_result[0], doctor_result[1]))

    return {
        "site": site_config.code,
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
    }
