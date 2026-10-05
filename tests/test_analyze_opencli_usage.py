"""台账汇总必须把「站点请求」与「本地命令」分开算峰值。

2026-10-05 踩到：`analyze_opencli_usage.py` 的分钟峰值把所有 opencli 调用
混在一起，`browser get/extract/close`（只跟已打开的本地标签页交互，不发
雪球请求、也不受限速器约束）被算进「访问频率」，于是当天读出 16 次/分的
假峰值，而同期真正的站点请求峰值只有 6 次/分 —— 口径差一个量级，会把
「要不要再降速」的判断直接带偏。风控只看站点请求。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analyze_opencli_usage import (  # noqa: E402
    SITE_OPERATIONS,
    _peak,
    render,
    summarize,
)

BASE = datetime(2026, 10, 5, 13, 0, 0)


def _record(minute: int, second: int, operation: str, ok: bool = True) -> dict:
    ts = BASE + timedelta(minutes=minute, seconds=second)
    return {
        "ts": ts.isoformat(),
        "operation": operation,
        "source": "test",
        "ok": ok,
    }


def test_local_commands_do_not_count_toward_site_peak():
    """站点 2 次 + 本地 5 次在同一分钟：站点峰值必须是 2，不是 7。"""
    records = [_record(0, s, "browser:get") for s in range(5)] + [
        _record(0, 10, "browser:open"),
        _record(0, 20, "user-articles"),
    ]

    summary = summarize(records, BASE - timedelta(hours=1))

    assert summary["site_total"] == 2
    assert summary["local_total"] == 5
    assert summary["by_minute_site"]["2026-10-05 13:00"] == 2
    assert summary["by_minute_local"]["2026-10-05 13:00"] == 5
    # 全部口径仍然是 7 —— 留着它只为排查耗时，不能拿来判风控
    assert summary["by_minute"]["2026-10-05 13:00"] == 7
    assert _peak(summary["by_minute_site"]) == ("2026-10-05 13:00", 2)


def test_site_failed_counts_only_site_operations():
    records = [
        _record(0, 1, "browser:open", ok=False),  # 站点失败
        _record(0, 2, "browser:extract", ok=False),  # 本地失败，不算风控压力
        _record(0, 3, "user-articles", ok=True),
    ]

    summary = summarize(records, BASE - timedelta(hours=1))

    assert summary["failed"] == 2
    assert summary["site_failed"] == 1


def test_every_throttled_operation_is_classified_as_site():
    """限速器拦的就是站点请求，两份名单必须一致 —— 别一边加一边忘。

    `_should_throttle` 放行的形态是 `browser open` 与 `xueqiu <子命令>`；
    SITE_OPERATIONS 是它们在台账 `operation` 字段里的名字。
    """
    assert SITE_OPERATIONS == frozenset(
        {
            "browser:open",
            "news",
            "comments",
            "replies",
            "stock-notices",
            "user-articles",
            "stock",
            "search",
        }
    )
    for local in ("browser:get", "browser:extract", "browser:close"):
        assert local not in SITE_OPERATIONS


def test_peak_returns_earliest_minute_on_tie():
    assert _peak({}) == ("", 0)
    assert _peak({"13:02": 4, "13:01": 4, "13:03": 1}) == ("13:01", 4)


def test_render_headlines_the_site_peak():
    records = [_record(0, s, "browser:get") for s in range(5)]
    records += [_record(0, 10, "browser:open"), _record(0, 20, "user-articles")]
    summary = summarize(records, BASE - timedelta(hours=1))

    text = render(summary)

    assert "站点请求 2 次" in text
    assert "本地命令 5 次" in text
    assert "站点请求分钟峰值 2 次/分" in text
    assert "站点请求分钟峰值 Top" in text


# ── 探针未命中不计入失败 ────────────────────────────────────────────────────
#
# 2026-10-05 实测：detail_fetcher 试 8 个正文选择器，没命中就换下一个。两个
# 页面各留 3 条 rc=2，台账把 browser:extract 报成「75% 失败」—— 而两次抓取
# 都成功了、管线 7/7。调用方现在用 expect_miss 声明这类调用。


def _probe(minute: int, second: int, selector: str) -> dict:
    row = _record(minute, second, "browser:extract", ok=False)
    row["expect_miss"] = True
    row["command"] = [
        "opencli",
        "browser",
        "detailfetch0",
        "extract",
        "--selector",
        selector,
    ]
    return row


def test_probe_misses_are_not_failures():
    """3 次探针未命中 + 1 次真失败：失败数必须是 1，探针单列 3。"""
    records = [
        _probe(0, 1, "div.article"),
        _probe(0, 2, "#artibody"),
        _probe(0, 3, ".article-content"),
        _record(0, 4, "user-articles", ok=False),  # 真失败
    ]

    summary = summarize(records, BASE - timedelta(hours=1))

    assert summary["failed"] == 1
    assert len(summary["probe_misses"]) == 3
    assert [r["operation"] for r in summary["failures"]] == ["user-articles"]
    # 全部调用数不受影响 —— 探针确实发生了，只是不该算失败
    assert summary["total"] == 4


def test_probe_misses_stay_out_of_hour_failure_counts():
    records = [_probe(0, 1, "div.article"), _probe(0, 2, "#artibody")]

    summary = summarize(records, BASE - timedelta(hours=1))

    assert summary["by_hour_fail"]["2026-10-05 13:00"] == 0
    assert summary["by_operation_fail"]["browser:extract"] == 0
    assert summary["failed"] == 0


def test_records_without_the_flag_still_count_as_failures():
    """旧台账没有 expect_miss 字段 —— 必须仍按失败计（安全方向）。"""
    records = [_record(0, 1, "browser:extract", ok=False)]

    summary = summarize(records, BASE - timedelta(hours=1))

    assert summary["failed"] == 1
    assert summary["probe_misses"] == []


def test_render_separates_probe_misses_from_failures():
    records = [_probe(0, 1, "div.article"), _record(0, 2, "news", ok=False)]

    text = render(summarize(records, BASE - timedelta(hours=1)))

    assert "探针未命中 1 次（预期控制流，非失败）" in text
    assert "失败 1 次" in text
    assert "另有探针未命中 1 次" in text
