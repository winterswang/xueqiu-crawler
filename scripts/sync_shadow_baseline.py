#!/usr/bin/env python3
"""把 legacy 侧「今天之前」的已知状态对齐到影子目录。

用法:
    python3 scripts/sync_shadow_baseline.py \
        --legacy-dir data --shadow-dir data-gateway [--date 2026-10-13] [--dry-run]

退出码:
    0 = 完成（含「无变更」）
    2 = 用法/读取错误（例如 legacy 侧没有 index.json —— 此时**不能**继续，
        静默放行等于让影子带着错误基线去抓）

为什么需要它（2026-10-13 实测）:
    影子（`crawl_gateway` 写 `data-gateway/`）与生产（legacy 写 `data/`）必须从
    **同一个「今天之前」的起点**各自去抓今天的新文，产物才可比。原实现只在影子
    目录不存在时镜像一次 `data/`（`run_daily.sh` 的 shadow 分支），于是**每被风控
    墙挡掉一天，影子就永久欠一截账**：那些文章在 `data/index.json` 里有、在
    `data-gateway/` 里没有 → 影子把**旧文**当成今天的新文重抓。

    代价是可量化的：当天影子抓了 62 次详情页、生产只抓 46 次（+25%），多出来的
    正是 8 篇 10-08~10-11 的旧文；对比里还会出现**假的 `only_in_gateway`**。
    更糟的是它自我放大 —— 请求越多越容易撞墙，撞墙越多欠账越大，而墙本身有
    「约 80 次请求/滚动小时」的额度（见 `xueqiu-waf-wall-cooldown` 那条结论）。

口径（别改错这一条）:
    **只对齐 `crawl_time` 的日期 < 今天的条目**。今天的一律不碰：两侧必须各自
    独立发现今天的新文，否则就是「拿标准答案改卷」，对比退化成自证一致。
    判据是索引里的 `crawl_time`（797/797 条都有值），**不是文件 mtime** ——
    mtime 会被复制动作改写。

做什么:
    1. 缺失的 `<uid>/<article_id>.md` 复制进影子目录（`.md` 本身就是
       `ArticleStore.known_article_ids` 的已知集合来源之一，见
       `crawl_gateway/adapters/xueqiu.py`）
    2. 对应条目并入影子 `index.json`（**只补不覆盖**，已存在的一律保留）
    3. 缺失的 `history/<uid>/<今天之前>.json` 一并复制（同样是已知集合来源）

幂等：跑第二遍不产生任何变更（日志会明确报「已存在」）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path


@dataclass
class SyncReport:
    """一次同步的结果，逐项计数（给日志和测试用）。"""

    copied_articles: int = 0
    already_present: int = 0
    missing_files: list[str] = field(default_factory=list)  # 索引里有、正文本地不存在
    index_added: int = 0
    index_kept: int = 0
    copied_history: int = 0
    history_skipped: int = 0
    today_entries: int = 0
    undecidable: list[str] = field(
        default_factory=list
    )  # crawl_time 缺失/无法解析 → 不敢动

    def summary_line(self) -> str:
        parts = [
            f"补齐 {self.copied_articles} 篇正文",
            f"索引新增 {self.index_added} 条",
            f"history 新增 {self.copied_history} 个",
            f"已存在 {self.already_present} 篇",
            f"今日条目 {self.today_entries} 篇不动",
        ]
        if self.missing_files:
            parts.append(f"⚠️ 索引有但正文缺失 {len(self.missing_files)} 篇")
        if self.undecidable:
            parts.append(f"⚠️ crawl_time 无法判定 {len(self.undecidable)} 条（已跳过）")
        return "[baseline] " + "；".join(parts)


def load_index(path: Path) -> dict:
    """读 index.json；不存在或损坏时抛 ValueError（调用方转成退出码 2）。"""
    if not path.exists():
        raise ValueError(f"索引不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"索引读取失败: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"索引结构不是对象: {path}")
    data.setdefault("articles", {})
    return data


def crawl_date(record: dict) -> str | None:
    """取条目 `crawl_time` 的日期部分；取不到返回 None（调用方会保守跳过）。"""
    raw = str(record.get("crawl_time") or "")
    return raw[:10] if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-" else None


def is_before_today(record: dict, today: str) -> bool | None:
    """`True` = 今天之前；`False` = 今天（含今天之后的脏数据）；`None` = 无法判定。"""
    crawl_day = crawl_date(record)
    if crawl_day is None:
        return None
    return crawl_day < today


def write_index_atomic(path: Path, data: dict) -> None:
    """tmp + os.replace 原子替换（与 crawler/adapters 的落盘约定一致）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def sync_baseline(
    *,
    legacy_dir: Path,
    shadow_dir: Path,
    today: str,
    dry_run: bool = False,
) -> SyncReport:
    """把 legacy 里「今天之前」的已知状态对齐到影子目录。"""
    report = SyncReport()
    legacy_index = load_index(legacy_dir / "index.json")
    shadow_index_path = shadow_dir / "index.json"
    # 影子目录可能还不存在（首次接入）：建出来，索引按空对象起步 —— 首次运行
    # 就等价于原先那次「整体镜像」，但已经带上了「今日不碰」的过滤。
    shadow_index = (
        load_index(shadow_index_path)
        if shadow_index_path.exists()
        else {"articles": {}, "last_update": None}
    )

    for key, record in legacy_index["articles"].items():
        if not isinstance(record, dict):
            report.undecidable.append(str(key))
            continue
        before = is_before_today(record, today)
        if before is None:
            report.undecidable.append(str(key))
            continue
        if not before:
            # 今天（或未来）的条目：两侧必须各自去发现，绝不镜像。
            report.today_entries += 1
            continue

        user_id = str(record.get("user_id") or "")
        article_id = str(record.get("article_id") or "")
        if not user_id or not article_id:
            report.undecidable.append(str(key))
            continue

        source_md = legacy_dir / user_id / f"{article_id}.md"
        target_md = shadow_dir / user_id / f"{article_id}.md"
        if not source_md.exists():
            # 索引有、正文没了（曾因 OOM/rebuild 丢过）：记下来，不要假装成功。
            report.missing_files.append(f"{key}")
        elif target_md.exists():
            report.already_present += 1
        else:
            if not dry_run:
                target_md.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_md, target_md)
            report.copied_articles += 1

        if key in shadow_index["articles"]:
            report.index_kept += 1
        else:
            if not dry_run:
                shadow_index["articles"][key] = record
            report.index_added += 1

    # history 是已知集合的第三个来源（`known_article_ids` 只看最近 7 个文件，
    # 但缺文件同样会让影子把旧文当新文）。只补「今天之前」的日期文件。
    legacy_history = legacy_dir / "history"
    if legacy_history.is_dir():
        for user_dir in sorted(legacy_history.iterdir()):
            if not user_dir.is_dir():
                continue
            for history_file in sorted(user_dir.glob("*.json")):
                if history_file.stem >= today:
                    report.history_skipped += 1
                    continue
                target = shadow_dir / "history" / user_dir.name / history_file.name
                if target.exists():
                    report.history_skipped += 1
                    continue
                if not dry_run:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(history_file, target)
                report.copied_history += 1

    if not dry_run and report.index_added:
        shadow_dir.mkdir(parents=True, exist_ok=True)
        write_index_atomic(shadow_index_path, shadow_index)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对齐影子目录的「今天之前」基线")
    parser.add_argument("--legacy-dir", default="data", help="生产侧目录（默认 data）")
    parser.add_argument(
        "--shadow-dir", default="data-gateway", help="影子目录（默认 data-gateway）"
    )
    parser.add_argument(
        "--date",
        default=None,
        help="把哪一天当作「今天」（YYYY-MM-DD，默认系统日期）；测试与补跑用",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只报告将要做什么，不落盘"
    )
    args = parser.parse_args(argv)

    today = args.date or date_cls.today().isoformat()
    try:
        report = sync_baseline(
            legacy_dir=Path(args.legacy_dir),
            shadow_dir=Path(args.shadow_dir),
            today=today,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"[baseline] ❌ {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(
            "[baseline] (dry-run) " + report.summary_line().replace("[baseline] ", "")
        )
    else:
        print(report.summary_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
