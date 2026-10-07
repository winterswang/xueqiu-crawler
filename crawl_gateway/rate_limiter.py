from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from typing import Callable

from crawl_gateway.config import RateLimitConfig


@dataclass(frozen=True)
class RateReservation:
    allowed: bool
    wait_seconds: float = 0.0
    reason: str | None = None


class SiteRateLimiter:
    def __init__(
        self,
        config: RateLimitConfig,
        *,
        interval_selector: Callable[[float, float], float] | None = None,
    ) -> None:
        if config.max_interval_seconds < config.min_interval_seconds:
            raise ValueError("max_interval_seconds must be >= min_interval_seconds")
        self._config = config
        self._interval_selector = interval_selector or (
            lambda minimum, maximum: random.uniform(minimum, maximum)
        )
        self._hourly: deque[float] = deque()
        self._daily: deque[float] = deque()
        self._next_allowed_at = 0.0

    def reserve(self, now: float) -> RateReservation:
        self._prune(self._hourly, now, 3600.0)
        self._prune(self._daily, now, 86400.0)

        if len(self._daily) >= self._config.daily_requests:
            return RateReservation(False, reason="daily_requests_exhausted")
        if len(self._hourly) >= self._config.hourly_requests:
            retry_after = self._hourly[0] + 3600.0 - now
            return RateReservation(
                False,
                wait_seconds=retry_after,
                reason="hourly_requests_exhausted",
            )

        wait_seconds = max(0.0, self._next_allowed_at - now)
        reserved_at = now + wait_seconds
        interval = self._interval_selector(
            self._config.min_interval_seconds,
            self._config.max_interval_seconds,
        )
        self._next_allowed_at = reserved_at + interval
        self._hourly.append(reserved_at)
        self._daily.append(reserved_at)
        return RateReservation(True, wait_seconds=wait_seconds)

    def seed(self, timestamps: list[float]) -> None:
        ordered = sorted(timestamps)
        self._hourly.clear()
        self._daily.clear()
        self._hourly.extend(ordered)
        self._daily.extend(ordered)
        if ordered:
            self._next_allowed_at = ordered[-1] + self._config.max_interval_seconds

    @staticmethod
    def _prune(timestamps: deque[float], now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
