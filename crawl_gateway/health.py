from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from crawl_gateway.config import CircuitBreakerConfig
from crawl_gateway.models import AttemptStatus


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitDecision:
    allowed: bool
    state: CircuitState
    probe: bool = False
    retry_after_seconds: float = 0.0


class CircuitBreaker:
    def __init__(self, config: CircuitBreakerConfig) -> None:
        self._config = config
        self.score = 80
        self.state = CircuitState.CLOSED
        self.opened_at = 0.0
        self._probe_pending = False
        self._hard_failures: deque[float] = deque()

    @property
    def hard_failure_times(self) -> tuple[float, ...]:
        return tuple(self._hard_failures)

    def seed_hard_failures(self, timestamps: list[float]) -> None:
        self._hard_failures.clear()
        self._hard_failures.extend(sorted(timestamps))

    def allow(self, now: float) -> CircuitDecision:
        opened_until = self.opened_at + self._config.cooldown_minutes * 60
        if self.state == CircuitState.OPEN:
            if now < opened_until:
                return CircuitDecision(
                    allowed=False,
                    state=CircuitState.OPEN,
                    retry_after_seconds=opened_until - now,
                )
            self.state = CircuitState.HALF_OPEN
            self._probe_pending = True
            return CircuitDecision(True, CircuitState.HALF_OPEN, probe=True)

        if self.state == CircuitState.HALF_OPEN and self._probe_pending:
            return CircuitDecision(False, CircuitState.HALF_OPEN)
        return CircuitDecision(True, CircuitState.CLOSED)

    def record(self, status: AttemptStatus, now: float) -> None:
        if status in {
            AttemptStatus.SUCCESS,
            AttemptStatus.NO_UPDATE,
            AttemptStatus.DUPLICATE,
        }:
            self.score = min(100, self.score + 2)
            self._hard_failures.clear()
            self._probe_pending = False
            self.state = CircuitState.CLOSED
            return

        self.score = max(0, self.score - self._penalty(status))
        if status in self._config.hard_failure_statuses:
            cutoff = now - self._config.window_minutes * 60
            while self._hard_failures and self._hard_failures[0] <= cutoff:
                self._hard_failures.popleft()
            self._hard_failures.append(now)

        hard_failure_trip = len(self._hard_failures) >= self._config.failure_threshold
        half_open_trip = self.state == CircuitState.HALF_OPEN
        if hard_failure_trip or half_open_trip:
            self.state = CircuitState.OPEN
            self.opened_at = now
            self._probe_pending = False

    @staticmethod
    def _penalty(status: AttemptStatus) -> int:
        if status == AttemptStatus.RATE_LIMITED:
            return 20
        if status in {
            AttemptStatus.BLOCKED_WAF,
            AttemptStatus.CAPTCHA_REQUIRED,
            AttemptStatus.AUTH_EXPIRED,
        }:
            return 40
        return 10
