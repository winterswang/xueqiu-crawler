from __future__ import annotations

from collections.abc import Iterable

from crawl_gateway.models import FAILURE_STATUSES, SUCCESS_STATUSES, AttemptResult


def summarize_attempts(results: Iterable[AttemptResult]) -> dict[str, int]:
    attempts = tuple(results)
    return {
        "total_attempts": len(attempts),
        "successful": sum(result.status in SUCCESS_STATUSES for result in attempts),
        "failed": sum(result.status in FAILURE_STATUSES for result in attempts),
        "new_articles": sum(result.new_articles for result in attempts),
        "saved_articles": sum(result.saved_articles for result in attempts),
    }
