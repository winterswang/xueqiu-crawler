"""hot_posts 的窗口过滤 / 打分 / 日报段装配 —— 纯函数单测，不碰网络。

背景：2026-10-17 早报新增「🔥 全网热贴 Top 10」（opencli-adapters#8 的
DOM 适配器供数）。抓取本身在 scripts/hot_posts.py 里走 opencli 子进程，
这里只测纯逻辑：T-1 窗口口径、$股票$ 提取、打分权重与霸榜降权、
日报段渲染。适配器字段契约见 opencli-adapters CONTRACT。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

import scripts.hot_posts as hp

TZ = hp.TZ
NOW = datetime(2026, 10, 17, 8, 0, tzinfo=TZ)  # 报告时刻：10-17 08:00

CFG = hp.load_hot_config()
CFG_STRICT = dict(CFG, include_overnight=False)  # 严格 T-1 口径（配置项反演）


def make_item(status_id, created_iso, **kwargs):
    item = {
        "status_id": str(status_id),
        "author": "作者",
        "author_id": "111",
        "url": f"https://xueqiu.com/111/{status_id}",
        "text": "正文" * 100,
        "created_at": created_iso,
        "likes": 10,
        "comments": 20,
        "forwards": 5,
        "position": 1,
    }
    item.update(kwargs)
    return item


# ── created_at 解析 ───────────────────────────────────────────────────


def test_parse_created_at_iso_with_tz():
    dt = hp.parse_created_at("2026-10-16T09:30:00+08:00")
    assert dt is not None and dt.utcoffset().total_seconds() == 8 * 3600


def test_parse_created_at_garbage_returns_none():
    assert hp.parse_created_at("8小时前") is None
    assert hp.parse_created_at("") is None
    assert hp.parse_created_at(None) is None


# ── T-1 窗口过滤 ──────────────────────────────────────────────────────


def test_filter_window_strict_t1():
    items = [
        make_item(1, "2026-10-16T00:01:00+08:00"),  # T-1 凌晨 → 留
        make_item(2, "2026-10-16T23:59:00+08:00"),  # T-1 深夜 → 留
        make_item(3, "2026-10-17T03:00:00+08:00"),  # 今晨 → 严格口径下弃
        make_item(4, "2026-10-15T12:00:00+08:00"),  # T-2 → 弃
        make_item(5, "8小时前"),  # 解析失败 → 弃
    ]
    kept, dropped = hp.filter_window(items, NOW, CFG_STRICT)
    assert [it["status_id"] for it in kept] == ["1", "2"]
    assert dropped == {"no_time": 1, "out_of_window": 2, "too_old": 0}


def test_filter_window_include_overnight():
    items = [
        make_item(1, "2026-10-16T12:00:00+08:00"),
        make_item(2, "2026-10-17T03:00:00+08:00"),
    ]
    kept, _ = hp.filter_window(items, NOW, CFG)  # 默认口径已含今晨
    by_id = {it["status_id"]: it for it in kept}
    assert by_id["1"]["is_overnight"] is False
    assert by_id["2"]["is_overnight"] is True


# ── $股票$ 提取 ───────────────────────────────────────────────────────


def test_extract_symbols_dedup_and_order():
    text = "$中国平安(SH601318)$ 看$伊利股份(SH600887)$，$中国平安(SH601318)$又提"
    assert hp.extract_symbols(text) == ["SH601318", "SH600887"]


def test_extract_symbols_none():
    assert hp.extract_symbols("没有提及任何股票") == []
    assert hp.extract_symbols("") == []


# ── 打分 ──────────────────────────────────────────────────────────────


def _windowed(items):
    kept, _ = hp.filter_window(items, NOW, CFG)
    return kept


def test_score_items_watchlist_and_followed_bonus():
    items = _windowed(
        [
            make_item(
                1,
                "2026-10-16T20:00:00+08:00",
                text="聊聊$中国平安(SH601318)$" + "详细" * 100,
                author_id="999",
            ),
            make_item(
                2,
                "2026-10-16T20:00:00+08:00",
                text="聊聊别的" + "详细" * 100,
                author_id="999",
            ),
            make_item(
                3,
                "2026-10-16T20:00:00+08:00",
                text="聊聊$贵州茅台(SH600519)$" + "详细" * 100,
                author_id="111",
            ),
        ]
    )
    scored = hp.score_items(items, ["SH601318"], {"111"}, CFG, NOW, set())
    by_id = {s["status_id"]: s for s in scored}
    assert by_id["1"]["breakdown"]["watchlist"] == CFG["weights"]["watchlist"]
    assert by_id["1"]["symbols_hit"] == ["SH601318"]
    assert by_id["2"]["breakdown"]["watchlist"] == 0
    assert by_id["3"]["breakdown"]["followed"] == CFG["weights"]["followed"]
    assert by_id["1"]["score"] > by_id["2"]["score"]
    # 同等条件下，榜位靠前的 rank 分更高
    assert by_id["1"]["breakdown"]["rank"] > by_id["3"]["breakdown"]["rank"]


def test_score_items_reply_post_gets_depth_penalty():
    items = _windowed(
        [
            make_item(
                9, "2026-10-16T20:00:00+08:00", text="回复@某人: " + "长篇大论" * 200
            ),
        ]
    )
    scored = hp.score_items(items, [], set(), CFG, NOW, set())
    assert scored[0]["breakdown"]["depth"] == pytest.approx(
        CFG["weights"]["depth"] * 0.2,
    )


def test_score_items_repeat_decay():
    items = _windowed(
        [
            make_item(7, "2026-10-16T20:00:00+08:00"),
            make_item(8, "2026-10-16T20:00:00+08:00"),
        ]
    )
    scored = hp.score_items(items, [], set(), CFG, NOW, {"7"})
    by_id = {s["status_id"]: s for s in scored}
    assert "repeat_decay_applied" in by_id["7"]["breakdown"]
    # rank 分让两条的原始分天然不同，降权断言只看「总分 = 明细和 × 系数」
    raw_7 = sum(
        v for k, v in by_id["7"]["breakdown"].items() if k != "repeat_decay_applied"
    )
    assert by_id["7"]["score"] == pytest.approx(
        raw_7 * CFG["repeat_decay"],
        abs=0.15,
    )


def test_select_top_orders_by_score():
    scored = [
        {"status_id": "a", "score": 50.0},
        {"status_id": "b", "score": 80.0},
        {"status_id": "c", "score": 65.0},
    ]
    top = hp.select_top(scored, 2)
    assert [s["status_id"] for s in top] == ["b", "c"]


# ── 落盘 / 读取 / 日报段 ──────────────────────────────────────────────


def test_load_top_doc_roundtrip(tmp_path):
    doc = {
        "generated_at": NOW.isoformat(),
        "include_overnight": False,
        "window": {
            "start": "2026-10-16T00:00:00+08:00",
            "end": "2026-10-17T00:00:00+08:00",
            "start_date": "2026-10-16",
        },
        "candidates": 2,
        "items": [
            {
                "status_id": "1",
                "author": "a",
                "text": "t",
                "url": "u",
                "likes": 1,
                "comments": 2,
                "forwards": 3,
                "score": 66.6,
                "is_overnight": False,
                "followed": True,
                "symbols_hit": ["SH601318"],
                "breakdown": {},
            }
        ],
    }
    out_dir = tmp_path / "hot_posts" / "2026-10-17"
    out_dir.mkdir(parents=True)
    (out_dir / "hot_top10.json").write_text(
        json.dumps(doc, ensure_ascii=False),
        encoding="utf-8",
    )
    loaded = hp.load_top_doc(tmp_path, report_date="2026-10-17")
    assert loaded["items"][0]["status_id"] == "1"
    assert hp.load_top_doc(tmp_path, report_date="1999-01-01") is None


def test_build_report_section_renders_tags_and_overnight():
    doc = {
        "include_overnight": True,
        "window": {"start_date": "2026-10-16"},
        "candidates": 23,
        "items": [
            {
                "status_id": "1",
                "author": "烽煙縱橫",
                "text": "科技深度调整的原因\n最近科技股又是一波下跌",
                "url": "https://xueqiu.com/1/2",
                "likes": 165,
                "comments": 143,
                "forwards": 3,
                "score": 71.3,
                "is_overnight": True,
                "followed": False,
                "symbols_hit": ["SH601318"],
                "breakdown": {},
            }
        ],
    }
    lines = hp.build_report_section(doc)
    text = "\n".join(lines)
    assert "## 🔥 全网热贴 Top 10" in text
    assert "2026-10-16" in text and "今晨" in text
    assert "烽煙縱橫" in text and "SH601318" in text
    assert "https://xueqiu.com/1/2" in text
