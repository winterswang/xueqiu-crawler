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
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_LOG = Path.home() / ".opencli" / "xueqiu-calls.jsonl"


def _log_path(explicit: str | None) -> Path:
    if explicit:
        return Path(os.path.expanduser(explicit))
    return Path(os.path.expanduser(os.environ.get("XUEQIU_OPENCLI_LOG", ""))) if os.environ.get(
        "XUEQIU_OPENCLI_LOG"
    ) else DEFAULT_LOG


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
    raw = record.get("ts") or ""
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "—"


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
    failures: list[dict] = []

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
        if not ok:
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
        "failures": failures,
    }


def render(summary: dict, top_minutes: int = 8, sample_failures: int = 8) -> str:
    lines: list[str] = []
    total = summary["total"]
    if not total:
        return "台账里这个时间窗内没有记录（检查 ~/.opencli/xueqiu-calls.jsonl 是否存在）"

    lines.append(f"总调用 {total} 次，失败 {summary['failed']} 次（{_pct(summary['failed'], total)}）")
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

    # 分钟峰值 —— 判定「访问频率异常」最直接的证据
    lines.append(f"== 分钟峰值 Top {top_minutes}（同一分钟内打了多少次）==")
    for minute, count in summary["by_minute"].most_common(top_minutes):
        lines.append(f"  {minute}  {count:>3} 次")
    lines.append("")

    if summary["failures"]:
        lines.append(f"== 失败样本（共 {len(summary['failures'])} 条，展示最近 {sample_failures} 条）==")
        for row in summary["failures"][-sample_failures:]:
            error = str(row.get("error") or "").replace("\n", " ")[:110]
            lines.append(
                f"  {row.get('ts','')}  {row.get('source','?')}  {row.get('operation','?')}"
                f"  rc={row.get('returncode')}  {error}"
            )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", help="台账路径，默认 ~/.opencli/xueqiu-calls.jsonl")
    parser.add_argument("--hours", type=int, default=24, help="回看小时数，默认 24")
    parser.add_argument("--date", help="只统计某天 YYYY-MM-DD（覆盖 --hours）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    path = _log_path(args.log)
    if not path.exists():
        print(f"找不到台账：{path}")
        print("（台账号在首次调用 opencli 后生成；若从未生成，检查 XUEQIU_OPENCLI_LOG 是否被设为 off）")
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

    if args.date:
        # 只保留当天
        end = cutoff + timedelta(days=1)
        summary["by_hour"] = Counter(
            {k: v for k, v in summary["by_hour"].items() if cutoff <= datetime.strptime(k, "%Y-%m-%d %H:00") < end}
        )
        summary["total"] = sum(summary["by_hour"].values())
        summary["failed"] = sum(
            v for k, v in summary["by_hour_fail"].items()
            if cutoff <= datetime.strptime(k, "%Y-%m-%d %H:00") < end
        )

    if args.json:
        print(json.dumps(
            {
                "total": summary["total"],
                "failed": summary["failed"],
                "by_hour": dict(summary["by_hour"]),
                "by_source": dict(summary["by_source"]),
                "by_operation": dict(summary["by_operation"]),
                "peak_minutes": dict(summary["by_minute"].most_common(20)),
            },
            ensure_ascii=False,
            indent=1,
        ))
        return 0

    print(render(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
