from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import yaml

import scripts.opencli_extractor
from crawl_gateway.adapters.xueqiu import (
    ArticleStore,
    SiteAccessError,
    XueqiuAdapter,
)
from crawl_gateway.adapters.opencli_client import OpencliArticleClient
from crawl_gateway.adapters.xueqiu_backend import XueqiuBackend
from crawl_gateway.adapters.xueqiu_nodriver import XueqiuNodriverAdapter
from crawl_gateway.models import SUCCESS_STATUSES, AttemptScope, AttemptStatus
from crawl_gateway.orchestrator import TaskSpec


USER_ID = "5739488179"
TASK = TaskSpec("user_timeline", USER_ID)


class FakeClient:
    def __init__(
        self,
        articles: list[dict],
        details: list[dict] | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self.articles = articles
        self.details = list(details or [])
        self.list_error = list_error
        self.detail_calls: list[str] = []

    def list_user_articles(self, user_id: str, count: int) -> list[dict]:
        if self.list_error is not None:
            raise self.list_error
        return self.articles[:count]

    def get_article_content(self, url: str) -> dict:
        self.detail_calls.append(url)
        if self.details:
            return self.details.pop(0)
        return {"title": "Detail title", "content": "Detail body"}


def write_accounts(path: Path, enabled: bool = True) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "accounts": [
                    {
                        "id": USER_ID,
                        "name": "Elon翻开每一页",
                        "enabled": enabled,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def make_adapter(
    tmp_path: Path,
    client: FakeClient,
    *,
    enabled: bool = True,
    is_error_content=lambda title, content: False,
) -> XueqiuAdapter:
    accounts_path = tmp_path / "accounts.yaml"
    write_accounts(accounts_path, enabled=enabled)
    timestamps = iter(
        [
            datetime(2026, 10, 6, 8, 1, 0),
            datetime(2026, 10, 6, 8, 1, 1),
            datetime(2026, 10, 6, 8, 1, 2),
            datetime(2026, 10, 6, 8, 1, 3),
            datetime(2026, 10, 6, 8, 1, 4),
            datetime(2026, 10, 6, 8, 1, 5),
        ]
    )
    return XueqiuAdapter(
        client=client,
        data_dir=tmp_path / "data",
        accounts_path=accounts_path,
        now=lambda: next(timestamps),
        is_error_content=is_error_content,
    )


def article(article_id: str, *, is_column: bool = True) -> dict:
    return {
        "article_id": article_id,
        "title": f"List title {article_id}",
        "author": "Elon翻开每一页",
        "time": "10-06 08:00",
        "likes": 12,
        "replies": 3,
        "is_column": is_column,
        "url": f"https://xueqiu.com/{USER_ID}/{article_id}",
    }


def test_adapter_saves_column_and_preserves_legacy_artifacts(tmp_path):
    client = FakeClient([article("1")])
    adapter = make_adapter(tmp_path, client)

    result = adapter.execute(TASK, "opencli")

    assert result.status == AttemptStatus.SUCCESS
    assert result.new_articles == 1
    assert result.saved_articles == 1

    markdown = (tmp_path / "data" / USER_ID / "1.md").read_text(encoding="utf-8")
    assert "# Detail title" in markdown
    assert "作者：Elon翻开每一页 | 发布时间：10-06 08:00" in markdown
    assert "原文链接：https://xueqiu.com/5739488179/1" in markdown
    assert "Detail body" in markdown
    assert "*爬取时间：2026-10-06 08:01:00*" in markdown

    index = json.loads((tmp_path / "data" / "index.json").read_text(encoding="utf-8"))
    record = index["articles"][f"{USER_ID}_1"]
    assert record["article_id"] == "1"
    assert record["file_path"] == record["filepath"]
    assert record["crawl_time"] == "2026-10-06T08:01:00"

    history = json.loads(
        (tmp_path / "data" / "history" / USER_ID / "2026-10-06.json").read_text(
            encoding="utf-8"
        )
    )
    assert history["article_count"] == 1
    assert history["articles"][0]["article_id"] == "1"


def test_adapter_deduplicates_against_index_history_and_filesystem(tmp_path):
    data_dir = tmp_path / "data"
    client = FakeClient([article("1"), article("2")])
    first_adapter = make_adapter(tmp_path, client)
    first_adapter.execute(TASK, "opencli")

    history_dir = data_dir / "history" / USER_ID
    history_path = history_dir / "2026-10-05.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(
            {"articles": [{"article_id": "2", "title": "Known", "crawl_time": None}]}
        ),
        encoding="utf-8",
    )
    filesystem_marker = data_dir / USER_ID / "3.md"
    filesystem_marker.parent.mkdir(parents=True, exist_ok=True)
    filesystem_marker.write_text("# Known", encoding="utf-8")

    second_client = FakeClient([article("1"), article("2"), article("3")])
    second_adapter = make_adapter(tmp_path, second_client)
    result = second_adapter.execute(TASK, "opencli")

    assert result.status == AttemptStatus.DUPLICATE
    assert result.new_articles == 0
    assert second_client.detail_calls == []


def test_adapter_limits_non_column_detail_probes(tmp_path):
    client = FakeClient(
        [
            article("1", is_column=False),
            article("2", is_column=False),
            article("3", is_column=False),
            article("4", is_column=False),
        ]
    )
    adapter = make_adapter(tmp_path, client)

    result = adapter.execute(TASK, "opencli")

    assert result.status == AttemptStatus.SUCCESS
    assert result.new_articles == 3
    assert result.saved_articles == 3
    assert len(client.detail_calls) == 3


def test_adapter_maps_waf_content_and_does_not_write_article(tmp_path):
    client = FakeClient([article("1")])
    adapter = make_adapter(
        tmp_path,
        client,
        is_error_content=lambda title, content: "slider" in content,
    )
    client.details = [{"title": "Block", "content": "slider required"}]

    result = adapter.execute(TASK, "opencli")

    assert result.status == AttemptStatus.BLOCKED_WAF
    assert result.error == "waf_content:1"
    # 列表页已经拿到了、只是这篇正文被拦 → 详情级，不该触发站点熔断
    assert result.scope is AttemptScope.DETAIL
    assert not (tmp_path / "data" / USER_ID / "1.md").exists()
    assert not (tmp_path / "data" / "index.json").exists()


def test_adapter_maps_site_access_error_and_skips_disabled_accounts(tmp_path):
    client = FakeClient(
        [],
        list_error=SiteAccessError(
            AttemptStatus.NETWORK_ERROR, "opencli daemon unavailable"
        ),
    )
    adapter = make_adapter(tmp_path, client)
    result = adapter.execute(TASK, "opencli")
    assert result.status == AttemptStatus.NETWORK_ERROR

    disabled_adapter = make_adapter(tmp_path, FakeClient([]), enabled=False)
    disabled_result = disabled_adapter.execute(TASK, "opencli")
    assert disabled_result.status == AttemptStatus.SKIPPED
    assert disabled_result.error == "account_disabled_or_missing"


def test_adapter_returns_no_update_for_empty_timeline_and_skips_nodriver(tmp_path):
    adapter = make_adapter(tmp_path, FakeClient([]))
    empty_result = adapter.execute(TASK, "opencli")
    unsupported_result = adapter.execute(TASK, "nodriver")

    assert empty_result.status == AttemptStatus.NO_UPDATE
    assert unsupported_result.status == AttemptStatus.SKIPPED
    assert unsupported_result.error == "backend_not_implemented_by_xueqiu_adapter"


def test_article_store_atomic_index_write_preserves_existing_records(tmp_path):
    store = ArticleStore(tmp_path)
    store.save_article(
        {
            "article_id": "1",
            "title": "One",
            "author": "Author",
            "publish_time": "10-06 08:00",
            "content": "Body",
            "url": "https://example.com/1",
            "crawl_time": "2026-10-06T08:00:00",
        },
        USER_ID,
        datetime(2026, 10, 6, 8, 0),
    )
    reloaded = ArticleStore(tmp_path)

    assert reloaded.known_article_ids(USER_ID) == {"1"}
    assert list(reloaded.index["articles"]) == [f"{USER_ID}_1"]


def test_opencli_client_maps_command_failure_and_waf_detail(monkeypatch):
    client = OpencliArticleClient()
    requested_sessions = []

    monkeypatch.setattr(
        scripts.opencli_extractor,
        "get_user_articles",
        lambda user_id, count: None,
    )
    try:
        client.list_user_articles(USER_ID, 20)
        raise AssertionError("expected SiteAccessError")
    except SiteAccessError as exc:
        assert exc.status == AttemptStatus.HTTP_ERROR

    monkeypatch.setattr(
        scripts.opencli_extractor,
        "get_article_content",
        lambda url, session_name: requested_sessions.append(session_name)
        or {"title": "", "content": "", "waf_detected": True},
    )
    try:
        client.get_article_content("https://xueqiu.com/1/1")
        raise AssertionError("expected SiteAccessError")
    except SiteAccessError as exc:
        assert exc.status == AttemptStatus.BLOCKED_WAF
    assert requested_sessions == [client.session_name]

    closed_sessions = []
    monkeypatch.setattr(
        scripts.opencli_extractor,
        "close_session",
        lambda session_name: closed_sessions.append(session_name),
    )
    client.close()
    assert closed_sessions == [client.session_name]


def test_opencli_waf_retry_success_is_not_marked_as_waf(monkeypatch):
    # 本用例钉的是**回退路径**（browser 直连 open/get/extract 三步）：必须把适配器
    # 探测钉成不可用，否则会走 opencli web article 分支，_run 的参数序列完全不同。
    monkeypatch.setattr(
        scripts.opencli_extractor, "is_article_available", lambda: False
    )
    outputs = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="Title - 雪球", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"content": "WAF"}), stderr=""
            ),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="Title - 雪球", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"content": "Good body"}), stderr=""
            ),
        ]
    )
    monkeypatch.setattr(
        scripts.opencli_extractor, "_run", lambda *a, **kw: next(outputs)
    )
    monkeypatch.setattr(
        scripts.opencli_extractor,
        "_is_error_page",
        lambda content: content == "WAF",
    )
    monkeypatch.setattr(scripts.opencli_extractor.time, "sleep", lambda seconds: None)

    detail = scripts.opencli_extractor.get_article_content(
        "https://xueqiu.com/5739488179/1",
        max_retries=1,
    )

    assert detail["content"] == "Good body"
    assert detail["waf_detected"] is False


def test_xueqiu_nodriver_adapter_maps_legacy_results():
    success_adapter = XueqiuNodriverAdapter(
        runner=lambda user_id, max_articles, data_dir: {
            "new_articles": 2,
            "saved_articles": 2,
        }
    )
    waf_adapter = XueqiuNodriverAdapter(
        runner=lambda user_id, max_articles, data_dir: {"error": "waf_blocked"}
    )
    timeline_adapter = XueqiuNodriverAdapter(
        runner=lambda user_id, max_articles, data_dir: {"error": "timeline_not_found"}
    )
    missing_adapter = XueqiuNodriverAdapter(
        runner=lambda user_id, max_articles, data_dir: {
            "error": "account_not_found:missing"
        }
    )

    assert success_adapter.execute(TASK, "nodriver").status == AttemptStatus.SUCCESS
    assert waf_adapter.execute(TASK, "nodriver").status == AttemptStatus.BLOCKED_WAF
    assert (
        timeline_adapter.execute(TASK, "nodriver").status == AttemptStatus.BLOCKED_WAF
    )
    assert missing_adapter.execute(TASK, "nodriver").status == AttemptStatus.SKIPPED
    assert success_adapter.execute(TASK, "opencli").status == AttemptStatus.SKIPPED


def test_nodriver_waf_triggered_is_a_failure_not_a_success():
    """传统爬虫撞 WAF 时只置 `waf_triggered`、不设 `error`，适配器必须认它.

    回归（2026-10-09 评审发现）：原来这里只看 `result.get("error")`，于是被拦的
    一轮落进 SUCCESS/NO_UPDATE 分支 ——
      ① 把 WAF 轮报成「N/N 成功、新增 0 篇」（`.last_crawl_stats.json` 的静默盲区）；
      ② 走 SUCCESS_STATUSES 把熔断器的硬失败计数与复犯阶梯一起清零，
         于是账号级失败永远攒不满阈值、冷却递进也生效不了。
    """

    def runner(**extra):
        def _run(user_id, max_articles, data_dir):
            base = {"new_articles": 0, "saved_articles": 0, "waf_triggered": True}
            base.update(extra)
            return base

        return XueqiuNodriverAdapter(runner=_run)

    detail = runner(waf_scope="detail", saved_articles=1).execute(TASK, "nodriver")
    account = runner(waf_scope="account").execute(TASK, "nodriver")
    unlabelled = runner().execute(TASK, "nodriver")  # 老返回，没标注范围

    assert detail.status == AttemptStatus.BLOCKED_WAF
    assert detail.scope is AttemptScope.DETAIL
    assert detail.saved_articles == 1
    assert detail.status not in SUCCESS_STATUSES

    assert account.status == AttemptStatus.BLOCKED_WAF
    assert account.scope is AttemptScope.ACCOUNT
    # 没标注的老返回按账号级处理（保守：宁可多停一次）
    assert unlabelled.status == AttemptStatus.BLOCKED_WAF
    assert unlabelled.scope is AttemptScope.ACCOUNT


def test_xueqiu_backend_dispatches_by_configured_backend(tmp_path):
    opencli_adapter = make_adapter(tmp_path, FakeClient([]))
    nodriver_adapter = XueqiuNodriverAdapter(
        runner=lambda user_id, max_articles, data_dir: {"error": "waf_blocked"}
    )
    backend = XueqiuBackend(opencli=opencli_adapter, nodriver=nodriver_adapter)

    opencli_result = backend.execute(TASK, "opencli")
    nodriver_result = backend.execute(TASK, "nodriver")
    unknown_result = backend.execute(TASK, "browser")

    assert opencli_result.status == AttemptStatus.NO_UPDATE
    assert nodriver_result.status == AttemptStatus.BLOCKED_WAF
    assert unknown_result.status == AttemptStatus.SKIPPED


# ── 标题可用性（站点栏目 / 列表占位符） ────────────────────────────────────
# 详情页没渲染出标题时 `document.title` 会退回站点栏目，它是**非空**的，
# 于是 `detail.get("title") or article.get("title","")` 不会回退 —— 与 crawler
# 侧是同一个洞（见 scripts/title_guard.py）。


def test_chrome_detail_title_falls_back_to_the_list_title(tmp_path):
    client = FakeClient(
        [article("1")],
        [{"title": "雪球-聪明的投资者都在这里", "content": "正文" * 50}],
    )
    adapter = make_adapter(tmp_path, client)

    result = adapter.execute(TASK, "opencli")

    assert result.status == AttemptStatus.SUCCESS
    markdown = (tmp_path / "data" / USER_ID / "1.md").read_text(encoding="utf-8")
    assert markdown.startswith("# List title 1"), "必须退回列表标题"
    assert "雪球-聪明的投资者都在这里" not in markdown


def test_article_with_no_usable_title_is_not_stored(tmp_path):
    """列表标题也是占位符 → 不入库，而且**不能**判成 PARSE_ERROR.

    `saw_empty_content` 的意思是「详情抓空了，账号可能有问题」；一篇图片帖
    不该把整个账号拖成 PARSE_ERROR。
    """
    client = FakeClient(
        [article("1")],
        [{"title": "雪球-聪明的投资者都在这里", "content": "来源：雪球App"}],
    )
    client.articles[0]["title"] = "展开\ue63c"
    adapter = make_adapter(tmp_path, client)

    result = adapter.execute(TASK, "opencli")

    assert result.status == AttemptStatus.NO_UPDATE
    assert result.saved_articles == 0
    assert not (tmp_path / "data" / USER_ID / "1.md").exists()


def test_normal_title_is_unaffected(tmp_path):
    """对照：标题正常时照旧落盘 —— 证明上面拦的是标题本身，不是顺手拦了所有文章."""
    client = FakeClient([article("1")], [{"title": "真标题", "content": "正文" * 50}])
    adapter = make_adapter(tmp_path, client)

    assert adapter.execute(TASK, "opencli").status == AttemptStatus.SUCCESS
    markdown = (tmp_path / "data" / USER_ID / "1.md").read_text(encoding="utf-8")
    assert markdown.startswith("# 真标题")
