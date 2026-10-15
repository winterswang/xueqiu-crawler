#!/usr/bin/env python3
"""汇总 opencli 对雪球的调用台账，用于归因风控/验证页问题。

数据来自 analyzer 的 opencli_call_logger，默认落在 `~/.opencli/xueqiu-calls.jsonl`
（每条一次调用，字段见该模块）。本脚本只读，不改任何东西。

为什么需要它
------------
2026-10-04 早上 17/17 个账号被重定向到 `check.xueqiu.com/captcha`，页面提示
「访问触发保护…当前网络环境访问频率异常」。判断是不是我们自己打得太频繁，
需要「谁在什么时间调了多少次」——opencli daemon 本身不留请求日志，
所以有了这份台账。

用法
----
    python3 scripts/analyze_opencli_usage.py                 # 最近 24 小时
    python3 scripts/analyze_opencli_usage.py --date 2026-10-04
    python3 scripts/analyze_opencli_usage.py --hours 72 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_LOG = Path.home() / ".opencli" / "xueqiu-calls.jsonl"

# 会**真正发起雪球站点请求**的操作 —— 只有这些才计入限流风险。
# 台账把所有 opencli 调用都记了，其中 browser:get / browser:extract / browser:close
# 只跟已打开的本地标签页交互，不发新请求（限速器也不限它们，见
# opencli_rate_limiter._should_throttle）。
# 2026-10-05 踩过：不分青红皂白算「分钟峰值」会得出 16 次/分的假高，
# 而同期真正的站点请求峰值只有 6 次/分 —— 口径不对会把结论带偏一个量级。
SITE_OPERATIONS = frozenset(
    {
        "browser:open",  # 导航到目标页面
        "article",  # 通用网页正文命令: 内部 page.goto 导航真实页面
        "news",
        "comments",
        "replies",
        "stock-notices",
        "user-articles",
        "stock",
        "search",
    }
)


# 插件侧台账（opencli-adapters/lib/requestlog.js 写）。它记的是**传输层真相**：
# 域名、限频等待/长停、被跳过原因、真实并发。Python 台账看不到这些 ——
# 尤其是「限速器自己的决策」，2026-10-12 那次排查就栽在这个盲区上。
ADAPTER_LOG = Path("~/.opencli/adapter-calls.jsonl").expanduser()

# 插件**看不见**的操作：`opencli browser …` 不经过任何钩子（上游
# jackwener/opencli#2606），所以它们既不在插件台账里、也不受插件层的限频与并发治理。
# 报告里要把它们单列，否则按插件台账算「域名密度」会漏掉这一大块。
#
# 只列 `browser:open`（**会真导航到目标站点**那一个）。`browser:get/extract/close`
# 只跟已打开的本地标签页交互、不发站点请求 —— 把它们一起算作「覆盖不到」会把一个
# 无害的本地操作说成风险，反而把真正该看的那个淹掉。
UNCOVERED_OPERATION_PREFIXES = ("browser:open",)

# 熔断器拦下调用时，会把 `OPENCLI_PROFILE` 指向这个**哨兵 profile 名**，opencli 于是
# 在 routing 阶段硬失败、一个请求都没发。这个字符串是熔断器专用的：它不会与「daemon
# 真的没连上」（同样报 BROWSER_CONNECT）混同，也不含「风控」二字。
#
# 为什么 Python 台账要靠哨兵而插件台账有显式字段：插件台账由 `lib/requestlog.js`
# 直接写 `cooldown_active`；Python 台账只收得到 opencli 的错误正文，只能从文本认。
COOLDOWN_PROFILE_SENTINEL = "__opencli_risk_cooldown__"


def _is_cooldown_block(row: dict) -> bool:
    """这一行是不是被熔断器拦下的（因而**没有发出任何请求**）。

    插件台账有显式字段；Python 台账看错误正文里的哨兵。
    """
    if row.get("cooldown_active") is True:
        return True
    err = row.get("error")
    if isinstance(err, dict):
        err = " ".join(str(v) for v in err.values())
    return COOLDOWN_PROFILE_SENTINEL in str(err or "")


def _adapter_log_path(explicit: str | None) -> Path | None:
    """插件台账路径；显式 `off` 或环境变量 `OPENCLI_ADAPTER_LOG=off` 时返回 None。"""
    raw = (
        explicit if explicit is not None else os.environ.get("OPENCLI_ADAPTER_LOG", "")
    )
    raw = (raw or "").strip()
    if raw.lower() in ("off", "disabled"):
        return None
    return Path(os.path.expanduser(raw)) if raw else ADAPTER_LOG


def _log_path(explicit: str | None) -> Path:
    if explicit:
        return Path(os.path.expanduser(explicit))
    return (
        Path(os.path.expanduser(os.environ.get("XUEQIU_OPENCLI_LOG", "")))
        if os.environ.get("XUEQIU_OPENCLI_LOG")
        else DEFAULT_LOG
    )


def load_records(path: Path) -> list[dict]:
    """读台账；轮转出来的 .1 也一并读入。坏行跳过而不是整份失败。"""
    records: list[dict] = []
    candidates = [path.with_suffix(path.suffix + ".1"), path]
    for candidate in candidates:
        if not candidate.exists():
            continue
        with candidate.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _parse_ts(record: dict) -> datetime | None:
    """台账行的 `ts` → 本地 **naive** datetime。

    两份台账的时间戳格式不同：Python 台账是本地 naive（`2026-10-12T08:14:21.302`），
    插件台账是带 `Z` 的 UTC（`2026-10-12T04:41:28.757Z`）。不归一化就会在比较时抛
    `TypeError: can't compare offset-naive and offset-aware datetimes`。
    """
    raw = str(record.get("ts") or "")
    # `fromisoformat` 到 Python 3.11 才认尾部的 `Z`；低版本（含 macOS 自带 python3）
    # 会抛 ValueError → 每一行都解析成 None → 整块静默读成「没有记录」。
    # 显式换成 `+00:00` 就不挑版本了。
    if raw.endswith(("Z", "z")):
        raw = f"{raw[:-1]}+00:00"
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts.astimezone().replace(tzinfo=None) if ts.tzinfo else ts


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "—"


def _peak(counter) -> tuple[str, int]:
    """返回 (峰值所在的分钟, 次数)；空则 ("", 0)。同分并列取最早的一分钟。"""
    if not counter:
        return "", 0
    top = max(counter.values())
    minute = min(key for key, value in counter.items() if value == top)
    return minute, top


def _bar(count: int, peak: int, width: int = 24) -> str:
    if peak <= 0:
        return ""
    filled = max(1, round(width * count / peak))
    return "█" * filled


def summarize(records: list[dict], cutoff: datetime) -> dict:
    rows = [r for r in records if (ts := _parse_ts(r)) and ts >= cutoff]
    rows.sort(key=lambda r: str(r.get("ts", "")))

    by_hour: Counter[str] = Counter()
    by_hour_fail: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    by_source_fail: Counter[str] = Counter()
    by_operation: Counter[str] = Counter()
    by_operation_fail: Counter[str] = Counter()
    by_minute: Counter[str] = Counter()
    by_minute_site: Counter[str] = Counter()
    by_minute_local: Counter[str] = Counter()
    failures: list[dict] = []
    probe_misses: list[dict] = []
    cooldown_blocked = 0

    for row in rows:
        ts = _parse_ts(row)
        if ts is None:
            continue
        hour = ts.strftime("%Y-%m-%d %H:00")
        minute = ts.strftime("%Y-%m-%d %H:%M")
        source = str(row.get("source") or "?")
        operation = str(row.get("operation") or "?")
        ok = bool(row.get("ok"))

        by_hour[hour] += 1
        by_source[source] += 1
        by_operation[operation] += 1
        by_minute[minute] += 1
        if _is_cooldown_block(row):
            # 熔断器拦下的调用**没发出任何请求**（routing 阶段就失败）—— 既不是站点
            # 请求、也不是失败。混进去，熔断器一生效就把自己的成绩算成「站点风控恶化」，
            # 而站点请求分钟峰值正是用来判断这件事的那一列。
            cooldown_blocked += 1
            continue
        if operation in SITE_OPERATIONS:
            by_minute_site[minute] += 1
        else:
            by_minute_local[minute] += 1
        if not ok:
            # 探针未命中不算失败：调用方（detail_fetcher 的正文选择器循环）
            # 明确声明过「这次不中」是控制流的一部分。混进失败率会把
            # browser:extract 报成 75% 失败，而实际上每次抓取都成功了。
            if row.get("expect_miss"):
                probe_misses.append(row)
            else:
                by_hour_fail[hour] += 1
                by_source_fail[source] += 1
                by_operation_fail[operation] += 1
                failures.append(row)

    return {
        "total": len(rows),
        "failed": len(failures),
        "first": str(rows[0].get("ts", "")) if rows else "",
        "last": str(rows[-1].get("ts", "")) if rows else "",
        "by_hour": by_hour,
        "by_hour_fail": by_hour_fail,
        "by_source": by_source,
        "by_source_fail": by_source_fail,
        "by_operation": by_operation,
        "by_operation_fail": by_operation_fail,
        "by_minute": by_minute,
        "by_minute_site": by_minute_site,
        "by_minute_local": by_minute_local,
        "site_total": sum(by_minute_site.values()),
        "local_total": sum(by_minute_local.values()),
        "site_failed": sum(
            1 for r in failures if str(r.get("operation") or "?") in SITE_OPERATIONS
        ),
        "failures": failures,
        "probe_misses": probe_misses,
        "cooldown_blocked": cooldown_blocked,
    }


def _in_window(row: dict, cutoff: datetime, end: datetime | None) -> bool:
    ts = _parse_ts(row)
    if ts is None:  # 先收窄类型，避免 mypy 报「datetime 与 None 不可比较」
        return False
    return ts >= cutoff and (end is None or ts < end)


def _local_ts(value: object) -> datetime | None:
    """`started_at`（插件的 UTC 串）→ 本地 naive datetime。

    直接切片 `started_at[11:16]` 拿到的是 **UTC** 时刻，会与报告别处的本地时间差 8 小时 ——
    同一份报告两套时钟。走 `_parse_ts` 归一化，和其余部分对齐（它读的是 `ts` 键，这里借道）。
    """
    return _parse_ts({"ts": value})


# 同一 pid 的两次调用间隔超过它就当成**两轮**任务。
# 防的是 OS 复用 pid：长窗（如 `--hours 72`）下，一个已退出的进程的 pid 被新进程
# 接着用，两者会被并成一个簇，渲染出「一个进程 N 次，08:00–18:00」的假跨度。
_CLUSTER_GAP_SECONDS = 2 * 60 * 60


def _segments(group: list[dict]) -> list[list[dict]]:
    """把同一进程的调用按时间断裂点切成连续段（见 `_CLUSTER_GAP_SECONDS`）。

    解析不出时间的行自成一段（它们无法参与时间排序，但仍要计数 —— 不能吞掉）。
    """
    timed = sorted(
        ((ts, row) for row in group if (ts := _local_ts(row.get("started_at")))),
        key=lambda pair: pair[0],
    )
    segments: list[list[dict]] = []
    current: list[dict] = []
    previous: datetime | None = None
    for ts, row in timed:
        if (
            previous is not None
            and (ts - previous).total_seconds() > _CLUSTER_GAP_SECONDS
        ):
            segments.append(current)
            current = []
        current.append(row)
        previous = ts
    if current:
        segments.append(current)
    untimed = [row for row in group if _local_ts(row.get("started_at")) is None]
    if untimed:
        segments.append(untimed)
    return segments


def _cluster_entry(ppid: object, group: list[dict]) -> dict:
    """一段同进程的调用 → 报告里的一「簇」。"""
    stamps = sorted(ts for ts in (_local_ts(r.get("started_at")) for r in group) if ts)
    return {
        "ppid": ppid,
        "count": len(group),
        "domains": Counter(str(r.get("domain_key") or "?") for r in group),
        "operations": Counter(str(r.get("operation") or "?") for r in group),
        "targets": Counter(
            str(r.get("target") or "") for r in group if r.get("target")
        ).most_common(3),
        # 本地时间（不是原始 UTC 串）—— 渲染端 `[11:16]` 切的就是 `HH:MM`。
        "first": stamps[0].isoformat() if stamps else "",
        "last": stamps[-1].isoformat() if stamps else "",
        "error_codes": Counter(
            str(r.get("error_code"))
            for r in group
            if r.get("error_code") and r.get("cooldown_active") is not True
        ),
        "cooldown_blocked": sum(1 for r in group if r.get("cooldown_active") is True),
    }


def _unattributed_clusters(
    rows: list[dict], min_count: int = 5
) -> tuple[list[dict], list[dict]]:
    """把「未标身份」的调用按**进程号**聚成一轮一轮的批量任务。

    为什么按 ppid 聚合：`XUEQIU_CALL_SOURCE` 不设就落 `source_kind="manual"`，与「手敲一行」
    无法区分 —— 但**同一个进程连着发很多次**几乎必然是一个脚本/一个 agent 会话在批量抓。
    聚起来才能看出「有一轮 N 次、打哪个域名、关键词是什么、失败了什么」。

    同一 ppid 的调用还会按时间断裂切段（`_segments`），避免 pid 复用把两个无关进程并簇。

    ⚠️ 这个前提对「**每次调用各起一个 shell**」的模式不成立：agent 会话来一条敲一条、
    或逐条手敲时，每次 opencli 的 ppid 都不同，全都落进 `len < min_count` 的散点里，
    永远看不到。所以本函数只负责 **长驻进程** 那一半，**未达标的行原样返回**，由调用方
    再按「域名/命令」聚合一次（`_unattributed_by_domain_operation`）——两条腿合起来才覆盖得住。

    返回 `(达到 min_count 的簇, 未达标的散点行)`。
    """
    buckets: dict[object, list[dict]] = {}
    leftover: list[dict] = []
    for row in rows:
        kind = row.get("source_kind")
        if kind and kind != "manual":
            continue
        pid = row.get("ppid")
        if pid is None:  # 缺 ppid 的行不能吞掉，交给调用方按域名/命令聚合
            leftover.append(row)
            continue
        buckets.setdefault(pid, []).append(row)

    clusters: list[dict] = []
    for pid, group in buckets.items():
        for segment in _segments(group):
            if len(segment) < min_count:
                leftover.extend(segment)
            else:
                clusters.append(_cluster_entry(pid, segment))
    clusters.sort(key=lambda c: -c["count"])
    return clusters, leftover


def _unattributed_by_domain_operation(
    rows: list[dict], top: int = 4
) -> list[tuple[str, int]]:
    """把散点（未聚成进程簇的未标身份调用）按「域名/命令」聚合。

    补的正是 ppid 聚簇漏掉的那一半：**每次调用各起一个 shell** 时 ppid 每次不同，
    只能靠「打同一个域名+命令」把同一轮任务认出来。返回 `[("xiaohongshu/search", 12), …]`。

    注意返回的是 `域名/命令` 组合串，**不是**台账里的 `target` 字段（那是关键词/URL）。
    """
    counter = Counter(
        f"{r.get('domain_key') or '?'}/{r.get('operation') or '?'}" for r in rows
    )
    return counter.most_common(top)


def summarize_adapter(
    records: list[dict], cutoff: datetime, end: datetime | None = None
) -> dict:
    """插件台账摘要 —— 回答「打哪个域名、等了多久、并发几路、有没有被放行」。"""
    rows = sorted(
        (row for row in records if _in_window(row, cutoff, end)),
        key=lambda row: _parse_ts(row) or datetime.min,
    )
    clusters, singles_rows = _unattributed_clusters(rows)
    return {
        "total": len(rows),
        "first": str(rows[0].get("started_at", "")) if rows else "",
        "last": str(rows[-1].get("started_at", "")) if rows else "",
        "by_domain": Counter(str(r.get("domain_key") or "?") for r in rows),
        "by_source_kind": Counter(str(r.get("source_kind") or "?") for r in rows),
        "by_operation": Counter(str(r.get("operation") or "?") for r in rows),
        # 真实并发：观测模式下唯一能拿到它的地方（`concurrency` 未设时闸门只记录不拦）。
        "inflight": Counter(
            r["inflight_at_acquire"]
            for r in rows
            if isinstance(r.get("inflight_at_acquire"), int)
        ),
        # 用行内的 `concurrency_enforced`（闸门真正生效与否），而不是「配了 max」——
        # 配了 max 但 `concurrency_enabled: false` 时仍是观测态，只看 max 会误报「已生效」。
        # 老行没有这个字段时退回按 max 判断。
        "enforced": any(
            r.get("concurrency_enforced")
            if r.get("concurrency_enforced") is not None
            else r.get("concurrency_max")
            for r in rows
        ),
        "rate_waited_ms": sum(int(r.get("rate_waited_ms") or 0) for r in rows),
        "long_pause_hits": sum(1 for r in rows if (r.get("long_pause_ms") or 0) > 0),
        "skipped": Counter(
            str(r.get("skip_reason")) for r in rows if r.get("skip_reason")
        ),
        # 下面几个都是「本该限速/限并发，但被放行了」的计数 —— 分别对应不同原因，
        # 混在一起看会读错（基建故障 ≠ 容量信号）。
        "fail_open": sum(1 for r in rows if r.get("rate_fail_open")),
        "rate_errors": sum(1 for r in rows if r.get("rate_error")),
        "overflowed": sum(1 for r in rows if r.get("overflowed")),
        "gate_fail_open": sum(1 for r in rows if r.get("gate_fail_open")),
        # 被**熔断器**拦下的调用（`cooldown_active`）**没有发出任何请求** —— 它在
        # routing 阶段就硬失败了。所以它既不算失败、也不该计进站点请求量，否则熔断器
        # 一生效就把自己的成绩算成「站点风控恶化」，把判断带反。
        # （本口径只覆盖插件台账：它有显式的 `cooldown_active`；Python 台账没有这个
        #  标记，只能靠错误文本，容易与「daemon 真的挂了」混同，故不动那边。）
        # 判 `is True` 而不是取真值：与下面 `ok is False` 同一条纪律，别让字符串
        # 形态的假值（"false"）被当成"被拦"。
        "cooldown_blocked": sum(1 for r in rows if r.get("cooldown_active") is True),
        # `ok` 缺失（例如超长行被降级截断成 {schema, ts, layer, truncated}）不等于失败，
        # 显式判 `is False`，否则会把截断的成功行算成失败、还带不出错误码。
        "failed": sum(
            1
            for r in rows
            if r.get("ok") is False and r.get("cooldown_active") is not True
        ),
        # `error_codes` 是给上面的 `failed` 做明细的 —— 必须用**同一判据**切，
        # 否则明细合计会大于 failed，同一屏里自相矛盾。
        "error_codes": Counter(
            str(r.get("error_code"))
            for r in rows
            if r.get("error_code") and r.get("cooldown_active") is not True
        ),
        "cooldown_error_codes": Counter(
            str(r.get("error_code"))
            for r in rows
            if r.get("error_code") and r.get("cooldown_active") is True
        ),
        # `web` 调用没能解析出目标域名 = 掉到弱档（6–8s、无长停）。这是「被 WAF 盯的
        # 详情页静默失去保护」的信号，必须能一眼看到。
        # 「管理外流量」：未标身份的调用，先按进程聚类（长驻进程），散点再按域名/命令聚合
        # （见 _unattributed_clusters / _unattributed_by_target 的 docstring）
        "unattributed": clusters,
        "unattributed_singles": len(singles_rows),
        "unattributed_singles_by_domain_operation": _unattributed_by_domain_operation(
            singles_rows
        ),
        "unrouted_web": sum(
            1
            for r in rows
            if r.get("site") == "web"
            and r.get("domain_via") in ("site-key", "url-unparseable")
        ),
    }


def uncovered_operations(
    records: list[dict], cutoff: datetime, end: datetime | None = None
) -> Counter:
    """从 **Python 台账** 里数出插件治理覆盖不到的操作（`browser:*`）。"""
    counter: Counter = Counter()
    for row in records:
        if not _in_window(row, cutoff, end):
            continue
        operation = str(row.get("operation") or "?")
        if operation.startswith(UNCOVERED_OPERATION_PREFIXES):
            counter[operation] += 1
    return counter


def _render_unattributed(adapter: dict, lines: list[str]) -> None:
    """「管理外流量」那一段：没有未标身份的调用就什么都不做。

    两条腿都用上 —— 长驻进程看的是一簇 N 次，散点看的是「域名/命令」的分布
    （见 `_unattributed_clusters` / `_unattributed_by_domain_operation`）。
    """
    clusters = adapter.get("unattributed") or []
    singles = adapter.get("unattributed_singles") or 0
    if not clusters and not singles:
        return
    lines.append("")
    lines.append("  —— 管理外流量（未标身份的调用）——")
    for c in clusters:
        # first/last 已在 _unattributed_clusters 里归一成本地时间；空则整段省掉
        span = f"{c['first'][11:16]}–{c['last'][11:16]}" if c["first"] else ""
        domains = "  ".join(f"{k} {v}" for k, v in c["domains"].most_common())
        ops = "  ".join(f"{k} {v}" for k, v in c["operations"].most_common(4))
        tail = f"，{span}" if span else ""
        lines.append(f"  ⚠️ 进程 {c['ppid']}：{c['count']} 次，{domains}{tail}")
        lines.append(f"     command: {ops}")
        if c["targets"]:
            lines.append(
                "     关键词 top: " + " / ".join(str(t) for t, _ in c["targets"])
            )
        if c["error_codes"]:
            lines.append(
                "     失败: "
                + "，".join(f"{k} {v}" for k, v in c["error_codes"].most_common())
            )
        if c.get("cooldown_blocked"):
            lines.append(
                f"     另有 {c['cooldown_blocked']} 次被熔断器拦下（没发请求，不计失败）"
            )
    if singles:
        # 每次调用各起一个 shell 时 ppid 每次不同 → 聚不成进程簇。按域名/命令再聚一次，
        # 否则一条批量任务会整片消失在「< 5 次」里。
        by_domain_op = adapter.get("unattributed_singles_by_domain_operation") or []
        detail = "，".join(f"{k} {v}" for k, v in by_domain_op)
        lines.append(
            f"  · 另有 {singles} 次未聚成进程簇（每次调用各起一个 shell 时就会这样）"
        )
        if detail:
            lines.append(f"     按域名/命令聚合: {detail}")
    lines.append(
        "  → 这些调用没设 XUEQIU_CALL_SOURCE，来源（脚本 / agent 会话 / 手敲）无法区分"
    )


def render_adapter(adapter: dict, uncovered: Counter) -> str:
    """插件层观测块。没有数据时返回一句说明，而不是整块省略（沉默会被误读成「没问题」）。"""
    lines = ["", "—— 插件层观测（adapter-calls.jsonl）——"]
    if not adapter.get("total"):
        lines.append(
            "  这个时间窗内没有记录（插件侧台账是否被 OPENCLI_ADAPTER_LOG 关掉了？）"
        )
    else:
        domains = "  ".join(
            f"{name} {count}" for name, count in adapter["by_domain"].most_common(6)
        )
        lines.append(f"  按域名: {domains}")
        lines.append(
            "  按来源: "
            + "  ".join(f"{k} {v}" for k, v in adapter["by_source_kind"].most_common(6))
        )
        if adapter["inflight"]:
            gate = "已生效" if adapter["enforced"] else "观测模式（只记录不拦）"
            dist = "  ".join(
                f"{n}路×{c}" for n, c in sorted(adapter["inflight"].items())
            )
            lines.append(f"  真实并发（同时在飞）: {dist}   闸门={gate}")
        lines.append(
            f"  限频: 累计等待 {adapter['rate_waited_ms'] / 1000:.1f}s，"
            f"长停命中 {adapter['long_pause_hits']} 次"
        )
        if adapter["skipped"]:
            lines.append(
                "  按原因跳过: "
                + "  ".join(f"{k} {v}" for k, v in adapter["skipped"].most_common())
            )
        released = (
            f"限速器异常 {adapter['rate_errors']}，"
            f"取锁放行 {adapter['fail_open']}，"
            f"排队超预算 {adapter['overflowed']}，"
            f"闸门故障 {adapter['gate_fail_open']}"
        )
        lines.append(f"  ⚠️ 本该限流却被放行: {released}")
        if adapter["unrouted_web"]:
            lines.append(
                f"  ⚠️ {adapter['unrouted_web']} 次 web 调用解析不出目标域名"
                "（掉到 6–8s 弱档、无长停）—— 查参数里 URL 的形态是否变了"
            )
        if adapter["failed"]:
            codes = "  ".join(
                f"{k} {v}" for k, v in adapter["error_codes"].most_common(4)
            )
            lines.append(f"  失败 {adapter['failed']} 次（{codes or '无错误码'}）")
        if adapter.get("cooldown_blocked"):
            blocked_codes = "  ".join(
                f"{k} {v}"
                for k, v in (
                    adapter.get("cooldown_error_codes") or Counter()
                ).most_common(3)
            )
            lines.append(
                f"  ⛔ 另有 {adapter['cooldown_blocked']} 次被**熔断器**拦下"
                "（routing 阶段就失败、一个请求都没发）"
                f"（{blocked_codes or '无错误码'}）"
                " —— **不计入「失败」那行**；但仍计入按域名/来源/命令的调用数"
            )

    _render_unattributed(adapter, lines)

    if uncovered:
        lines.append(
            "  ⚠️ 插件覆盖不到（不经钩子、不受限频与并发治理）: "
            + "  ".join(f"{k} {v}" for k, v in uncovered.most_common(6))
        )
        lines.append("     这些调用**不计入**上面的域名密度（上游 #2606）")
    return "\n".join(lines)


def render(summary: dict, top_minutes: int = 8, sample_failures: int = 8) -> str:
    lines: list[str] = []
    total = summary["total"]
    if not total:
        return (
            "台账里这个时间窗内没有记录（检查 ~/.opencli/xueqiu-calls.jsonl 是否存在）"
        )

    site_total = summary.get("site_total", 0)
    local_total = summary.get("local_total", 0)
    site_peak_minute, site_peak_count = _peak(summary.get("by_minute_site", {}))

    lines.append(
        f"总调用 {total} 次，失败 {summary['failed']} 次（{_pct(summary['failed'], total)}）"
    )
    lines.append(
        f"  其中站点请求 {site_total} 次，本地命令 {local_total} 次"
        f"（只有站点请求会触发风控，判定以站点口径为准）"
    )
    if site_peak_count:
        lines.append(
            f"  站点请求分钟峰值 {site_peak_count} 次/分（{site_peak_minute}）"
        )
    probe_misses = summary.get("probe_misses", [])
    if probe_misses:
        lines.append(
            f"  另有探针未命中 {len(probe_misses)} 次（调用方声明的预期结果，不计入失败）"
        )
    if summary.get("cooldown_blocked"):
        lines.append(
            f"  ⛔ 其中 {summary['cooldown_blocked']} 次被**熔断器**拦下"
            "（routing 阶段就失败、一个请求都没发）—— 不计入站点请求与失败"
        )
    lines.append(f"时间范围 {summary['first']} → {summary['last']}")
    lines.append("")

    lines.append("== 按小时（调用数 / 失败数）==")
    peak = max(summary["by_hour"].values()) if summary["by_hour"] else 0
    for hour in sorted(summary["by_hour"]):
        count = summary["by_hour"][hour]
        failed = summary["by_hour_fail"][hour]
        mark = f"  ✗{failed}" if failed else ""
        lines.append(f"  {hour}  {count:>4}{mark:<6} {_bar(count, peak)}")
    lines.append("")

    lines.append("== 按来源 ==")
    for source, count in summary["by_source"].most_common():
        failed = summary["by_source_fail"][source]
        lines.append(f"  {count:>5}  {_pct(failed, count):>7} 失败  {source}")
    lines.append("")

    lines.append("== 按操作 ==")
    for operation, count in summary["by_operation"].most_common():
        failed = summary["by_operation_fail"][operation]
        lines.append(f"  {count:>5}  {_pct(failed, count):>7} 失败  {operation}")
    lines.append("")

    # 分钟峰值 —— 判定「访问频率异常」最直接的证据。
    # 只有**站点请求**才该拿来和风控阈值比；本地命令（browser get/extract/close）
    # 混进来会把峰值抬高一档，见文件头 SITE_OPERATIONS 的注释。
    lines.append(f"== 站点请求分钟峰值 Top {top_minutes}（决定风控的那一列）==")
    for minute, count in summary.get("by_minute_site", Counter()).most_common(
        top_minutes
    ):
        lines.append(f"  {minute}  {count:>3} 次")
    lines.append("")

    lines.append(f"== 本地命令分钟峰值 Top {top_minutes}（不触发风控，仅供排查耗时）==")
    for minute, count in summary.get("by_minute_local", Counter()).most_common(
        top_minutes
    ):
        lines.append(f"  {minute}  {count:>3} 次")
    lines.append("")

    lines.append(
        f"== 全部调用分钟峰值 Top {top_minutes}（站点+本地，不等于风控压力）=="
    )
    for minute, count in summary["by_minute"].most_common(top_minutes):
        lines.append(f"  {minute}  {count:>3} 次")
    lines.append("")

    if summary["failures"]:
        lines.append(
            f"== 失败样本（共 {len(summary['failures'])} 条，展示最近 {sample_failures} 条）=="
        )
        for row in summary["failures"][-sample_failures:]:
            error = str(row.get("error") or "").replace("\n", " ")[:110]
            lines.append(
                f"  {row.get('ts', '')}  {row.get('source', '?')}  {row.get('operation', '?')}"
                f"  rc={row.get('returncode')}  {error}"
            )
        lines.append("")

    if probe_misses:
        # 单列，是为了让人一眼看出「这些不是故障」，而不是去追一个不存在的 bug
        by_op = Counter(str(r.get("operation") or "?") for r in probe_misses)
        detail = "、".join(f"{op} {n} 次" for op, n in by_op.most_common())
        lines.append(f"== 探针未命中 {len(probe_misses)} 次（预期控制流，非失败）==")
        lines.append(f"  {detail}")
        lines.append(
            "  这些调用带 expect_miss 标记：调用方按顺序试多个候选，"
            "不中即换下一个，属正常流程。"
        )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", help="台账路径，默认 ~/.opencli/xueqiu-calls.jsonl")
    parser.add_argument("--hours", type=int, default=24, help="回看小时数，默认 24")
    parser.add_argument("--date", help="只统计某天 YYYY-MM-DD（覆盖 --hours）")
    parser.add_argument(
        "--adapter-log",
        help="插件侧台账路径，默认 ~/.opencli/adapter-calls.jsonl；off 关闭",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    path = _log_path(args.log)
    if not path.exists():
        print(f"找不到台账：{path}")
        print(
            "（台账号在首次调用 opencli 后生成；若从未生成，检查 XUEQIU_OPENCLI_LOG 是否被设为 off）"
        )
        return 1

    if args.date:
        try:
            start = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"--date 格式错误：{args.date}（应为 YYYY-MM-DD）")
            return 2
        cutoff = start
    else:
        cutoff = datetime.now() - timedelta(hours=args.hours)

    records = load_records(path)
    summary = summarize(records, cutoff)

    window_end = cutoff + timedelta(days=1) if args.date else None
    adapter_path = _adapter_log_path(args.adapter_log)
    adapter = (
        summarize_adapter(load_records(adapter_path), cutoff, window_end)
        if adapter_path and adapter_path.exists()
        else {"total": 0}
    )
    uncovered = uncovered_operations(records, cutoff, window_end)

    if args.date:
        # 只保留当天
        end = cutoff + timedelta(days=1)

        def _in_window(key: str, fmt: str) -> bool:
            return cutoff <= datetime.strptime(key, fmt) < end

        for key in (
            "by_hour",
            "by_hour_fail",
            "by_minute",
            "by_minute_site",
            "by_minute_local",
        ):
            summary[key] = Counter(
                {
                    k: v
                    for k, v in summary[key].items()
                    if _in_window(k, "%Y-%m-%d %H:%M")
                }
                if key.startswith("by_minute")
                else {
                    k: v
                    for k, v in summary[key].items()
                    if _in_window(k, "%Y-%m-%d %H:00")
                }
            )
        summary["total"] = sum(summary["by_hour"].values())
        summary["failed"] = sum(summary["by_hour_fail"].values())
        summary["site_total"] = sum(summary["by_minute_site"].values())
        summary["local_total"] = sum(summary["by_minute_local"].values())
        summary["failures"] = [
            r
            for r in summary["failures"]
            if (ts := _parse_ts(r)) and ts >= cutoff and ts < end
        ]
        summary["probe_misses"] = [
            r
            for r in summary.get("probe_misses", [])
            if (ts := _parse_ts(r)) and ts >= cutoff and ts < end
        ]
        summary["site_failed"] = sum(
            1
            for r in summary["failures"]
            if str(r.get("operation") or "?") in SITE_OPERATIONS
        )

    if args.json:
        site_minute, site_count = _peak(summary.get("by_minute_site", {}))
        local_minute, local_count = _peak(summary.get("by_minute_local", {}))
        print(
            json.dumps(
                {
                    "total": summary["total"],
                    "failed": summary["failed"],
                    "site_total": summary.get("site_total", 0),
                    "local_total": summary.get("local_total", 0),
                    "site_failed": summary.get("site_failed", 0),
                    "probe_misses": len(summary.get("probe_misses", [])),
                    "site_peak": {"minute": site_minute, "count": site_count},
                    "local_peak": {"minute": local_minute, "count": local_count},
                    "by_hour": dict(summary["by_hour"]),
                    "by_source": dict(summary["by_source"]),
                    "by_operation": dict(summary["by_operation"]),
                    "peak_minutes_site": dict(
                        summary.get("by_minute_site", Counter()).most_common(20)
                    ),
                    "peak_minutes_all": dict(summary["by_minute"].most_common(20)),
                    # 插件侧台账（传输层真相）。键是**新增**的，老消费者不受影响。
                    "adapter_total": adapter.get("total", 0),
                    "adapter_cooldown_blocked": adapter.get("cooldown_blocked", 0),
                    "adapter_first": adapter.get("first", ""),
                    "adapter_last": adapter.get("last", ""),
                    "adapter_by_operation": dict(
                        adapter.get("by_operation", Counter())
                    ),
                    "adapter_by_domain": dict(adapter.get("by_domain", Counter())),
                    "adapter_by_source_kind": dict(
                        adapter.get("by_source_kind", Counter())
                    ),
                    "adapter_inflight": {
                        str(k): v for k, v in adapter.get("inflight", Counter()).items()
                    },
                    "adapter_concurrency_enforced": bool(adapter.get("enforced")),
                    "adapter_skipped": dict(adapter.get("skipped", Counter())),
                    "adapter_released": {
                        "rate_errors": adapter.get("rate_errors", 0),
                        "fail_open": adapter.get("fail_open", 0),
                        "overflowed": adapter.get("overflowed", 0),
                        "gate_fail_open": adapter.get("gate_fail_open", 0),
                    },
                    "adapter_unrouted_web": adapter.get("unrouted_web", 0),
                    "adapter_unattributed": [
                        {
                            "ppid": c["ppid"],
                            "count": c["count"],
                            "domains": dict(c["domains"]),
                            "operations": dict(c["operations"]),
                            "targets": c["targets"],
                            "first": c["first"],
                            "last": c["last"],
                            "error_codes": dict(c["error_codes"]),
                        }
                        for c in adapter.get("unattributed", [])
                    ],
                    "adapter_unattributed_singles": adapter.get(
                        "unattributed_singles", 0
                    ),
                    "adapter_unattributed_singles_by_domain_operation": [
                        {"domain_operation": k, "count": v}
                        for k, v in (
                            adapter.get("unattributed_singles_by_domain_operation")
                            or []
                        )
                    ],
                    # 不经钩子、不受插件治理的调用（`browser:*`）—— 不计入域名密度
                    "uncovered_operations": dict(uncovered),
                },
                ensure_ascii=False,
                indent=1,
            )
        )
        return 0

    print(render(summary))
    print(render_adapter(adapter, uncovered))
    return 0


if __name__ == "__main__":
    sys.exit(main())
