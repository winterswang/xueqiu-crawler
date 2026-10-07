from __future__ import annotations

from dataclasses import dataclass

from crawl_gateway.config import RetryConfig
from crawl_gateway.models import AttemptStatus


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    delay_seconds: float
    reason: str


class RetryPolicy:
    def __init__(self, config: RetryConfig) -> None:
        self._config = config

    def decide(self, status: AttemptStatus, completed_attempts: int) -> RetryDecision:
        if completed_attempts < 1:
            raise ValueError("completed_attempts must be >= 1")
        if completed_attempts >= self._config.max_attempts:
            return RetryDecision(False, 0.0, "max_attempts_reached")
        if status not in self._config.retry_statuses:
            return RetryDecision(False, 0.0, "status_not_retryable")

        exponent = completed_attempts - 1
        delay = self._config.backoff_base_seconds * (
            self._config.backoff_multiplier**exponent
        )
        return RetryDecision(True, delay, "retry_scheduled")
