"""熔断策略：按失败范围计数 + 冷却递进（2026-10-08 调整）。

起因是当天 shadow 对比整轮作废：阿里云 WAF 只拦 `/uid/statusid` 这类文章 URL
（whoami / watchlist / 首页全正常），而熔断器把「详情被拦」当成「站点挂了」，
3 次就跳闸 60 分钟、剩下 8 个账号全被跳过。这里把新口径钉住。
"""

from __future__ import annotations

import pytest

from crawl_gateway.config import (
    CircuitBreakerConfig,
    ConfigError,
    _parse_circuit_breaker,
)
from crawl_gateway.health import CircuitBreaker, CircuitState
from crawl_gateway.models import AttemptResult, AttemptScope, AttemptStatus
from crawl_gateway.storage import GatewayStore

HARD = frozenset(
    {
        AttemptStatus.BLOCKED_WAF,
        AttemptStatus.CAPTCHA_REQUIRED,
        AttemptStatus.AUTH_EXPIRED,
    }
)


def make_breaker(**overrides) -> CircuitBreaker:
    return CircuitBreaker(
        CircuitBreakerConfig(
            failure_threshold=overrides.pop("failure_threshold", 3),
            window_minutes=overrides.pop("window_minutes", 10),
            cooldown_minutes=overrides.pop("cooldown_minutes", 15),
            hard_failure_statuses=HARD,
            **overrides,
        )
    )


def _trip(breaker: CircuitBreaker, now: float) -> None:
    for offset in range(3):
        breaker.record(AttemptStatus.BLOCKED_WAF, now + offset)


# ── 按范围计数 ────────────────────────────────────────────────────────────


def test_detail_scope_waf_never_trips_the_breaker():
    """详情级 WAF 只说明某篇文章被拦，列表页是通的 —— 不该停整个站点。"""
    breaker = make_breaker()

    for offset in range(10):
        breaker.record(
            AttemptStatus.BLOCKED_WAF, 1_000.0 + offset, scope=AttemptScope.DETAIL
        )

    assert breaker.state is CircuitState.CLOSED
    assert breaker.hard_failure_times == ()
    assert breaker.allow(1_020.0).allowed is True


def test_account_scope_waf_trips_at_threshold():
    breaker = make_breaker()

    _trip(breaker, 1_000.0)

    assert breaker.state is CircuitState.OPEN
    assert len(breaker.hard_failure_times) == 3
    decision = breaker.allow(1_010.0)
    assert decision.allowed is False
    assert decision.retry_after_seconds > 0


def test_scope_defaults_to_account_so_untouched_callers_behave_as_before():
    """没标注 scope 的调用方语义不变（引入本字段前是 3 次必跳闸）。"""
    assert AttemptResult(AttemptStatus.SUCCESS, "opencli").scope is AttemptScope.ACCOUNT

    breaker = make_breaker()
    for offset in range(3):
        breaker.record(AttemptStatus.BLOCKED_WAF, 1_000.0 + offset)
    assert breaker.state is CircuitState.OPEN


# ── 冷却递进 ──────────────────────────────────────────────────────────────


def test_cooldown_starts_short_then_escalates_and_caps():
    breaker = make_breaker(
        cooldown_minutes=15, cooldown_multiplier=2, max_cooldown_minutes=60
    )
    now = 10_000.0

    _trip(breaker, now)
    assert breaker.cooldown_seconds == 15 * 60, "首跳 15 分钟"

    # 冷却结束 → 半开探测 → 仍失败 = 复犯，冷却翻倍
    probe_at = now + 2 + breaker.cooldown_seconds + 1
    assert breaker.allow(probe_at).probe is True
    breaker.record(AttemptStatus.BLOCKED_WAF, probe_at)
    assert breaker.cooldown_seconds == 30 * 60

    probe_at = probe_at + breaker.cooldown_seconds + 1
    assert breaker.allow(probe_at).probe is True
    breaker.record(AttemptStatus.BLOCKED_WAF, probe_at)
    assert breaker.cooldown_seconds == 60 * 60

    # 继续复犯不再超过封顶
    probe_at = probe_at + breaker.cooldown_seconds + 1
    assert breaker.allow(probe_at).probe is True
    breaker.record(AttemptStatus.BLOCKED_WAF, probe_at)
    assert breaker.cooldown_seconds == 60 * 60


def test_success_resets_the_escalation_ladder():
    breaker = make_breaker(
        cooldown_minutes=15, cooldown_multiplier=2, max_cooldown_minutes=60
    )
    now = 10_000.0
    _trip(breaker, now)
    assert breaker.cooldown_seconds == 15 * 60

    breaker.record(AttemptStatus.SUCCESS, now + 1)
    assert breaker.open_count == 0

    _trip(breaker, now + 2)
    assert breaker.cooldown_seconds == 15 * 60, "真恢复过 → 冷却回到首跳值"


def test_half_open_detail_failure_closes_and_consumes_the_trip_count():
    """半开探测时列表通了、只是详情被拦 → 站点可达，闭合.

    闭合时要把跳闸残留的满阈值计数清掉：不清的话状态虽已闭合、计数仍是 3，
    下**一次**账号级失败就会立即再跳闸（阈值退化成 1），并把冷却阶梯无端推高一档。
    复犯阶梯（open_count）**不**重置 —— 那不是「恢复」。
    """
    breaker = make_breaker()
    now = 5_000.0
    _trip(breaker, now)
    assert len(breaker.hard_failure_times) == 3
    opens_before = breaker.open_count

    probe_at = now + 2 + breaker.cooldown_seconds + 1
    assert breaker.allow(probe_at).probe is True
    breaker.record(AttemptStatus.BLOCKED_WAF, probe_at, scope=AttemptScope.DETAIL)

    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow(probe_at + 1).allowed is True
    assert breaker.hard_failure_times == (), "跳闸计数在闭合时消费掉"
    assert breaker.open_count == opens_before, "复犯阶梯不重置"


def test_after_half_open_close_one_failure_is_not_enough_to_trip_again():
    """闭合后再撞一次账号级失败，不该立刻又跳闸（阈值仍是 3，不是 1）."""
    breaker = make_breaker()
    now = 5_000.0
    _trip(breaker, now)

    probe_at = now + 2 + breaker.cooldown_seconds + 1
    assert breaker.allow(probe_at).probe is True
    breaker.record(AttemptStatus.BLOCKED_WAF, probe_at, scope=AttemptScope.DETAIL)
    assert breaker.state is CircuitState.CLOSED

    breaker.record(AttemptStatus.BLOCKED_WAF, probe_at + 1)

    assert breaker.state is CircuitState.CLOSED
    assert len(breaker.hard_failure_times) == 1


def test_half_open_probe_can_be_abandoned_so_the_job_does_not_wedge():
    """限速器挡下半开探测时必须能放掉探测标记，否则整个 job 卡死.

    回归（2026-10-09 通盘审查）：`_execute_with_policy` 在限速器拒绝时直接返回
    SKIPPED、**不调 `record()`**，而 `_probe_pending` 只有 `record()` 会清 ——
    于是之后每个任务拿到的都是 HALF_OPEN 的 not-allowed，一次 job 全被跳过。
    """
    breaker = make_breaker()
    now = 7_000.0
    _trip(breaker, now)

    probe_at = now + 2 + breaker.cooldown_seconds + 1
    assert breaker.allow(probe_at).probe is True  # 进入 HALF_OPEN，探测待发
    assert breaker.allow(probe_at + 1).allowed is False  # 探测未完成前不放行

    breaker.abandon_probe()  # 限速器把这次尝试挡下了

    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.allow(probe_at + 2).allowed is True, "下一个任务还得能探"


def test_half_open_account_failure_reopens():
    breaker = make_breaker()
    now = 5_000.0
    _trip(breaker, now)

    probe_at = now + 2 + breaker.cooldown_seconds + 1
    assert breaker.allow(probe_at).probe is True
    breaker.record(AttemptStatus.BLOCKED_WAF, probe_at)

    assert breaker.state is CircuitState.OPEN
    assert breaker.allow(probe_at + 1).allowed is False


# ── 配置 ──────────────────────────────────────────────────────────────────


def _raw_circuit(**overrides) -> dict:
    raw = {
        "failure_threshold": 3,
        "window_minutes": 10,
        "cooldown_minutes": 15,
        "hard_failure_statuses": ["blocked_waf"],
    }
    raw.update(overrides)
    return raw


def test_ladder_keys_are_optional_with_defaults():
    config = _parse_circuit_breaker(_raw_circuit(), "xueqiu")

    assert config.cooldown_multiplier == 2
    assert config.max_cooldown_minutes == 60


def test_rejects_max_cooldown_below_first_cooldown():
    """封顶小于首跳 = 首跳就被静默截短（120 配默认 60 → 实际 60），必须报错。"""
    with pytest.raises(ConfigError, match="max_cooldown_minutes"):
        _parse_circuit_breaker(
            _raw_circuit(cooldown_minutes=120, max_cooldown_minutes=60), "xueqiu"
        )


def _repo_site(code: str):
    from crawl_gateway.config import load_sites_config

    return load_sites_config("config/sites.yaml").sites[code]


def test_xiaohongshu_ladder_is_not_silently_shortened():
    """仓库里真实配置的第二站：cooldown 120 必须配封顶 ≥120，不能被默认 60 截短。"""
    xhs = _repo_site("xiaohongshu").circuit_breaker

    assert xhs.cooldown_minutes == 120
    assert xhs.max_cooldown_minutes >= xhs.cooldown_minutes


def test_xueqiu_ladder_matches_the_documented_policy():
    xueqiu = _repo_site("xueqiu").circuit_breaker

    assert (xueqiu.cooldown_minutes, xueqiu.cooldown_multiplier) == (15, 2)
    assert xueqiu.max_cooldown_minutes == 60


# ── 持久化 ────────────────────────────────────────────────────────────────


def test_open_count_round_trips_through_the_store(tmp_path):
    store = GatewayStore(tmp_path / "gateway.sqlite3")

    store.save_health("xueqiu", 40, "open", 123.0, (1.0, 2.0), 456.0, open_count=2)
    snapshot = store.load_health("xueqiu")

    assert snapshot is not None
    assert snapshot.open_count == 2
    assert snapshot.state == "open"


def test_legacy_database_gains_the_open_count_column(tmp_path):
    """老库没有 open_count 列 —— 打开时应自动补上，默认 0。"""
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE site_health (
            site TEXT PRIMARY KEY,
            score INTEGER NOT NULL,
            state TEXT NOT NULL,
            opened_at REAL NOT NULL,
            hard_failure_times TEXT NOT NULL DEFAULT '[]',
            updated_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO site_health VALUES ('xueqiu', 80, 'closed', 0.0, '[]', 0.0)"
    )
    connection.commit()
    connection.close()

    snapshot = GatewayStore(path).load_health("xueqiu")

    assert snapshot is not None
    assert snapshot.open_count == 0
