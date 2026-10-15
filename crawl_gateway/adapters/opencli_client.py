from __future__ import annotations

import os
from typing import Any

from crawl_gateway.adapters.xueqiu import SiteAccessError
from crawl_gateway.models import AttemptStatus


def _in_cooldown() -> bool:
    """xueqiu 是否处于插件层的熔断冷却中（读不到 / 装不上 analyzer 时当「否」）。"""
    try:
        from xueqiu_analyzer.opencli_cooldown import in_cooldown

        return in_cooldown()
    except Exception:  # noqa: BLE001 —— 这条判断只是优化，失败必须退化成原行为
        return False


class OpencliArticleClient:
    def __init__(self) -> None:
        self.session_name = f"xq-crawler-gateway-{os.getpid()}"

    def list_user_articles(self, user_id: str, count: int) -> list[dict]:
        from scripts.opencli_extractor import WafBlockedError, get_user_articles

        try:
            articles = get_user_articles(user_id, count)
        except WafBlockedError as exc:
            # 列表页都被风控拦住 = 站点对这个账号不可达 → 账号级硬失败，
            # 要能计入熔断（http_error 不在 hard_failure_statuses 里）。
            raise SiteAccessError(
                AttemptStatus.BLOCKED_WAF,
                f"opencli user articles blocked by waf for {user_id}: {exc}",
            ) from exc
        except Exception as exc:
            raise SiteAccessError(
                AttemptStatus.NETWORK_ERROR,
                f"opencli user articles exception for {user_id}: {exc}",
            ) from exc
        if articles is None:
            if _in_cooldown():
                # 熔断器正封着这个站点：这是**我们主动停手**，不是网络/接口故障。
                # 用 CIRCUIT_OPEN（它**不在** FAILURE_STATUSES 里）→ 编排器不会换后端。
                # 若报 HTTP_ERROR（默认 ACCOUNT 级、且在 FAILURE_STATUSES 里），
                # `backend_priority: [opencli, nodriver]` 会**回退到 nodriver 真开浏览器
                # 打站点** —— 冷却期内那正是要避免的事。
                raise SiteAccessError(
                    AttemptStatus.CIRCUIT_OPEN,
                    f"opencli user articles skipped for {user_id}: 熔断冷却中，未发出请求",
                )
            raise SiteAccessError(
                AttemptStatus.HTTP_ERROR,
                f"opencli user articles failed for {user_id}",
            )
        return articles

    def get_article_content(self, url: str) -> dict[str, Any]:
        from scripts.opencli_extractor import get_article_content

        detail = get_article_content(url, self.session_name)
        if detail.get("waf_detected"):
            raise SiteAccessError(
                AttemptStatus.BLOCKED_WAF,
                f"opencli article content blocked: {url}",
            )
        return detail

    def close(self) -> None:
        from scripts.opencli_extractor import close_session

        close_session(self.session_name)
