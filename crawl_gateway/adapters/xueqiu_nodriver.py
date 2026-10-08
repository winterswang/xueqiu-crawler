from __future__ import annotations

from pathlib import Path
from typing import Protocol

from crawl_gateway.models import AttemptResult, AttemptScope, AttemptStatus
from crawl_gateway.orchestrator import TaskSpec


class LegacyNodriverRunner(Protocol):
    def __call__(self, user_id: str, max_articles: int, data_dir: str | Path) -> dict:
        pass


class XueqiuNodriverAdapter:
    def __init__(
        self,
        *,
        runner: LegacyNodriverRunner | None = None,
        max_articles: int = 20,
    ) -> None:
        self._runner = runner or _run_legacy_crawler
        self._max_articles = max_articles

    def execute(self, task: TaskSpec, backend: str) -> AttemptResult:
        if backend != "nodriver":
            return AttemptResult(
                AttemptStatus.SKIPPED,
                backend,
                error="backend_not_supported_by_xueqiu_nodriver_adapter",
            )
        if task.resource_type != "user_timeline":
            return AttemptResult(
                AttemptStatus.SKIPPED,
                backend,
                error=f"unsupported_resource_type:{task.resource_type}",
            )
        try:
            result = self._runner(
                task.resource_id, self._max_articles, _default_data_dir()
            )
        except Exception as exc:
            return AttemptResult(
                AttemptStatus.NETWORK_ERROR,
                backend,
                error=f"nodriver_exception:{exc}",
            )

        error = str(result.get("error", ""))
        saved_articles = int(result.get("saved_articles", 0))
        new_articles = int(result.get("new_articles", saved_articles))

        # 传统爬虫撞 WAF 时只置 `waf_triggered`、**不设 `error`**（见
        # crawler_nodriver 的两处 `except WafDetectedError`）。这里原来只看
        # `error`，于是被拦的一轮直接落进下面的 SUCCESS/NO_UPDATE 分支：
        #   ① 把 WAF 轮报成「N/N 成功、新增 0 篇」——正是要消掉的那个盲区；
        #   ② 看走 SUCCESS_STATUSES，把熔断器的硬失败计数与复犯阶梯一起清零，
        #      于是账号级失败永远攒不满阈值、递进也生效不了。
        # waf_scope 由爬虫标注：detail=只是某篇正文被拦，account=时间线都拿不到。
        if result.get("waf_triggered"):
            scope = str(result.get("waf_scope") or "")
            return AttemptResult(
                AttemptStatus.BLOCKED_WAF,
                backend,
                error=f"waf_triggered:{scope or 'unspecified'}",
                new_articles=new_articles,
                saved_articles=saved_articles,
                # 没标注的老返回按账号级处理（保守：宁可多停一次）
                scope=(
                    AttemptScope.DETAIL if scope == "detail" else AttemptScope.ACCOUNT
                ),
            )

        if not error:
            return AttemptResult(
                AttemptStatus.SUCCESS if saved_articles else AttemptStatus.NO_UPDATE,
                backend,
                new_articles=new_articles,
                saved_articles=saved_articles,
            )
        if error == "waf_blocked":
            return AttemptResult(
                AttemptStatus.BLOCKED_WAF,
                backend,
                error=error,
                new_articles=new_articles,
                saved_articles=saved_articles,
            )
        if error == "timeline_not_found":
            return AttemptResult(
                AttemptStatus.BLOCKED_WAF,
                backend,
                error=error,
                new_articles=new_articles,
                saved_articles=saved_articles,
            )
        if error.startswith("account_not_found:"):
            return AttemptResult(
                AttemptStatus.SKIPPED,
                backend,
                error=error,
            )
        return AttemptResult(
            AttemptStatus.NETWORK_ERROR,
            backend,
            error=error,
            new_articles=new_articles,
            saved_articles=saved_articles,
        )


def _default_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data"


def _run_legacy_crawler(user_id: str, max_articles: int, data_dir: str | Path) -> dict:
    import asyncio

    from scripts.crawler_nodriver import XueqiuCrawlerNodriver

    crawler = XueqiuCrawlerNodriver(force_nodriver=True)
    crawler._use_opencli = False
    crawler._opencli = None
    crawler.data_dir = Path(data_dir).resolve()
    crawler.index_file = crawler.data_dir / "index.json"
    crawler.index = crawler._load_index()
    account = next(
        (account for account in crawler.accounts if str(account.get("id")) == user_id),
        None,
    )
    if account is None:
        return {"error": f"account_not_found:{user_id}"}

    async def run() -> dict:
        await crawler._start_browser()
        await crawler._warmup()
        await crawler._inject_cookies()
        try:
            return await crawler._crawl_one_user(account, max_articles)
        finally:
            await crawler._close_browser()

    return asyncio.run(run())
