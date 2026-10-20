"""热榜帖补齐 raw：IMA 同步不再静默漏掉热榜入选帖（2026-10-20）。

背景：日报入选的 9 篇里 6 篇是热榜独有帖，它们的正文只在
`data/hot_posts/<date>/hot_top10.json`，没有 `data/<uid>/<post_id>.md`。
原来的 `find_selected_article_files` 只认后者，于是这 6 篇被「raw 找不到」
一句 warning 静默跳过 —— 每天知识库少 2/3 的入选内容。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sync_raw_articles_to_ima import (  # noqa: E402
    HOT_RAW_SUBDIR,
    find_selected_article_files,
    load_hot_top_items,
    materialize_hot_post_raw,
    render_hot_post_markdown,
)

DATE = "2026-10-20"

HOT_ITEM = {
    "rank": "5",
    "author": "切了个菜",
    "author_id": "7336413989",
    "status_id": "412922588",
    "url": "https://xueqiu.com/7336413989/412922588",
    "title": "股息这玩意儿，本质上是古代的地租",
    "created_at": "2026-10-19T21:31:00+08:00",
    "likes": "88",
    "comments": "18",
    "content": "来源：雪球App，作者： 切了个菜\n\n"
    + "股息这玩意儿，本质上就是古代的地租。" * 12,
    "content_source": "full",
}

DAILY_REPORT = f"""# 📊 价值投资日报

**日期**：{DATE}

### 🔴 必读

#### 1. 自选账号文章

- **作者**：逸修1
- **链接**：https://xueqiu.com/1936609590/412907698

---

#### 2. 热榜文章 {HOT_ITEM["title"]}

- **作者**：切了个菜
- **链接**：https://xueqiu.com/7336413989/412922588

---

### 🔵 参考

1. [不该入选](https://xueqiu.com/111/222)（某人）
"""


def _write_hot_doc(data_dir: Path, items: list[dict]) -> Path:
    path = data_dir / "hot_posts" / DATE / "hot_top10.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"generated_at": "2026-10-20T06:00:14+08:00", "items": items}),
        encoding="utf-8",
    )
    return path


def _write_daily(data_dir: Path, text: str = DAILY_REPORT) -> Path:
    report_dir = data_dir / "daily_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{DATE}.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_render_is_deterministic_and_carries_title_and_body():
    """内容只由条目决定：不含运行时间戳，否则每天都算「内容变了」→ 重复上传。"""
    first = render_hot_post_markdown(HOT_ITEM)
    second = render_hot_post_markdown(dict(HOT_ITEM))

    assert first == second
    assert first.startswith(f"# {HOT_ITEM['title']}\n")
    assert HOT_ITEM["content"] in first
    assert HOT_ITEM["url"] in first
    assert "7336413989" in first


def test_materialize_writes_raw_md_under_hot_dir(tmp_path):
    hot_items = {HOT_ITEM["status_id"]: HOT_ITEM}

    path = materialize_hot_post_raw(
        DATE, "7336413989", "412922588", hot_items, tmp_path / "hot_posts"
    )

    assert path is not None
    assert path.parent.name == "7336413989"
    assert path.parent.parent.name == HOT_RAW_SUBDIR
    assert path.read_text(encoding="utf-8") == render_hot_post_markdown(HOT_ITEM)


def test_materialize_keeps_mtime_when_content_unchanged(tmp_path):
    """重跑同一天不能改写文件：mtime 一变，下游会当成新文章再传一遍。"""
    hot_items = {HOT_ITEM["status_id"]: HOT_ITEM}
    hot_dir = tmp_path / "hot_posts"

    first = materialize_hot_post_raw(
        DATE, "7336413989", "412922588", hot_items, hot_dir
    )
    assert first is not None
    os.utime(first, (1_600_000_000, 1_600_000_000))
    before = first.stat().st_mtime

    second = materialize_hot_post_raw(
        DATE, "7336413989", "412922588", hot_items, hot_dir
    )

    assert second == first
    assert second.stat().st_mtime == before


def test_materialize_skips_excerpt_only_item(tmp_path):
    """只有首页摘要的条目不能当全文上传 —— 宁可算 missing 也不塞半篇进知识库。"""
    item = dict(HOT_ITEM, content_source="excerpt", content="摘要：股息是地租。")

    path = materialize_hot_post_raw(
        DATE, "7336413989", "412922588", {HOT_ITEM["status_id"]: item}, tmp_path
    )

    assert path is None
    assert not (tmp_path / DATE / HOT_RAW_SUBDIR).exists()


def test_materialize_rejects_author_id_mismatch(tmp_path):
    path = materialize_hot_post_raw(
        DATE,
        "9999999999",  # 日报里的作者 ID 与热榜条目对不上
        "412922588",
        {HOT_ITEM["status_id"]: HOT_ITEM},
        tmp_path,
    )

    assert path is None


def test_materialize_falls_back_to_url_for_missing_author_id(tmp_path):
    """热榜条目的 author_id 可能是空的（2026-10-20 的 412809907 就是），
    这时按 url 里的 uid 判定，不要因此丢掉一篇能补的全文。"""
    item = dict(HOT_ITEM, author_id="")

    path = materialize_hot_post_raw(
        DATE, "7336413989", "412922588", {HOT_ITEM["status_id"]: item}, tmp_path
    )

    assert path is not None
    assert path.exists()


def test_load_hot_top_items_tolerates_missing_and_broken_file(tmp_path):
    assert load_hot_top_items(DATE, tmp_path) == {}

    broken = tmp_path / DATE / "hot_top10.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json", encoding="utf-8")

    assert load_hot_top_items(DATE, tmp_path) == {}


def test_find_selected_article_files_fills_hot_post(tmp_path):
    """今天这个真实故障的回归：热榜入选帖必须被找到，而不是只留一句 warning。"""
    _write_daily(tmp_path)
    _write_hot_doc(tmp_path, [HOT_ITEM])
    watchlist_dir = tmp_path / "1936609590"
    watchlist_dir.mkdir()
    (watchlist_dir / "412907698.md").write_text(
        "# 自选账号文章\n\n> 作者：逸修1\n\n正文\n" * 20, encoding="utf-8"
    )

    paths, total, missing, hot_filled = find_selected_article_files(DATE, tmp_path)

    assert total == 2
    assert missing == 0
    assert hot_filled == 1
    assert {p.parent.name for p in paths} == {"1936609590", "7336413989"}


def test_find_selected_article_files_counts_unresolvable_as_missing(tmp_path):
    """热榜文件缺失时保持原行为：算 missing，不崩。"""
    _write_daily(tmp_path)
    watchlist_dir = tmp_path / "1936609590"
    watchlist_dir.mkdir()
    (watchlist_dir / "412907698.md").write_text("# 自选\n", encoding="utf-8")

    paths, total, missing, hot_filled = find_selected_article_files(DATE, tmp_path)

    assert total == 2
    assert len(paths) == 1
    assert missing == 1
    assert hot_filled == 0


@pytest.mark.parametrize("source", ["full", "excerpt"])
def test_materialize_only_uses_full_text(tmp_path, source):
    item = dict(HOT_ITEM, content_source=source)

    path = materialize_hot_post_raw(
        DATE, "7336413989", "412922588", {HOT_ITEM["status_id"]: item}, tmp_path
    )

    assert (path is not None) is (source == "full")
