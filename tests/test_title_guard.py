"""标题可用性 —— 站点栏目标题 / 列表占位符不该被当成文章标题（2026-10-09）.

背景：雪球详情页没渲染出文章标题时 `document.title` 会退回站点栏目
`雪球-聪明的投资者都在这里`。它是**非空**的，所以躲过了 `is_error_page` 的
「空标题算错误页」判定，一路写进 `index.json` 与 `.md`，在日报「参考」段里
显示成一篇正常文章。实测 `data/index.json` 里 7 条这样的标题，全部来自
同一个只发图不发字的账号（永庆好公司）。

另一条同源：列表页的截断提示 `展开` 会连图标字形一起被抓成标题（`展开\ue63c`），
在详情标题不可用时就会顶上。
"""

from __future__ import annotations

import datetime
import json
from unittest.mock import MagicMock

import pytest

import scripts.generate_report as gr
import scripts.opencli_extractor as oe
from scripts.crawler_nodriver import XueqiuCrawlerNodriver
from scripts.title_guard import is_usable_title, normalize_title, resolve_title

CHROME = "雪球-聪明的投资者都在这里"


# ── 判定本身 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("title", [CHROME, "雪球", f"  {CHROME}  "])
def test_site_chrome_titles_are_not_usable(title):
    assert is_usable_title(title) is False


@pytest.mark.parametrize("title", ["展开", "展开\ue63c", "全文", "阅读全文"])
def test_list_placeholders_are_not_usable(title):
    assert is_usable_title(title) is False


@pytest.mark.parametrize(
    "title",
    [
        "这么糊，那算了吧",
        "把他们在亚马逊和 Temu 上的运营费用进行比较",
        "我看好雪球",  # 含「雪球」但是正常标题 —— 只做精确匹配，不做子串
        "主动vs被动展开",  # 截断痕迹留着，但不能因为尾巴像占位符就整条扔掉
    ],
)
def test_real_titles_stay_usable(title):
    assert is_usable_title(title) is True


@pytest.mark.parametrize("title", ["", "   ", None])
def test_blank_titles_are_not_usable(title):
    assert is_usable_title(title) is False


def test_normalize_strips_icon_font_glyphs():
    assert normalize_title("展开\ue63c") == "展开"
    assert normalize_title("  某标题\ue63c ") == "某标题"


# ── 兜底链 ──────────────────────────────────────────────────────────────


def test_resolve_prefers_the_detail_title():
    assert resolve_title("真标题", "列表标题") == "真标题"


def test_resolve_falls_back_to_the_list_title():
    """这就是原来的漏洞：`detail or list` 对非空但没用的值不会回退."""
    assert resolve_title(CHROME, "列表标题") == "列表标题"


def test_resolve_returns_empty_when_neither_is_usable():
    assert resolve_title(CHROME, "展开\ue63c") == ""
    assert resolve_title("", "") == ""


# ── crawler 接线 ────────────────────────────────────────────────────────


def _crawler(monkeypatch, detail: dict, tmp_path) -> XueqiuCrawlerNodriver:
    """走**真实的 `OpencliExtractor`**，只把最底层的模块函数换掉.

    与 test_waf_observability 同一套做法：手写的假 `_opencli` 会把
    `get_article_content` 重建 dict 时丢字段这类真问题一起遮掉。
    """
    crawler = object.__new__(XueqiuCrawlerNodriver)
    crawler.logger = MagicMock()
    crawler.index = {"articles": {}}
    crawler._waf_blocked_articles = 0
    crawler.data_dir = tmp_path
    crawler.index_file = tmp_path / "index.json"
    crawler._opencli = oe.OpencliExtractor()
    monkeypatch.setattr(oe, "get_article_content", lambda _url, _session: dict(detail))
    return crawler


def test_crawler_uses_the_list_title_when_the_detail_title_is_chrome(
    monkeypatch, tmp_path
):
    crawler = _crawler(
        monkeypatch,
        {"url": "u", "title": CHROME, "content": "正文" * 100, "waf_detected": False},
        tmp_path,
    )

    saved = crawler._extract_and_save_opencli(
        {"article_id": "1", "url": "https://xueqiu.com/1/1", "title": "列表里的真标题"},
        "1",
        "u",
    )

    assert saved is True
    entry = crawler.index["articles"]["1_1"]
    assert entry["title"] == "列表里的真标题", "站点栏目标题不能落进索引"
    on_disk = (tmp_path / "1" / "1.md").read_text(encoding="utf-8")
    assert on_disk.startswith("# 列表里的真标题")
    assert CHROME not in on_disk


def test_crawler_skips_an_article_with_no_usable_title(monkeypatch, tmp_path):
    """列表标题也是占位符 → 没有可用标题，不落盘，且**不能**算成 WAF 拦截."""
    crawler = _crawler(
        monkeypatch,
        {
            "url": "u",
            "title": CHROME,
            "content": "来源：雪球App",
            "waf_detected": False,
        },
        tmp_path,
    )

    saved = crawler._extract_and_save_opencli(
        {
            "article_id": "411546152",
            "url": "https://xueqiu.com/1/1",
            "title": "展开\ue63c",
        },
        "1",
        "u",
    )

    assert saved is False
    assert crawler.index["articles"] == {}
    assert not (tmp_path / "1").exists(), "不该留下任何落盘文件"
    assert crawler._waf_blocked_articles == 0, "渲染不全 ≠ 风控拦截，别混成一个数"


# ── 日报接线 ────────────────────────────────────────────────────────────


def _write_article(data, article_id: str, title: str, body: str) -> None:
    user = data / "9"
    user.mkdir(parents=True, exist_ok=True)
    md = user / f"{article_id}.md"
    md.write_text(
        f"# {title}\n\n> 作者：某人\n> 原文链接：https://xueqiu.com/9/{article_id}\n\n"
        f"---\n\n{body}\n\n---\n\n*爬取时间：x*\n",
        encoding="utf-8",
    )
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    index_file = data / "index.json"
    index = (
        json.loads(index_file.read_text(encoding="utf-8"))
        if index_file.exists()
        else {"articles": {}}
    )
    index["articles"][f"9_{article_id}"] = {
        "article_id": article_id,
        "user_id": "9",
        "title": title,
        "author": "某人",
        "crawl_time": f"{today}T08:00:00",
        "filepath": str(md),
    }
    index_file.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


def test_get_today_articles_drops_chrome_titled_articles(tmp_path):
    """回归：带站点栏目标题的文章原来会以「一篇正常文章」进日报的参考段.

    那篇的真实内容只剩「来源：雪球App…」，`is_error_page` 判它没问题
    （标题非空、正文不命中风控词），只有标题可用性这一条能拦住。
    """
    data = tmp_path / "data"
    _write_article(data, "1", CHROME, "来源：雪球App，作者： 永庆好公司")
    _write_article(
        data, "2", "把他们在亚马逊和 Temu 上的运营费用进行比较", "正文" * 100
    )

    titles = [a["title"] for a in gr.get_today_articles(str(data))]

    assert titles == ["把他们在亚马逊和 Temu 上的运营费用进行比较"]


def test_get_today_articles_keeps_normal_titles(tmp_path):
    """对照：同一篇文章换个正常标题就必须留下 —— 证明上面拦的确实是标题本身."""
    data = tmp_path / "data"
    _write_article(data, "1", "我看好雪球", "正文" * 100)

    assert len(gr.get_today_articles(str(data))) == 1
