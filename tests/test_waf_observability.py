"""让「被 WAF 拦」这件事在各个调用方都可见（2026-10-09）。

三个独立但同源的问题：

1. `cli.py` 构造 `XueqiuNodriverAdapter()` 时丢了 `--data-dir` → shadow 跑
   `--data-dir data-gateway` 时一旦回退 nodriver，会写进**生产目录** `data/`，
   污染用来做对比的基准 —— 而 shadow 对比正是这轮改动的起因。
2. opencli 列表撞风控被 `get_user_articles` 吞成 None → gateway 侧映射成
   `http_error`，而它不在 `circuit_breaker.hard_failure_statuses` 里 ——
   「账号级」这个口径在 opencli 路径上等于不存在。
3. `.last_crawl_stats.json` 没有「被拦篇数」字段：WAF 拦掉的正文不计入 `failed`，
   于是一轮被拦的爬取会显示成「17/17 成功、新增 0 篇」，分不出是安静日还是被拦日。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import scripts.opencli_extractor as oe
from crawl_gateway.adapters.opencli_client import OpencliArticleClient
from crawl_gateway.adapters.xueqiu_nodriver import XueqiuNodriverAdapter
from crawl_gateway.models import AttemptStatus
from crawl_gateway.orchestrator import TaskSpec
from scripts.crawler_nodriver import XueqiuCrawlerNodriver

TASK = TaskSpec("user_timeline", "5739488179")


# ── 1. data_dir 必须传到 nodriver 适配器 ────────────────────────────────


def test_nodriver_adapter_uses_the_configured_data_dir(tmp_path):
    seen: list[str] = []

    def runner(_user_id, _max_articles, data_dir):
        seen.append(str(data_dir))
        return {"saved_articles": 0, "new_articles": 0}

    target = tmp_path / "data-gateway"
    XueqiuNodriverAdapter(runner=runner, data_dir=target).execute(TASK, "nodriver")

    assert seen == [str(target)]


def test_nodriver_adapter_defaults_to_the_repo_data_dir():
    """不传 data_dir 时保持历史行为（写仓库 data/）。"""
    adapter = XueqiuNodriverAdapter()
    assert adapter.data_dir.name == "data"
    assert adapter.data_dir.parent.name == "xueqiu-crawler"


def test_execute_backend_wires_data_dir_into_nodriver(tmp_path):
    """回归防线：原来这里构造 `XueqiuNodriverAdapter()` 是无参的。"""
    from crawl_gateway.cli import _build_execute_backend

    target = tmp_path / "data-gateway"
    _client, backend = _build_execute_backend("xueqiu", str(target))

    assert backend.nodriver.data_dir == target


# ── 2. 列表页撞风控要能作为账号级硬失败传上去 ───────────────────────────


def _waf_runtime_error() -> RuntimeError:
    return RuntimeError(
        "opencli failed: 风控验证页（滑动验证 / 访问频繁）——这不是\"没有数据\""
    )


def test_get_user_articles_raises_on_waf_instead_of_swallowing(monkeypatch):
    monkeypatch.setattr(
        oe, "_run", lambda *a, **kw: (_ for _ in ()).throw(_waf_runtime_error())
    )

    with pytest.raises(oe.WafBlockedError):
        oe.get_user_articles("1", 20)


def test_get_user_articles_still_returns_none_on_other_failures(monkeypatch):
    """普通失败保持原语义（None），别把调用方的分支改动放大。"""
    monkeypatch.setattr(
        oe,
        "_run",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("opencli failed: boom")),
    )

    assert oe.get_user_articles("1", 20) is None


def test_opencli_client_maps_listing_waf_to_blocked_waf(monkeypatch):
    monkeypatch.setattr(
        oe,
        "get_user_articles",
        lambda *a, **kw: (_ for _ in ()).throw(oe.WafBlockedError("风控验证页")),
    )

    with pytest.raises(Exception) as excinfo:
        OpencliArticleClient().list_user_articles("1", 20)

    assert getattr(excinfo.value, "status", None) is AttemptStatus.BLOCKED_WAF


def test_opencli_client_waf_listing_is_a_hard_failure_status():
    """BLOCKED_WAF 必须在熔断器的硬失败名单里 —— 这是「账号级」口径的落点。"""
    from crawl_gateway.config import load_sites_config
    from pathlib import Path

    config = load_sites_config(Path("config/sites.yaml")).sites["xueqiu"]
    assert AttemptStatus.BLOCKED_WAF in config.circuit_breaker.hard_failure_statuses


# ── 3. 被拦篇数要落进统计文件 ─────────────────────────────────────────


def _crawler_with_detail(detail: dict) -> XueqiuCrawlerNodriver:
    crawler = object.__new__(XueqiuCrawlerNodriver)
    crawler.logger = MagicMock()
    crawler.index = {"articles": {}}
    crawler._opencli = SimpleNamespace(get_article_content=lambda _url: detail)
    crawler._waf_blocked_articles = 0
    return crawler


def test_adapter_waf_hit_is_counted_and_not_saved():
    crawler = _crawler_with_detail(
        {"url": "u", "title": "被拦", "content": "", "waf_detected": True}
    )

    saved = crawler._extract_and_save_opencli(
        {"article_id": "1", "url": "https://xueqiu.com/1/1", "title": "被拦"}, "1", "u"
    )

    assert saved is False
    assert crawler._waf_blocked_articles == 1


def test_empty_content_without_waf_is_not_counted_as_blocked():
    """空正文 ≠ 被拦 —— 别把两者混成一个数。"""
    crawler = _crawler_with_detail(
        {"url": "u", "title": "空的", "content": "", "waf_detected": False}
    )

    crawler._extract_and_save_opencli(
        {"article_id": "1", "url": "https://xueqiu.com/1/1", "title": "空的"}, "1", "u"
    )

    assert crawler._waf_blocked_articles == 0


def test_last_crawl_stats_carries_blocked_articles(tmp_path):
    """统计文件要有 blocked_articles，好让巡检区分「安静日」与「被拦日」.

    走的是**生产那个 `_write_crawl_stats`**，不是在这里重写一遍字段拼装 ——
    否则断言只是在验证测试自己。
    """
    import json as _json

    crawler = object.__new__(XueqiuCrawlerNodriver)
    crawler.logger = MagicMock()
    crawler.data_dir = tmp_path

    crawler._write_crawl_stats(
        {
            "total_users": 2,
            "total_new": 0,
            "total_saved": 0,
            "total_blocked": 3,
            "users": [{"saved_articles": 0}, {"saved_articles": 0}],
        }
    )

    payload = _json.loads((tmp_path / ".last_crawl_stats.json").read_text())
    # 这一组正是要防的误读：看起来「2/2 成功、新增 0」，其实有 3 篇被拦
    assert payload["successful"] == 2
    assert payload["failed"] == 0
    assert payload["new_articles"] == 0
    assert payload["blocked_articles"] == 3
