from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from crawl_gateway.models import FAILURE_STATUSES, SUCCESS_STATUSES, AttemptResult

logger = logging.getLogger(__name__)


def summarize_attempts(results: Iterable[AttemptResult]) -> dict[str, int]:
    attempts = tuple(results)
    return {
        "total_attempts": len(attempts),
        "successful": sum(result.status in SUCCESS_STATUSES for result in attempts),
        "failed": sum(result.status in FAILURE_STATUSES for result in attempts),
        "new_articles": sum(result.new_articles for result in attempts),
        "saved_articles": sum(result.saved_articles for result in attempts),
    }


def export_last_crawl_stats(
    *,
    summary: dict[str, object],
    total_tasks: int,
    data_dir: str | Path,
    now: datetime | None = None,
) -> Path | None:
    """按旧版字段导出 .last_crawl_stats.json，供 scripts/generate_report.py 读取。

    写入失败只记 warning 不抛出，与旧爬虫行为一致：统计文件缺失时报告仍可生成。
    """
    stats = {
        "date": (now or datetime.now()).strftime("%Y-%m-%d"),
        "total_users": total_tasks,
        "successful": int(summary.get("successful", 0)),
        "failed": int(summary.get("failed", 0)),
        "new_articles": int(summary.get("new_articles", 0)),
        # 与 legacy 爬虫写出的同名字段对齐。**故意不进 compare_crawl_outputs 的
        # STATS_FIELDS** —— 两侧统计精度不同（legacy 记篇数，gateway 记遇到被拦的
        # 账号数），拿来对比只会产生噪音；它只用于让人/巡检区分安静日与被拦日。
        "blocked_articles": int(summary.get("blocked_articles", 0)),
    }
    path = Path(data_dir) / ".last_crawl_stats.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("导出 .last_crawl_stats.json 失败：%s", path)
        return None
    return path
