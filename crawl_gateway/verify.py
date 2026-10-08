from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from crawl_gateway.config import SiteConfig
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

    snapshot = store.load_health(site_config.code)
    if snapshot is None:
        checks.append(_check("circuit_state", True, "no health record; closed"))
    elif snapshot.state == "open":
        checks.append(
            _check(
                "circuit_state",
                False,
                f"circuit open since {snapshot.opened_at} "
                f"(第 {snapshot.open_count} 次跳闸，冷却按递进加长)",
            )
        )
    else:
        checks.append(
            _check(
                "circuit_state",
                True,
                f"state={snapshot.state} score={snapshot.score} "
                f"opens={snapshot.open_count}",
            )
        )

    doctor_result = (doctor or opencli_doctor)()
    checks.append(_check("opencli_available", doctor_result[0], doctor_result[1]))

    return {
        "site": site_config.code,
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
    }
