"""Shared crawl access contracts and policies."""

from crawl_gateway.models import (
    AttemptResult,
    AttemptStatus,
    FAILURE_STATUSES,
    HARD_FAILURE_STATUSES,
    SUCCESS_STATUSES,
)

__all__ = [
    "AttemptResult",
    "AttemptStatus",
    "FAILURE_STATUSES",
    "HARD_FAILURE_STATUSES",
    "SUCCESS_STATUSES",
]
