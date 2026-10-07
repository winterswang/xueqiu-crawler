from __future__ import annotations

import yaml
import pytest

from crawl_gateway.compatibility import summarize_attempts
from crawl_gateway.config import (
    CircuitBreakerConfig,
    ConfigError,
    RateLimitConfig,
    load_sites_config,
)
from crawl_gateway.health import CircuitBreaker, CircuitState
from crawl_gateway.models import (
    FAILURE_STATUSES,
    HARD_FAILURE_STATUSES,
    SUCCESS_STATUSES,
    AttemptResult,
    AttemptStatus,
)
from crawl_gateway.rate_limiter import SiteRateLimiter
from crawl_gateway.retry import RetryPolicy


def _write_config(tmp_path, sites):
    path = tmp_path / "sites.yaml"
    path.write_text(yaml.safe_dump({"sites": sites}), encoding="utf-8")
    return path


def _site_config(**overrides):
    config = {
        "enabled": True,
        "backend_priority": ["opencli", "nodriver"],
        "rate_limit": {
            "concurrency": 1,
            "min_interval_seconds": 4,
            "max_interval_seconds": 9,
            "hourly_requests": 80,
            "daily_requests": 300,
        },
        "retry": {
            "max_attempts": 2,
            "backoff_base_seconds": 60,
            "backoff_multiplier": 2,
            "retry_statuses": ["network_error", "http_error", "parse_error"],
        },
        "circuit_breaker": {
            "failure_threshold": 3,
            "window_minutes": 10,
            "cooldown_minutes": 60,
            "hard_failure_statuses": [
                "blocked_waf",
                "captcha_required",
                "auth_expired",
            ],
        },
    }
    config.update(overrides)
    return config


def _rate_config(**overrides):
    raw = _site_config()["rate_limit"] | overrides
    return RateLimitConfig(
        concurrency=raw["concurrency"],
        min_interval_seconds=raw["min_interval_seconds"],
        max_interval_seconds=raw["max_interval_seconds"],
        hourly_requests=raw["hourly_requests"],
        daily_requests=raw["daily_requests"],
    )


def _circuit_config(**overrides):
    raw = _site_config()["circuit_breaker"] | overrides
    return CircuitBreakerConfig(
        failure_threshold=raw["failure_threshold"],
        window_minutes=raw["window_minutes"],
        cooldown_minutes=raw["cooldown_minutes"],
        hard_failure_statuses=frozenset(
            AttemptStatus(status) for status in raw["hard_failure_statuses"]
        ),
    )


def test_attempt_status_categories_do_not_overlap():
    all_statuses = set(AttemptStatus)

    assert SUCCESS_STATUSES | FAILURE_STATUSES < all_statuses
    assert not SUCCESS_STATUSES & FAILURE_STATUSES
    assert HARD_FAILURE_STATUSES < FAILURE_STATUSES
    assert AttemptStatus.CIRCUIT_OPEN not in SUCCESS_STATUSES | FAILURE_STATUSES
    assert AttemptStatus.SKIPPED not in SUCCESS_STATUSES | FAILURE_STATUSES


def test_load_sites_config_parses_and_normalizes_policy(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "xueqiu": _site_config(),
            "xiaohongshu": _site_config(
                backend_priority=["opencli"],
                enabled=False,
            ),
        },
    )

    config = load_sites_config(path)

    assert set(config.sites) == {"xueqiu", "xiaohongshu"}
    xueqiu = config.sites["xueqiu"]
    assert xueqiu.enabled is True
    assert xueqiu.backend_priority == ("opencli", "nodriver")
    assert xueqiu.retry.retry_statuses == {
        AttemptStatus.NETWORK_ERROR,
        AttemptStatus.HTTP_ERROR,
        AttemptStatus.PARSE_ERROR,
    }
    assert config.sites["xiaohongshu"].enabled is False


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        ({"backend_priority": []}, "backend_priority must be a non-empty list"),
        (
            {"backend_priority": ["opencli", "opencli"]},
            "backend_priority entries must be unique",
        ),
        (
            {
                "retry": {
                    "max_attempts": 1,
                    "backoff_base_seconds": 1,
                    "backoff_multiplier": 2,
                    "retry_statuses": ["not_a_status"],
                }
            },
            "contains an unknown status",
        ),
        (
            {
                "rate_limit": {
                    "concurrency": 0,
                    "min_interval_seconds": 1,
                    "max_interval_seconds": 2,
                    "hourly_requests": 1,
                    "daily_requests": 1,
                }
            },
            "rate_limit.concurrency must be a positive integer",
        ),
        (
            {
                "rate_limit": {
                    "concurrency": 1,
                    "min_interval_seconds": 5,
                    "max_interval_seconds": 4,
                    "hourly_requests": 1,
                    "daily_requests": 1,
                }
            },
            "max_interval_seconds must be >= min_interval_seconds",
        ),
    ],
)
def test_load_sites_config_rejects_unsafe_values(tmp_path, overrides, expected_error):
    path = _write_config(tmp_path, {"xueqiu": _site_config(**overrides)})

    with pytest.raises(ConfigError, match=expected_error):
        load_sites_config(path)


def test_rate_limiter_enforces_spacing_and_hourly_window():
    config = _rate_config(
        min_interval_seconds=4,
        max_interval_seconds=4,
        hourly_requests=3,
        daily_requests=10,
    )
    limiter = SiteRateLimiter(
        config,
        interval_selector=lambda minimum, maximum: minimum,
    )

    assert limiter.reserve(100).wait_seconds == 0
    assert limiter.reserve(104).wait_seconds == 0
    assert limiter.reserve(105).wait_seconds == 3

    denied = limiter.reserve(108)

    assert denied.allowed is False
    assert denied.reason == "hourly_requests_exhausted"
    assert denied.wait_seconds == 3592


def test_rate_limiter_enforces_daily_quota():
    config = _rate_config(
        hourly_requests=10,
        daily_requests=2,
    )
    limiter = SiteRateLimiter(
        config,
        interval_selector=lambda minimum, maximum: minimum,
    )

    assert limiter.reserve(0).allowed is True
    assert limiter.reserve(1).allowed is True

    denied = limiter.reserve(2)

    assert denied.allowed is False
    assert denied.reason == "daily_requests_exhausted"


def test_retry_policy_uses_status_and_backoff(tmp_path):
    config = (
        load_sites_config(_write_config(tmp_path, {"xueqiu": _site_config()}))
        .sites["xueqiu"]
        .retry
    )
    policy = RetryPolicy(config)

    first = policy.decide(AttemptStatus.NETWORK_ERROR, 1)
    blocked = policy.decide(AttemptStatus.BLOCKED_WAF, 1)
    exhausted = policy.decide(AttemptStatus.NETWORK_ERROR, 2)

    assert (first.retry, first.delay_seconds) == (True, 60)
    assert blocked.retry is False
    assert blocked.reason == "status_not_retryable"
    assert exhausted.reason == "max_attempts_reached"


def test_circuit_breaker_opens_and_allows_one_probe():
    config = _circuit_config(
        failure_threshold=2,
        cooldown_minutes=1,
        hard_failure_statuses=["blocked_waf"],
    )
    breaker = CircuitBreaker(config)

    breaker.record(AttemptStatus.BLOCKED_WAF, 0)
    assert breaker.state == CircuitState.CLOSED
    breaker.record(AttemptStatus.BLOCKED_WAF, 1)
    assert breaker.state == CircuitState.OPEN

    blocked = breaker.allow(30)
    assert blocked.allowed is False
    assert blocked.retry_after_seconds == 31

    probe = breaker.allow(61)
    assert probe.allowed is True
    assert probe.state == CircuitState.HALF_OPEN
    assert probe.probe is True

    second_probe = breaker.allow(61.1)
    assert second_probe.allowed is False

    breaker.record(AttemptStatus.SUCCESS, 62)
    assert breaker.state == CircuitState.CLOSED
    assert breaker.score == 2


def test_half_open_hard_failure_reopens_immediately():
    config = _circuit_config(
        failure_threshold=5,
        cooldown_minutes=1,
        hard_failure_statuses=["captcha_required"],
    )
    breaker = CircuitBreaker(config)

    breaker.record(AttemptStatus.CAPTCHA_REQUIRED, 0)
    assert breaker.state == CircuitState.CLOSED

    breaker.state = CircuitState.OPEN
    breaker.opened_at = 0
    assert breaker.allow(60).probe is True
    breaker.record(AttemptStatus.CAPTCHA_REQUIRED, 61)

    assert breaker.state == CircuitState.OPEN
    assert breaker.allow(61).allowed is False


def test_circuit_breaker_uses_rolling_failure_window():
    breaker = CircuitBreaker(
        _circuit_config(
            failure_threshold=2,
            window_minutes=1,
            cooldown_minutes=1,
            hard_failure_statuses=["blocked_waf"],
        )
    )

    breaker.record(AttemptStatus.BLOCKED_WAF, 0)
    breaker.record(AttemptStatus.BLOCKED_WAF, 61)

    assert breaker.state == CircuitState.CLOSED

    breaker.record(AttemptStatus.BLOCKED_WAF, 62)

    assert breaker.state == CircuitState.OPEN


def test_half_open_soft_failure_also_reopens():
    breaker = CircuitBreaker(
        _circuit_config(
            failure_threshold=1,
            cooldown_minutes=1,
            hard_failure_statuses=["blocked_waf"],
        )
    )

    breaker.record(AttemptStatus.BLOCKED_WAF, 0)
    assert breaker.allow(60).probe is True
    breaker.record(AttemptStatus.NETWORK_ERROR, 61)

    assert breaker.state == CircuitState.OPEN


def test_compatible_summary_does_not_count_waf_as_success():
    summary = summarize_attempts(
        [
            AttemptResult(
                AttemptStatus.SUCCESS,
                "opencli",
                new_articles=2,
                saved_articles=2,
            ),
            AttemptResult(AttemptStatus.NO_UPDATE, "opencli"),
            AttemptResult(AttemptStatus.DUPLICATE, "opencli", saved_articles=1),
            AttemptResult(AttemptStatus.BLOCKED_WAF, "nodriver", error="slider"),
        ]
    )

    assert summary == {
        "total_attempts": 4,
        "successful": 3,
        "failed": 1,
        "new_articles": 2,
        "saved_articles": 3,
    }
