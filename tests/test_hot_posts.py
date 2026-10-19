"""hot_posts 的窗口过滤 / 打分 / 融合映射 —— 纯函数单测，不碰网络。

背景：2026-10-17 早报新增热榜贴（opencli-adapters#8 的 DOM 适配器供数），
2026-10-18 起不再单独成段，而是按文章 ID 去重合并进自选账号文章、走同一套
LLM 分析。抓取本身在 scripts/hot_posts.py 里走 opencli 子进程，这里只测纯逻辑：
T-1 窗口口径、$股票$ 提取、打分权重与霸榜降权、热贴 → article 映射与合并。
适配器字段契约见 opencli-adapters CONTRACT。
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


def test_filter_window_rolling_24h():
    items = [
        make_item(1, "2026-10-16T09:00:00+08:00"),  # 23h 前 → 留
        make_item(2, "2026-10-17T03:00:00+08:00"),  # 5h 前 → 留
        make_item(3, "2026-10-16T07:00:00+08:00"),  # 25h 前 → 弃（窗外）
        make_item(4, "2026-10-15T12:00:00+08:00"),  # 更早 → 弃
        make_item(5, "8小时前"),  # 解析失败 → 弃
    ]
    kept, dropped = hp.filter_window(items, NOW, CFG)
    assert [it["status_id"] for it in kept] == ["1", "2"]
    assert dropped == {"no_time": 1, "out_of_window": 2}


def test_filter_window_tags_overnight():
    items = [
        make_item(1, "2026-10-16T12:00:00+08:00"),
        make_item(2, "2026-10-17T03:00:00+08:00"),
    ]
    kept, _ = hp.filter_window(items, NOW, CFG)  # 两条都在最近 24h 内
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


def test_score_items_uses_fulltext_for_watchlist():
    """摘要里没有自选股、全文里有的，必须给分 —— 实测今天 top10 里有 2 条这种情况。"""
    items = _windowed(
        [
            make_item(
                1,
                "2026-10-16T20:00:00+08:00",
                text="聊聊行情" + "摘要" * 60,
                content="正文详述 $英伟达(NVDA)$ " + "全文" * 500,
            ),
        ]
    )

    scored = hp.score_items(items, ["NVDA"], set(), CFG, NOW, set())

    assert scored[0]["symbols_hit"] == ["NVDA"]
    assert scored[0]["breakdown"]["watchlist"] == CFG["weights"]["watchlist"]


def test_score_items_depth_uses_fulltext_length():
    """深度分档按全文长度（摘要永远够不到高档位）。"""
    items = _windowed(
        [
            make_item(1, "2026-10-16T20:00:00+08:00", content="字" * 4000),
            make_item(2, "2026-10-16T20:00:00+08:00", content="字" * 1000),
            make_item(3, "2026-10-16T20:00:00+08:00", content="字" * 200),
        ]
    )

    scored = hp.score_items(items, [], set(), CFG, NOW, set())
    depth = {s["status_id"]: s["breakdown"]["depth"] for s in scored}

    assert depth["1"] == pytest.approx(CFG["weights"]["depth"] * 1.0)
    assert depth["2"] == pytest.approx(CFG["weights"]["depth"] * 0.5)
    assert depth["3"] == pytest.approx(CFG["weights"]["depth"] * 0.25)


def test_looks_like_pnl_showoff():
    # 晒收益 / 晒亏损：命中
    assert hp.looks_like_pnl_showoff("前前后后在股市里，赚了差不多300万")
    assert hp.looks_like_pnl_showoff("今年回撤170万，啥也不说了看实盘")
    assert hp.looks_like_pnl_showoff("账户翻了3倍")
    # 纯持仓记录：不碰（用户要保留这类）
    assert not hp.looks_like_pnl_showoff("招商银行：持股22300股，不变，每股成本36.075")
    # 公司业绩分析：不碰
    assert not hp.looks_like_pnl_showoff("台积电三季度营收467.2亿美元、同比增长41.2%")


def test_score_items_penalizes_pnl_showoff():
    plain = "行业分析" * 800
    pnl = "前前后后在股市里，赚了差不多300万。" + "行业分析" * 790
    items = _windowed(
        [
            make_item(1, "2026-10-16T20:00:00+08:00", content=plain),
            make_item(2, "2026-10-16T20:00:00+08:00", content=pnl),
        ]
    )

    scored = hp.score_items(items, [], set(), CFG, NOW, set())
    by_id = {s["status_id"]: s for s in scored}

    assert by_id["2"]["is_pnl_showoff"] is True
    assert by_id["1"]["is_pnl_showoff"] is False
    assert by_id["2"]["breakdown"]["pnl_showoff"] == -CFG["penalties"]["pnl_showoff"]
    assert by_id["2"]["score"] < by_id["1"]["score"]


def test_depth_score_thresholds():
    thresholds = {"deep": 3000, "medium": 1500, "short": 800}
    assert hp._depth_score(5000, thresholds) == 1.0
    assert hp._depth_score(2000, thresholds) == 0.75
    assert hp._depth_score(1000, thresholds) == 0.5
    assert hp._depth_score(100, thresholds) == 0.25


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


# ── 落盘 / 读取 ───────────────────────────────────────────────────────


def test_load_top_doc_roundtrip(tmp_path):
    doc = {
        "generated_at": NOW.isoformat(),
        "window": {
            "start": "2026-10-16T00:00:00+08:00",
            "end": "2026-10-17T00:00:00+08:00",
            "hours": 24,
            "start_label": "10-16 00:00",
            "end_label": "10-17 00:00",
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


# ── 热贴 → article 映射 / 与自有文章去重合并 ────────────────────────────


def test_derive_hot_title_collapses_whitespace_and_truncates():
    title = hp.derive_hot_title("科技深度调整的原因\n\n  最近科技股  又是一波下跌")
    assert "\n" not in title
    assert "  " not in title
    long_title = hp.derive_hot_title("字" * 100)
    assert len(long_title) == hp.HOT_TITLE_MAX_CHARS + 1
    assert long_title.endswith("…")


def test_derive_hot_title_strips_reply_prefix():
    assert not hp.derive_hot_title("回复@某人: 我看好这只票").startswith("回复@")
    assert "我看好这只票" in hp.derive_hot_title("回复@某人: 我看好这只票")


def test_derive_hot_title_falls_back_for_blank_text():
    assert hp.derive_hot_title("", "张三") == "张三的热门贴"


def test_hot_item_to_article_maps_all_fields():
    item = make_item(
        "412750388",
        "2026-10-17T23:41:00+08:00",
        author="股海行道",
        author_id="2740510444",
        text="这波牛市教会我一个道理，" + "反复复盘才发现" * 20,
        likes=59,
        comments=49,
        forwards=0,
        score=76.03,
        is_overnight=True,
        followed=True,
        symbols_hit=["SH601899"],
    )
    article = hp.hot_item_to_article(item, 3)

    assert article is not None
    assert article["article_id"] == "412750388"
    assert article["user_id"] == "2740510444"
    assert article["source"] == "hot"
    assert article["content_source"] == "excerpt"
    assert article["content"] == item["text"]
    assert article["publish_time"] == "2026-10-17T23:41:00+08:00"
    assert article["url"] == "https://xueqiu.com/111/412750388"
    assert article["hot"] == {
        "rank": 3,
        "site_rank": None,
        "score": 76.0,
        "likes": 59,
        "comments": 49,
        "forwards": 0,
        "is_overnight": True,
        "followed": True,
        "symbols_hit": ["SH601899"],
    }


def test_hot_item_to_article_parses_user_id_from_url():
    item = make_item("7", "2026-10-17T10:00:00+08:00", author_id="")
    item["url"] = "https://xueqiu.com/9508203182/7"
    assert hp.hot_item_to_article(item, 1)["user_id"] == "9508203182"


def test_hot_item_to_article_rejects_blank_status_or_short_text():
    short = make_item("1", "2026-10-17T10:00:00+08:00", text="太短了" * 3)
    assert hp.hot_item_to_article(short, 1) is None
    no_id = make_item("", "2026-10-17T10:00:00+08:00")
    assert hp.hot_item_to_article(no_id, 1) is None


def test_merge_hot_items_merges_by_article_id_and_keeps_fulltext():
    full_text = "全文" * 500
    articles = [
        {"article_id": "123", "title": "自有标题", "content": full_text},
        {"article_id": "456", "title": "另一篇", "content": full_text},
    ]
    hot_items = [make_item("123", "2026-10-17T10:00:00+08:00", text="摘要" * 60)]

    merged, stats = hp.merge_hot_items(articles, hot_items)

    assert len(merged) == 2
    assert merged[0]["source"] == "both"
    assert merged[0]["content"] == full_text  # 摘要绝不覆盖已爬到的全文
    assert merged[0]["hot"]["rank"] == 1
    assert merged[0]["url"] == "https://xueqiu.com/111/123"
    assert stats == {"hot_total": 1, "merged": 1, "hot_only": 0, "skipped": 0}


def test_merge_hot_items_appends_hot_only_in_order():
    articles = [
        {"article_id": "a", "title": "a", "content": "x" * 400},
        {"article_id": "b", "title": "b", "content": "x" * 400},
    ]
    hot_items = [
        make_item("c", "2026-10-17T10:00:00+08:00"),
        make_item("a", "2026-10-17T09:00:00+08:00"),
    ]

    merged, stats = hp.merge_hot_items(articles, hot_items)

    assert [a["article_id"] for a in merged] == ["a", "b", "c"]
    assert merged[0]["source"] == "both"
    assert merged[2]["source"] == "hot"
    assert merged[2]["hot"]["rank"] == 1
    assert stats == {"hot_total": 2, "merged": 1, "hot_only": 1, "skipped": 0}


def test_merge_hot_items_skips_duplicate_hot_status():
    hot_items = [
        make_item("1", "2026-10-17T10:00:00+08:00"),
        make_item("1", "2026-10-17T10:00:00+08:00"),
    ]
    merged, stats = hp.merge_hot_items([], hot_items)
    assert len(merged) == 1
    assert stats["hot_only"] == 1
    assert stats["skipped"] == 1


def test_merge_hot_items_prefers_longer_text_when_crawled_body_shorter():
    """详情页只渲染出一部分的短正文，不该压过更长的热榜摘要。"""
    articles = [{'article_id': '1', 'title': 't', 'content': '短' * 50}]
    hot_items = [make_item('1', '2026-10-17T10:00:00+08:00', text='长' * 150)]

    merged, _ = hp.merge_hot_items(articles, hot_items)

    assert len(merged[0]['content']) == 150
    assert merged[0]['content_source'] == 'excerpt'


def test_merge_keeps_full_source_when_hot_side_has_fulltext():
    """热榜侧已有全文时，替换后不能标成 excerpt（否则 LLM 会被误导成「只有摘要」）。"""
    articles = [{'article_id': '1', 'title': 't', 'content': '短' * 50}]
    hot_items = [
        make_item(
            '1',
            '2026-10-17T10:00:00+08:00',
            content='全文' * 200,
            content_source='full',
        )
    ]

    merged, _ = hp.merge_hot_items(articles, hot_items)

    assert merged[0]['content'] == '全文' * 200
    assert merged[0]['content_source'] == 'full'


def test_merge_hot_items_skips_non_dict_items():
    merged, stats = hp.merge_hot_items([], ['not-a-dict'])
    assert merged == []
    assert stats['skipped'] == 1


def test_filter_candidates_drops_short_and_prior():
    items = [
        make_item("1", "2026-10-17T10:00:00+08:00"),
        make_item("2", "2026-10-17T10:00:00+08:00", text="太短" * 10),
        make_item("3", "2026-10-17T10:00:00+08:00"),
        make_item("1", "2026-10-17T10:00:00+08:00"),
    ]

    out = hp.filter_candidates(items, {"3"})

    assert [item["status_id"] for item in out] == ["1"]


# ── 01:00 prepare job：抓全文并写回 item ────────────────────────────────


def _enrich(items, fetch):
    return hp.enrich_items_with_fulltext(items, fetch=fetch)


def test_enrich_writes_fulltext_and_title():
    items = [make_item("1", "2026-10-17T10:00:00+08:00")]
    calls = []

    def fetch(url):
        calls.append(url)
        return {"url": url, "title": "真实标题", "content": "全文" * 300}

    stats = _enrich(items, fetch)

    assert stats == {"attempted": 1, "fetched": 1, "skipped": 0}
    assert calls == ["https://xueqiu.com/111/1"]
    assert items[0]["content_source"] == "full"
    assert items[0]["content"] == "全文" * 300
    assert items[0]["title"] == "真实标题"


def test_enrich_skips_items_already_has_content():
    items = [make_item("1", "2026-10-17T10:00:00+08:00", content="已抓好的全文")]

    def fetch(url):  # pragma: no cover - 不该被调用
        raise AssertionError("已有全文的条目不该重复抓")

    stats = _enrich(items, fetch)

    assert stats == {"attempted": 0, "fetched": 0, "skipped": 0}


@pytest.mark.parametrize("mode", ["waf", "cooldown"])
def test_enrich_stops_on_waf_or_cooldown(mode):
    items = [
        make_item("1", "2026-10-17T10:00:00+08:00"),
        make_item("2", "2026-10-17T10:00:00+08:00"),
    ]
    calls = []

    def fetch(url):
        calls.append(url)
        if mode == "waf":
            return {"url": url, "title": "", "content": "", "waf_detected": True}
        return {"url": url, "title": "", "content": "", "cooldown": True}

    stats = _enrich(items, fetch)

    assert len(calls) == 1, "撞墙/冷却后必须停手，不能逐条继续打站点"
    assert stats["fetched"] == 0
    assert all("content" not in item for item in items)


def test_enrich_keeps_excerpt_when_body_shorter():
    items = [make_item("1", "2026-10-17T10:00:00+08:00", text="摘要" * 100)]

    def fetch(url):
        return {"url": url, "title": "", "content": "只有一小段" * 2}

    stats = _enrich(items, fetch)

    assert stats["skipped"] == 1
    assert "content" not in items[0]


def test_enrich_keeps_derived_title_when_site_chrome():
    items = [make_item("1", "2026-10-17T10:00:00+08:00")]

    def fetch(url):
        return {
            "url": url,
            "title": "雪球-聪明的投资者都在这里",
            "content": "全文" * 300,
        }

    _enrich(items, fetch)

    assert items[0]["content_source"] == "full"
    assert items[0]["title"].startswith("正文")


def test_enrich_survives_fetch_exception():
    items = [
        make_item("1", "2026-10-17T10:00:00+08:00"),
        make_item("2", "2026-10-17T10:00:00+08:00"),
    ]
    calls = []

    def fetch(url):
        calls.append(url)
        raise FileNotFoundError("opencli: command not found")

    stats = _enrich(items, fetch)

    assert len(calls) == 1
    assert stats["fetched"] == 0
    assert all("content" not in item for item in items)


# ── 全文抓取节流（详情页令牌桶，2026-10-19 两次实测死在第 42 篇）────────


class _FakeClock:
    """替代 hp.time：monotonic 可控，sleep 只推进时钟（测试不真睡）。"""

    def __init__(self, start=1000.0):
        self.now = float(start)
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_enrich_paces_between_fetches(monkeypatch):
    clock = _FakeClock()

    def fetch(url):
        clock.now += 9.0  # 单次导航 ~9s（限速 + 导航），插件预留被导航吃掉
        return {"url": url, "title": "", "content": "全文" * 300}

    monkeypatch.setattr(hp, "time", clock)
    items = [
        make_item("1", "2026-10-17T10:00:00+08:00"),
        make_item("2", "2026-10-17T10:00:00+08:00"),
        make_item("3", "2026-10-17T10:00:00+08:00"),
    ]

    stats = hp.enrich_items_with_fulltext(items, fetch=fetch, interval_seconds=15.0)

    assert stats["fetched"] == 3
    # 首条不等待；后续每条把「距上次发起」补齐到 15s（导航已用 9s → 睡 6s）
    assert clock.sleeps == [6.0, 6.0]


def test_enrich_without_interval_never_sleeps(monkeypatch):
    clock = _FakeClock()

    def fetch(url):
        clock.now += 9.0
        return {"url": url, "title": "", "content": "全文" * 300}

    monkeypatch.setattr(hp, "time", clock)
    items = [make_item("1", "2026-10-17T10:00:00+08:00")]

    stats = hp.enrich_items_with_fulltext(items, fetch=fetch)

    assert stats["fetched"] == 1
    assert clock.sleeps == []


def test_enrich_interval_counts_against_budget(monkeypatch):
    clock = _FakeClock()

    def fetch(url):
        clock.now += 9.0
        return {"url": url, "title": "", "content": "全文" * 300}

    monkeypatch.setattr(hp, "time", clock)
    items = [
        make_item("1", "2026-10-17T10:00:00+08:00"),
        make_item("2", "2026-10-17T10:00:00+08:00"),
        make_item("3", "2026-10-17T10:00:00+08:00"),
    ]

    stats = hp.enrich_items_with_fulltext(
        items, fetch=fetch, budget_seconds=20.0, interval_seconds=15.0
    )

    # 预算 20s：#1 发起@1000 → 完@1009；#2 等 6s 发起@1015 → 完@1024；
    # #3 顶部检查 1024 > 1020 截停。等待确实占预算，不会靠睡绕过截停。
    assert stats["attempted"] == 2
    assert clock.sleeps == [6.0]


# ── main 的调用顺序：watchlist 必须先于全文抓取 ─────────────────────────


def test_main_fetches_watchlist_before_fulltext_burst(monkeypatch, tmp_path):
    """熔断丢弃 bug 的回归钉。

    2026-10-19 01:00 实测：watchlist 排在全文抓取之后，全文途中撞墙触发的
    熔断冷却让它抛 RuntimeError → 整轮作废，已抓到手的 41 篇全文全部丢弃。
    顺序对调后，全文途中撞墙只由 enrich 内部停手兜底，hot_top10.json 照常落盘。
    """
    order = []
    raw = [make_item("1", "2026-10-17T10:00:00+08:00")]

    monkeypatch.setattr(hp, "load_hot_config", lambda path=None: dict(CFG))
    monkeypatch.setattr(hp, "fetch_hot_posts", lambda cfg: list(raw))
    monkeypatch.setattr(
        hp,
        "filter_window",
        lambda items, now, cfg: (list(items), {"no_time": 0, "out_of_window": 0}),
    )
    monkeypatch.setattr(hp, "load_prior_status_ids", lambda data_dir, day: set())
    monkeypatch.setattr(hp, "load_prev_top_status_ids", lambda data_dir, now: [])
    monkeypatch.setattr(hp, "filter_candidates", lambda items, prior: list(items))
    monkeypatch.setattr(
        hp,
        "fetch_watchlist_codes",
        lambda data_dir, now: order.append("watchlist") or ["00700"],
    )
    monkeypatch.setattr(hp, "load_followed_author_ids", lambda: {})

    def fake_enrich(
        items, log=None, budget_seconds=0.0, fetch=None, interval_seconds=0.0
    ):
        # 顺手钉死配置透传：main() 改了 cfg.get 的键名/删掉透传，这里立刻红
        assert budget_seconds == CFG["fulltext_budget_seconds"]
        assert interval_seconds == CFG["fulltext_interval_seconds"]
        order.append("fulltext")
        return {"attempted": len(items), "fetched": len(items), "skipped": 0}

    monkeypatch.setattr(hp, "enrich_items_with_fulltext", fake_enrich)
    monkeypatch.setattr(
        hp,
        "score_items",
        lambda candidates, watchlist, followed, cfg, now, prev: [
            dict(item, score=50.0) for item in candidates
        ],
    )
    monkeypatch.setattr(hp, "select_top", lambda scored, n: list(scored)[:n])

    rc = hp.main(["--fetch-fulltext", "--data-dir", str(tmp_path)])

    assert rc == 0
    assert order == ["watchlist", "fulltext"]


def test_load_prior_status_ids_excludes_today(tmp_path):
    (tmp_path / 'index.json').write_text(
        json.dumps(
            {
                "articles": {
                    "a": {"article_id": "1", "crawl_time": "2026-10-17T08:00:00"},
                    "b": {"article_id": "2", "crawl_time": "2026-10-18T08:00:00"},
                    "c": {"article_id": "3"},
                }
            }
        ),
        encoding="utf-8",
    )

    assert hp.load_prior_status_ids(tmp_path, "2026-10-18") == {"1", "3"}


def test_merge_hot_items_empty_hot_returns_original():
    articles = [{"article_id": "a", "title": "a", "content": "x" * 400}]
    merged, stats = hp.merge_hot_items(articles, [])
    assert merged == articles
    assert stats == {"hot_total": 0, "merged": 0, "hot_only": 0, "skipped": 0}
