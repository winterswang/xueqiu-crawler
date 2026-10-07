from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from crawl_gateway.models import AttemptStatus


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int
    backoff_base_seconds: int
    backoff_multiplier: float
    retry_statuses: frozenset[AttemptStatus]


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int
    window_minutes: int
    cooldown_minutes: int
    hard_failure_statuses: frozenset[AttemptStatus]


@dataclass(frozen=True)
class RateLimitConfig:
    concurrency: int
    min_interval_seconds: float
    max_interval_seconds: float
    hourly_requests: int
    daily_requests: int


@dataclass(frozen=True)
class SiteConfig:
    code: str
    enabled: bool
    backend_priority: tuple[str, ...]
    rate_limit: RateLimitConfig
    retry: RetryConfig
    circuit_breaker: CircuitBreakerConfig


@dataclass(frozen=True)
class SitesConfig:
    sites: dict[str, SiteConfig]


def load_sites_config(path: str | Path) -> SitesConfig:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"unable to load config {config_path}: {exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("sites"), dict):
        raise ConfigError("config must contain a mapping at sites")
    if not raw["sites"]:
        raise ConfigError("at least one site must be configured")

    sites: dict[str, SiteConfig] = {}
    for code, site_raw in raw["sites"].items():
        if not isinstance(code, str) or not code.strip():
            raise ConfigError("site codes must be non-empty strings")
        sites[code] = _parse_site(code, site_raw)
    return SitesConfig(sites=sites)


def _parse_site(code: str, raw: Any) -> SiteConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"site {code} must be a mapping")

    rate_limit = _parse_rate_limit(raw.get("rate_limit"), code)
    if rate_limit.max_interval_seconds < rate_limit.min_interval_seconds:
        field = f"{code}.rate_limit.max_interval_seconds"
        raise ConfigError(f"{field} must be >= min_interval_seconds")

    return SiteConfig(
        code=code,
        enabled=_boolean(raw.get("enabled"), f"{code}.enabled"),
        backend_priority=_string_tuple(raw, "backend_priority", code),
        rate_limit=rate_limit,
        retry=_parse_retry(raw.get("retry"), code),
        circuit_breaker=_parse_circuit_breaker(raw.get("circuit_breaker"), code),
    )


def _parse_rate_limit(raw: Any, code: str) -> RateLimitConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{code}.rate_limit must be a mapping")
    return RateLimitConfig(
        concurrency=_positive_int(
            raw.get("concurrency"), f"{code}.rate_limit.concurrency"
        ),
        min_interval_seconds=_positive_float(
            raw.get("min_interval_seconds"),
            f"{code}.rate_limit.min_interval_seconds",
        ),
        max_interval_seconds=_positive_float(
            raw.get("max_interval_seconds"),
            f"{code}.rate_limit.max_interval_seconds",
        ),
        hourly_requests=_positive_int(
            raw.get("hourly_requests"), f"{code}.rate_limit.hourly_requests"
        ),
        daily_requests=_positive_int(
            raw.get("daily_requests"), f"{code}.rate_limit.daily_requests"
        ),
    )


def _parse_retry(raw: Any, code: str) -> RetryConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{code}.retry must be a mapping")
    return RetryConfig(
        max_attempts=_positive_int(
            raw.get("max_attempts"), f"{code}.retry.max_attempts"
        ),
        backoff_base_seconds=_positive_int(
            raw.get("backoff_base_seconds"),
            f"{code}.retry.backoff_base_seconds",
        ),
        backoff_multiplier=_positive_float(
            raw.get("backoff_multiplier"), f"{code}.retry.backoff_multiplier"
        ),
        retry_statuses=_statuses(
            raw.get("retry_statuses"), f"{code}.retry.retry_statuses"
        ),
    )


def _parse_circuit_breaker(raw: Any, code: str) -> CircuitBreakerConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{code}.circuit_breaker must be a mapping")
    return CircuitBreakerConfig(
        failure_threshold=_positive_int(
            raw.get("failure_threshold"),
            f"{code}.circuit_breaker.failure_threshold",
        ),
        window_minutes=_positive_int(
            raw.get("window_minutes"), f"{code}.circuit_breaker.window_minutes"
        ),
        cooldown_minutes=_positive_int(
            raw.get("cooldown_minutes"),
            f"{code}.circuit_breaker.cooldown_minutes",
        ),
        hard_failure_statuses=_statuses(
            raw.get("hard_failure_statuses"),
            f"{code}.circuit_breaker.hard_failure_statuses",
        ),
    )


def _string_tuple(raw: dict[str, Any], key: str, code: str) -> tuple[str, ...]:
    values = raw.get(key)
    if not isinstance(values, list) or not values:
        raise ConfigError(f"{code}.{key} must be a non-empty list")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ConfigError(f"{code}.{key} entries must be non-empty strings")
    if len(set(values)) != len(values):
        raise ConfigError(f"{code}.{key} entries must be unique")
    return tuple(values)


def _statuses(raw: Any, field: str) -> frozenset[AttemptStatus]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{field} must be a non-empty list")
    try:
        return frozenset(AttemptStatus(value) for value in raw)
    except ValueError as exc:
        valid = ", ".join(status.value for status in AttemptStatus)
        raise ConfigError(
            f"{field} contains an unknown status; valid values: {valid}"
        ) from exc


def _boolean(raw: Any, field: str) -> bool:
    if not isinstance(raw, bool):
        raise ConfigError(f"{field} must be a boolean")
    return raw


def _positive_int(raw: Any, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ConfigError(f"{field} must be a positive integer")
    return raw


def _positive_float(raw: Any, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw <= 0:
        raise ConfigError(f"{field} must be a positive number")
    return float(raw)
