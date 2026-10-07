from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AttemptStatus(StrEnum):
    SUCCESS = "success"
    NO_UPDATE = "no_update"
    DUPLICATE = "duplicate"
    BLOCKED_WAF = "blocked_waf"
    CAPTCHA_REQUIRED = "captcha_required"
    AUTH_EXPIRED = "auth_expired"
    RATE_LIMITED = "rate_limited"
    HTTP_ERROR = "http_error"
    PARSE_ERROR = "parse_error"
    NETWORK_ERROR = "network_error"
    CIRCUIT_OPEN = "circuit_open"
    SKIPPED = "skipped"


SUCCESS_STATUSES = frozenset(
    {
        AttemptStatus.SUCCESS,
        AttemptStatus.NO_UPDATE,
        AttemptStatus.DUPLICATE,
    }
)
FAILURE_STATUSES = frozenset(
    {
        AttemptStatus.BLOCKED_WAF,
        AttemptStatus.CAPTCHA_REQUIRED,
        AttemptStatus.AUTH_EXPIRED,
        AttemptStatus.RATE_LIMITED,
        AttemptStatus.HTTP_ERROR,
        AttemptStatus.PARSE_ERROR,
        AttemptStatus.NETWORK_ERROR,
    }
)
HARD_FAILURE_STATUSES = frozenset(
    {
        AttemptStatus.BLOCKED_WAF,
        AttemptStatus.CAPTCHA_REQUIRED,
        AttemptStatus.AUTH_EXPIRED,
    }
)


@dataclass(frozen=True)
class AttemptResult:
    status: AttemptStatus
    backend: str
    error: str | None = None
    duration_ms: int = 0
    new_articles: int = 0
    saved_articles: int = 0

    @property
    def successful(self) -> bool:
        return self.status in SUCCESS_STATUSES
