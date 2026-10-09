#!/usr/bin/env python3
"""对比 shadow 模式下 legacy 与 gateway 两侧的爬取产物。

用法:
    python3 scripts/compare_crawl_outputs.py \
        --legacy-dir data --gateway-dir data-gateway \
        --date 2026-10-07 --out logs/shadow_compare_2026-10-07.json

退出码:
    0 = 无差异
    1 = 有差异（供 cron 告警）
    2 = 用法或读取错误（例如两侧都没有 index.json，说明影子目录没接上）

对比口径（重要）:
    两侧的 index.json 都是**累计**索引，直接比全量集合会把历史差异也算进来。
    因此「当天新增文章」一律取自 history/<user>/<date>.json —— 两侧都只在真的
    存下新文章时才写这个文件，语义等价于「该用户当天各侧新保存了什么」。

    同时校验基线是否一致：基线 = index 扣掉「两侧当天新增 id 的并集」。基线不一致
    说明 data-gateway/ 没有和 data/ 对齐过，此时两侧「新文章」口径根本不可比，
    其余对比结论都不可信，会单独报一条差异。

    注意运行前提：data-gateway/ 首次使用前必须与 data/ 对齐已知状态（run_daily.sh
    的 shadow 分支会自动做）。影子目录为空时 gateway 会把窗口内所有文章都当成
    新文章，上面这条基线校验就是用来兜住这种情况的。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

# 参与逐字段对比的字段。刻意排除 crawl_time / filepath / file_path：
# 它们只反映「什么时候爬的」「文件落在哪」，两侧不可能相同，比了全是噪音。
COMPARED_FIELDS = ("title", "author", "publish_time")
# .last_crawl_stats.json 的 5 个兼容字段
STATS_FIELDS = ("date", "total_users", "successful", "failed", "new_articles")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def stats_snapshot(data_dir: Path) -> dict:
    payload = load_json(data_dir / ".last_crawl_stats.json")
    return {field: payload.get(field) for field in STATS_FIELDS}


def index_records(data_dir: Path) -> dict:
    return load_json(data_dir / "index.json").get("articles", {}) or {}


def daily_article_ids(data_dir: Path, day: str) -> dict[str, set[str]]:
    """各用户当天新保存的文章 id（来自 history/<user>/<day>.json）。"""
    result: dict[str, set[str]] = {}
    history_dir = data_dir / "history"
    if not history_dir.is_dir():
        return result
    for user_dir in sorted(history_dir.iterdir()):
        if not user_dir.is_dir():
            continue
        payload = load_json(user_dir / f"{day}.json")
        result[user_dir.name] = {
            str(article.get("article_id"))
            for article in payload.get("articles", [])
            if article.get("article_id")
        }
    return result


def compare_stats(legacy_dir: Path, gateway_dir: Path) -> tuple[dict, list[str]]:
    legacy = stats_snapshot(legacy_dir)
    gateway = stats_snapshot(gateway_dir)
    diff = {
        field: [legacy[field], gateway[field]]
        for field in STATS_FIELDS
        if legacy[field] != gateway[field]
    }
    differences = [
        f"统计字段 {field} 不一致: legacy={pair[0]!r} gateway={pair[1]!r}"
        for field, pair in diff.items()
    ]
    return {"legacy": legacy, "gateway": gateway, "diff": diff}, differences


def compare_articles(
    legacy_dir: Path,
    gateway_dir: Path,
    day: str,
) -> tuple[dict, list[str]]:
    legacy_records = index_records(legacy_dir)
    gateway_records = index_records(gateway_dir)
    legacy_new = daily_article_ids(legacy_dir, day)
    gateway_new = daily_article_ids(gateway_dir, day)

    only_in_legacy: list[dict] = []
    only_in_gateway: list[dict] = []
    field_mismatches: list[dict] = []
    differences: list[str] = []

    seen_users = set(legacy_new) | set(gateway_new)
    for user_id in sorted(seen_users):
        legacy_ids = legacy_new.get(user_id, set())
        gateway_ids = gateway_new.get(user_id, set())
        for article_id in sorted(legacy_ids - gateway_ids):
            only_in_legacy.append({"user_id": user_id, "article_id": article_id})
        for article_id in sorted(gateway_ids - legacy_ids):
            only_in_gateway.append({"user_id": user_id, "article_id": article_id})
        for article_id in sorted(legacy_ids & gateway_ids):
            key = f"{user_id}_{article_id}"
            legacy_record = legacy_records.get(key, {})
            gateway_record = gateway_records.get(key, {})
            for field in COMPARED_FIELDS:
                if legacy_record.get(field) != gateway_record.get(field):
                    field_mismatches.append(
                        {
                            "user_id": user_id,
                            "article_id": article_id,
                            "field": field,
                            "legacy": legacy_record.get(field),
                            "gateway": gateway_record.get(field),
                        }
                    )

    if only_in_legacy:
        differences.append(f"仅 legacy 抓到 {len(only_in_legacy)} 篇当天文章")
    if only_in_gateway:
        differences.append(f"仅 gateway 抓到 {len(only_in_gateway)} 篇当天文章")
    if field_mismatches:
        differences.append(f"{len(field_mismatches)} 处文章字段不一致")

    # 基线校验：index 扣掉「两侧当天新增的并集」后应完全一致，否则影子目录未与
    # data/ 对齐。必须用并集而不是各侧自己的当天集合——否则一侧只是漏记了当天的
    # 新文章时，那篇会被误算进基线，报出「未对齐」这种与事实不符的诊断。
    today_keys = {
        f"{user_id}_{article_id}"
        for user_id, ids in list(legacy_new.items()) + list(gateway_new.items())
        for article_id in ids
    }
    baseline_legacy = set(legacy_records) - today_keys
    baseline_gateway = set(gateway_records) - today_keys
    baseline_only_legacy = sorted(baseline_legacy - baseline_gateway)
    baseline_only_gateway = sorted(baseline_gateway - baseline_legacy)
    if baseline_only_legacy or baseline_only_gateway:
        differences.append(
            "基线不一致（影子目录未与 data/ 对齐）: "
            f"仅 legacy {len(baseline_only_legacy)} 条, 仅 gateway {len(baseline_only_gateway)} 条"
        )

    return (
        {
            "legacy_index_total": len(legacy_records),
            "gateway_index_total": len(gateway_records),
            "legacy_new_total": sum(len(ids) for ids in legacy_new.values()),
            "gateway_new_total": sum(len(ids) for ids in gateway_new.values()),
            "only_in_legacy": only_in_legacy,
            "only_in_gateway": only_in_gateway,
            "field_mismatches": field_mismatches,
            "baseline_only_in_legacy": baseline_only_legacy[:20],
            "baseline_only_in_gateway": baseline_only_gateway[:20],
        },
        differences,
    )


def gateway_attempt_statuses(db_path: Path, day: str | None = None) -> dict[str, int]:
    """影子库里的 attempt 状态分布，用于回答「为什么没数据」。不影响判定。

    `day` 是必需的实参语义：给了就**只统计那一天的 job**。原来不过滤日期，
    于是把影子库里**历史所有运行**累加在一起 —— 2026-10-09 实测今早那轮
    21 次尝试被报成 57 次，还混进前一天的 8 条 `circuit_open`，直接读出
    「今早熔断跳了 8 个账号」这种不存在的因果关系。
    """
    if not db_path.exists():
        return {}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            if day:
                rows = connection.execute(
                    """
                    SELECT a.status, COUNT(*)
                    FROM attempts a
                    JOIN tasks t ON t.id = a.task_id
                    JOIN jobs j ON j.id = t.job_id
                    WHERE date(j.started_at, 'unixepoch', 'localtime') = ?
                    GROUP BY a.status
                    """,
                    (day,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT status, COUNT(*) FROM attempts GROUP BY status"
                ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(status): int(count) for status, count in rows}


def build_report(
    *,
    legacy_dir: Path,
    gateway_dir: Path,
    day: str,
    gateway_db: Path | None = None,
) -> dict:
    stats, stats_differences = compare_stats(legacy_dir, gateway_dir)
    articles, article_differences = compare_articles(legacy_dir, gateway_dir, day)
    differences = stats_differences + article_differences

    # 两侧当天都没新增时，「无差异」是空话：没有任何一篇文章被真正比对过。
    # 这不该判为失败（安静的日子本来就该是 0），但必须显式说出来 ——
    # 否则 ok=true 会被误当成「这一天验证过了」。
    # 2026-10-08 正是因为没这层提示，一次「完美一致」掩盖了「gateway 拿到的
    # 是被拷进去的标准答案、根本没验证抓取」这一事实。
    warnings: list[str] = []
    if not articles["legacy_new_total"] and not articles["gateway_new_total"]:
        warnings.append(
            "两侧当天均无新增文章，本次对比未覆盖「新文章」路径，不能算一次有效验证"
        )

    failures = {
        "legacy_failed": stats["legacy"]["failed"],
        "gateway_failed": stats["gateway"]["failed"],
    }
    if gateway_db is not None:
        failures["gateway_attempt_statuses"] = gateway_attempt_statuses(gateway_db, day)

    return {
        "date": day,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "legacy_dir": str(legacy_dir),
        "gateway_dir": str(gateway_dir),
        "ok": not differences,
        "differences": differences,
        "warnings": warnings,
        "stats": stats,
        "articles": articles,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-dir", default="data")
    parser.add_argument("--gateway-dir", default="data-gateway")
    parser.add_argument("--gateway-db", default=None)
    parser.add_argument("--date", default=None, help="对比日期，默认今天")
    parser.add_argument("--out", default=None, help="报告输出路径（JSON）")
    args = parser.parse_args(argv)

    legacy_dir = Path(args.legacy_dir)
    gateway_dir = Path(args.gateway_dir)
    day = args.date or date_cls.today().isoformat()

    if (
        not (legacy_dir / "index.json").exists()
        and not (gateway_dir / "index.json").exists()
    ):
        print(
            f"两侧都没有 index.json（legacy={legacy_dir} gateway={gateway_dir}），"
            "影子目录没有接上，无法对比",
            file=sys.stderr,
        )
        return 2

    gateway_db = Path(args.gateway_db) if args.gateway_db else None
    report = build_report(
        legacy_dir=legacy_dir,
        gateway_dir=gateway_dir,
        day=day,
        gateway_db=gateway_db,
    )

    out_path = (
        Path(args.out) if args.out else Path("logs") / f"shadow_compare_{day}.json"
    )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        print(f"写报告失败: {out_path} ({exc})", file=sys.stderr)
        return 2

    # 警告走 stderr：stdout 保持是可解析的 JSON（调用方可能直接吃它），
    # 而 run_daily.sh 把两者都重定向进日志，所以警告不会被丢掉。
    for warning in report["warnings"]:
        print(f"WARN: {warning}", file=sys.stderr)

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
