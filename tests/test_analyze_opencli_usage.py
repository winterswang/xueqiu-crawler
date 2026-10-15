"""台账汇总必须把「站点请求」与「本地命令」分开算峰值。

2026-10-05 踩到：`analyze_opencli_usage.py` 的分钟峰值把所有 opencli 调用
混在一起，`browser get/extract/close`（只跟已打开的本地标签页交互，不发
雪球请求、也不受限速器约束）被算进「访问频率」，于是当天读出 16 次/分的
假峰值，而同期真正的站点请求峰值只有 6 次/分 —— 口径差一个量级，会把
「要不要再降速」的判断直接带偏。风控只看站点请求。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from collections import Counter  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analyze_opencli_usage import _parse_ts as analyze_parse_ts  # noqa: E402
from analyze_opencli_usage import (  # noqa: E402
    SITE_OPERATIONS,
    _peak,
    _unattributed_by_domain_operation,
    _unattributed_clusters,
    load_records,
    render,
    render_adapter,
    summarize,
    summarize_adapter,
    uncovered_operations,
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


def test_site_operations_set_is_pinned():
    """钉住 SITE_OPERATIONS 的确切内容（增删都要显式改这里）.

    注意：这**只**钉字面量。真正的跨模块不变量是
    `test_every_throttled_invocation_lands_in_site_operations`，那条会真去调
    限速器和 logger。
    """
    assert SITE_OPERATIONS == frozenset(
        {
            "browser:open",
            "article",
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


def test_every_throttled_invocation_lands_in_site_operations():
    """被限速器拦住的每一条调用，台账口径都必须把它算成站点操作.

    三个名字是跨模块拼起来的：`_should_throttle` 决定拦不拦、
    `_operation` 决定台账里叫什么、SITE_OPERATIONS 决定算不算站点请求。
    2026-10-08 差点真踩到：新增的 `web article` 一度在台账里只记成 "web"，
    而口径是按 operation 分类的 —— 这条主通道的站点请求会全部漏出峰值监控。
    （本用例把三段串起来真调一遍；上面那条只钉字面量，拦不住这种漏。）
    """
    from xueqiu_analyzer.opencli_call_logger import _operation
    from xueqiu_analyzer.opencli_rate_limiter import _should_throttle

    invocations = {
        "browser:open": ["opencli", "browser", "s0", "open", "https://xueqiu.com/1/2"],
        "article": [
            "opencli",
            "web",
            "article",
            "https://xueqiu.com/1/2",
            "-f",
            "json",
        ],
        "news": ["opencli", "xueqiu", "news", "SH600519"],
        "comments": ["opencli", "xueqiu", "comments", "SH600519"],
        "replies": ["opencli", "xueqiu", "replies", "https://xueqiu.com/1/2"],
        "stock-notices": ["opencli", "xueqiu", "stock-notices", "SH600519"],
        "user-articles": ["opencli", "xueqiu", "user-articles", "--user_id", "1"],
        "stock": ["opencli", "xueqiu", "stock", "SH600519"],
    }
    for expected, argv in invocations.items():
        assert _should_throttle(argv), f"{argv} 应当被限速"
        assert _operation(argv) == expected, f"{argv} 的台账名变了"
        assert expected in SITE_OPERATIONS, (
            f"{expected} 被限速却没算成站点操作 —— 站点峰值会漏算"
        )


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


def test_adapter_ledger_is_folded_in_without_breaking_old_keys(tmp_path, monkeypatch):
    """插件台账要能并进来，且**不能**动老键（peak.mjs 等消费者只看老键）。

    这里刻意造了一行带 `Z` 的 UTC 时间戳：插件台账写的是 ISO8601 UTC
    （`2026-10-12T04:41:28.757Z`），而 Python 台账是本地 naive —— 不归一化就会
    在比较窗口时抛 `TypeError: can't compare offset-naive and offset-aware`。
    """
    adapter = tmp_path / "adapter-calls.jsonl"
    adapter.write_text(
        json.dumps(
            {
                "schema": 2,
                "layer": "plugin",
                "ts": "2026-10-12T04:41:28.757Z",
                "started_at": "2026-10-12T04:41:10.585Z",
                "domain_key": "xueqiu",
                "host": "xueqiu.com",
                "site": "xueqiu",
                "operation": "user-articles",
                "source_kind": "monitor",
                "throttled": True,
                "rate_waited_ms": 0,
                "rate_reserved_ms": 10925,
                "long_pause_ms": 0,
                "inflight_at_acquire": 2,
                "concurrency_max": None,
                "overflowed": False,
                "gate_fail_open": False,
                "rate_error": None,
                "ok": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = summarize_adapter(
        load_records(adapter), datetime.now() - timedelta(days=3650)
    )
    assert summary["total"] == 1
    assert summary["by_domain"]["xueqiu"] == 1
    assert summary["inflight"][2] == 1
    assert summary["enforced"] is False, "没配 concurrency 时应报告「观测模式」"


def test_uncovered_operations_flags_only_navigating_browser_calls():
    """只列 `browser:open`（**会真导航**那个），别把本地交互也算成风险。

    `browser:get / extract / close` 只跟已打开的本地标签页交互、不发站点请求
    （限速器自己的注释就是这么写的）。把它们一起算作「覆盖不到」会把无害的本地
    操作说成风险，反而把真正该看的 `browser:open` 淹掉。
    """
    cutoff = datetime.now() - timedelta(days=3650)
    rows = [
        {"ts": "2026-10-12T08:00:00", "operation": "browser:open"},
        {"ts": "2026-10-12T08:00:01", "operation": "browser:extract"},
        {"ts": "2026-10-12T08:00:02", "operation": "browser:close"},
        {"ts": "2026-10-12T08:00:03", "operation": "article"},
    ]
    counter = uncovered_operations(rows, cutoff)
    assert counter["browser:open"] == 1
    assert "browser:extract" not in counter, "本地交互、不发站点请求，不该算覆盖不到"
    assert "browser:close" not in counter
    assert "article" not in counter, "article 走钩子、是被治理的，不该算作覆盖不到"


def test_adapter_timestamp_with_z_parses_on_any_python():
    """带 `Z` 的 UTC 时间戳必须能解析并落到正确的时间窗内。

    `datetime.fromisoformat` 到 Python 3.11 才认尾部 `Z`；低版本会抛 ValueError →
    每行都解析成 None → 整块静默读成「没有记录」。这条测试直接钉住解析结果。
    """
    row = {"ts": "2026-10-12T04:41:28.757Z"}
    parsed = analyze_parse_ts(row)
    assert parsed is not None, "带 Z 的时间戳解析失败了 —— 低版本 Python 会把整块读成空"
    assert parsed.year == 2026 and parsed.month == 10 and parsed.day == 12

    summary = summarize_adapter([row], datetime(2026, 1, 1))
    assert summary["total"] == 1, "该行应落在时间窗内"


# ---------- 「管理外流量」聚类：未标身份的调用怎么归因 ----------
#
# `XUEQIU_CALL_SOURCE` 不设就落 `source_kind="manual"`，与「手敲一行」无法区分。
# 读取侧要做的是**尽量还原出「谁在批量抓」**，靠两条腿：长驻进程按 ppid 聚簇，
# 每次一 shell 的散点按「目标」再聚一次。下面几条分别钉住这两条腿与它们的边界。


def _adapter_row(
    ppid: object,
    started_at: str,
    *,
    source_kind: str = "manual",
    domain_key: str = "xiaohongshu",
    operation: str = "search",
    target: str | None = "AI工具",
    error_code: str | None = None,
) -> dict:
    row: dict = {
        "ppid": ppid,
        "started_at": started_at,
        "ts": started_at,
        "source_kind": source_kind,
        "domain_key": domain_key,
        "operation": operation,
    }
    if target is not None:
        row["target"] = target
    if error_code is not None:
        row["error_code"] = error_code
    return row


@pytest.fixture()
def tz_shanghai():
    """把进程时区钉到东八区。

    没有它，「本地 ≠ UTC」的断言在 CI（多半跑在 UTC）下会**空转通过** —— 那时
    `astimezone()` 归一等价于不归一，正好盖住要抓的 bug。
    """
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_cluster_span_is_local_not_utc(tz_shanghai):
    """簇的时间跨度必须是**本地**时间，不是原始 UTC 串。

    `started_at` 是插件写的 UTC（`…Z`）。此前渲染端直接 `started_at[11:16]` 切片，
    于是在同一份报告里出现两套时钟 —— 2026-10-12 真发生过：报告印 `06:03–06:54`，
    而那一刻本地时间是 `14:03–14:54`，差 8 小时。
    """
    rows = [_adapter_row(62253, f"2026-10-12T{h:02d}:00:00.000Z") for h in range(6, 12)]
    clusters, leftover = _unattributed_clusters(rows)
    assert leftover == []
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["count"] == 6
    # 06:00Z / 11:00Z → 东八区 14:00 / 19:00
    assert cluster["first"][11:16] == "14:00", f"切出来的是 UTC：{cluster['first']}"
    assert cluster["last"][11:16] == "19:00", f"切出来的是 UTC：{cluster['last']}"


def test_shell_per_call_is_recovered_by_target():
    """每次调用各起一个 shell（ppid 各不相同）时，ppid 聚簇看不见它 —— 按目标能补上。

    这是那条「agent 会话逐条敲 opencli」的漏口：8 次调用 8 个 ppid，一个簇都聚不成，
    若不补第二条腿，整轮批量任务就消失在「< 5 次」里，正是要感知的那种流量。
    """
    rows = [_adapter_row(70000 + i, f"2026-10-12T10:{i:02d}:00.000Z") for i in range(8)]
    clusters, leftover = _unattributed_clusters(rows)
    assert clusters == [], "8 个不同 ppid 不该被聚成一个进程簇"
    assert len(leftover) == 8
    assert _unattributed_by_domain_operation(leftover) == [("xiaohongshu/search", 8)]


def test_only_manual_rows_considered_and_missing_ppid_not_dropped():
    """只归因未标身份的行；缺 ppid 的行必须落散点，不能被吞掉。"""
    rows = [
        _adapter_row(1, "2026-10-12T10:00:00.000Z", source_kind="monitor"),
        _adapter_row(None, "2026-10-12T10:01:00.000Z"),
        *[_adapter_row(2, f"2026-10-12T10:0{i}:00.000Z") for i in range(2, 7)],
    ]
    clusters, leftover = _unattributed_clusters(rows)
    assert [c["ppid"] for c in clusters] == [2], "monitor 行不该进簇"
    assert len(leftover) == 1, "缺 ppid 的行被吞掉了"
    assert leftover[0]["ppid"] is None


def test_unparseable_time_does_not_blank_out_the_elsewhere_span():
    """一行时间戳坏掉，不能把**其余**行的跨度抹成空、也不能让它只剩半截。

    旧实现用 `min(str(started_at))`：空串恒为最小/最大，于是**一行坏数据**就让整簇
    的 `first`/`last` 变成 ""，渲染出 `–` 这种空跨度。

    坏行本身排不上时间轴 → 自成一段、落散点（它仍计入「未标身份」的散点统计，
    只是不跟可定位的行混作同一轮）。
    """
    rows = [
        _adapter_row(9, ""),  # 坏行
        *[_adapter_row(9, f"2026-10-12T{h:02d}:00:00.000Z") for h in (7, 8, 9, 10, 11)],
    ]
    clusters, leftover = _unattributed_clusters(rows)
    assert len(leftover) == 1, "坏行该落散点，而不是并进某一轮"
    assert leftover[0]["started_at"] == ""
    assert len(clusters) == 1
    assert clusters[0]["count"] == 5, "可定位的 5 次仍要成簇"
    assert clusters[0]["first"], "一行坏数据把整簇跨度抹空了"
    assert clusters[0]["last"], "一行坏数据把整簇跨度抹空了"


def test_cluster_with_no_parseable_time_at_all_blanks_the_span():
    """全是坏行时，跨度留空（渲染端会整段省掉），但次数照记。"""
    rows = [_adapter_row(9, "") for _ in range(5)]
    clusters, leftover = _unattributed_clusters(rows)
    assert leftover == []
    assert clusters[0]["count"] == 5
    assert clusters[0]["first"] == ""
    assert clusters[0]["last"] == ""


def test_summarize_adapter_exposes_leftover_breakdown():
    """摘要里要同时带上「散点计数」与「散点按目标聚合」——上游渲染与 JSON 都靠它。"""
    rows = [_adapter_row(70000 + i, f"2026-10-12T10:{i:02d}:00.000Z") for i in range(3)]
    summary = summarize_adapter(rows, datetime(2026, 1, 1))
    assert summary["unattributed"] == []
    assert summary["unattributed_singles"] == 3
    assert summary["unattributed_singles_by_domain_operation"] == [
        ("xiaohongshu/search", 3)
    ]


def test_same_ppid_far_apart_splits_into_two_clusters():
    """同一 ppid、但相隔很远的调用不能被并成一簇 —— OS 会复用 pid。

    长窗（`--hours 72`）下，一个已退出进程的 pid 被新进程接着用，两者会被并成
    「一个进程 N 次，07:00–17:00」的假跨度。超过断裂阈值即切段。
    """
    rows = [
        _adapter_row(4242, f"2026-10-12T{h:02d}:00:00.000Z") for h in range(5, 12)
    ] + [_adapter_row(4242, f"2026-10-13T{h:02d}:00:00.000Z") for h in range(5, 12)]
    clusters, leftover = _unattributed_clusters(rows)
    assert leftover == []
    assert len(clusters) == 2, "相隔一天的同一 pid 被并成了一簇"
    assert {c["count"] for c in clusters} == {7}


def test_cooldown_blocked_calls_are_not_counted_as_failures():
    """被熔断器拦下的调用**没发出任何请求**，不该算失败。

    否则熔断器一生效，它自己的成绩就被算成「站点风控恶化」—— 判断被带反。
    判据用插件台账的显式字段 `cooldown_active`（Python 台账没有这个标记）。
    """
    rows = [
        # 被熔断拦下：ok=False 但一个请求都没发
        {
            "ppid": 1,
            "started_at": "2026-10-15T02:00:00.000Z",
            "ts": "2026-10-15T02:00:00.000Z",
            "source_kind": "monitor",
            "operation": "news",
            "domain_key": "xueqiu",
            "ok": False,
            "cooldown_active": True,
            "error_code": "BROWSER_CONNECT",
        },
        # 真失败
        {
            "ppid": 2,
            "started_at": "2026-10-15T02:01:00.000Z",
            "ts": "2026-10-15T02:01:00.000Z",
            "source_kind": "monitor",
            "operation": "news",
            "domain_key": "xueqiu",
            "ok": False,
            "error_code": "COMMAND_EXEC",
        },
    ]

    summary = summarize_adapter(rows, datetime(2026, 1, 1))

    assert summary["cooldown_blocked"] == 1
    assert summary["failed"] == 1, "被熔断拦下的那次不该算进失败"
    assert summary["total"] == 2, "但它确实发生过，总数不变"


def test_cooldown_blocked_shows_up_in_the_report():
    rows = [
        {
            "ppid": 1,
            "started_at": "2026-10-15T02:00:00.000Z",
            "ts": "2026-10-15T02:00:00.000Z",
            "source_kind": "monitor",
            "operation": "news",
            "domain_key": "xueqiu",
            "ok": False,
            "cooldown_active": True,
            "error_code": "BROWSER_CONNECT",
        }
    ]
    summary = summarize_adapter(rows, datetime(2026, 1, 1))

    text = render_adapter(summary, Counter())

    assert "被**熔断器**拦下" in text


def test_explicit_false_cooldown_row_still_counts_as_failure():
    """反方向：`cooldown_active: false`（插件对**每一行**都写这个字段）必须是**真失败**。

    此前的对照组用的是「缺键」而不是「显式 false」，恰好绕开了最容易写错的那半：
    把真值判断写成 `"cooldown_active" in r`（有键就当被拦）也能全绿，而真实台账里
    727 行有该字段、其中 726 行是 false —— 那会把失败几乎清零。
    """
    rows = [
        {
            "ppid": 1,
            "started_at": "2026-10-15T02:00:00.000Z",
            "ts": "2026-10-15T02:00:00.000Z",
            "source_kind": "monitor",
            "operation": "news",
            "domain_key": "xueqiu",
            "ok": False,
            "cooldown_active": False,  # 显式 false：正常失败
            "error_code": "COMMAND_EXEC",
        }
    ]

    summary = summarize_adapter(rows, datetime(2026, 1, 1))

    assert summary["cooldown_blocked"] == 0
    assert summary["failed"] == 1


def test_error_codes_detail_never_exceeds_failed():
    """`error_codes` 是给 `failed` 做明细的 —— 明细合计不能大于 failed。

    被拦行原本会同时进两边，渲染出「失败 2 次（BROWSER_CONNECT 4 …）」这种自相矛盾的
    括注，并在同一屏里与 ⛔ 行打架。
    """

    def row(ok, code, cooldown=None):
        r = {
            "ppid": 1,
            "started_at": "2026-10-15T02:00:00.000Z",
            "ts": "2026-10-15T02:00:00.000Z",
            "source_kind": "monitor",
            "operation": "news",
            "domain_key": "xueqiu",
            "ok": ok,
            "error_code": code,
        }
        if cooldown is not None:
            r["cooldown_active"] = cooldown
        return r

    rows = [
        row(False, "BROWSER_CONNECT", True),
        row(False, "BROWSER_CONNECT", True),
        row(False, "BROWSER_CONNECT", False),
        row(False, "TIMEOUT", False),
    ]

    summary = summarize_adapter(rows, datetime(2026, 1, 1))

    assert summary["failed"] == 2
    assert sum(summary["error_codes"].values()) == 2, "明细合计必须等于 failed"
    assert summary["cooldown_blocked"] == 2
    assert sum(summary["cooldown_error_codes"].values()) == 2
    # 只有**没被拦**的那条该进明细（计数 1），被拦的两条不该进来
    assert summary["error_codes"]["BROWSER_CONNECT"] == 1


def test_cluster_does_not_call_cooldown_blocks_failures():
    """「管理外流量」簇里也不能把被拦的算成失败。"""
    from analyze_opencli_usage import _unattributed_clusters

    rows = [
        {
            "ppid": 9,
            "started_at": f"2026-10-15T02:0{i}:00.000Z",
            "ts": f"2026-10-15T02:0{i}:00.000Z",
            "source_kind": "manual",
            "domain_key": "xueqiu",
            "operation": "whoami",
            "ok": False,
            "cooldown_active": True,
            "error_code": "BROWSER_CONNECT",
        }
        for i in range(5)
    ]

    clusters, _ = _unattributed_clusters(rows)

    assert clusters[0]["cooldown_blocked"] == 5
    assert clusters[0]["error_codes"] == Counter()


def test_python_ledger_cooldown_blocks_are_excluded_by_the_profile_sentinel():
    """Python 台账没有 `cooldown_active` 字段，但错误正文里有**哨兵 profile 名**。

    这个哨兵是熔断器专用的（`__opencli_risk_cooldown__`），不会与「daemon 真的没连上」
    混同 —— 所以它是精确判据，而不是注释里担心的「只能靠文本猜」。
    """
    blocked = dict(_record(0, 1, "news", ok=False))
    blocked["error"] = {
        "code": "BROWSER_CONNECT",
        "message": 'Browser profile "__opencli_risk_cooldown__" is not connected',
    }
    real_fail = dict(_record(0, 2, "news", ok=False))
    real_fail["error"] = {"code": "BROWSER_CONNECT", "message": "daemon unreachable"}

    summary = summarize([blocked, real_fail], BASE - timedelta(hours=1))

    assert summary["cooldown_blocked"] == 1
    assert summary["failed"] == 1, "只有真正那条该算失败"
    assert summary["site_total"] == 1, "被拦的那次没发请求，不该算站点请求"
    assert summary["total"] == 2, "但它确实发生过"
