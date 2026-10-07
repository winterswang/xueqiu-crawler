from __future__ import annotations

import os
from typing import Any

from crawl_gateway.adapters.xueqiu import SiteAccessError
from crawl_gateway.models import AttemptStatus


class OpencliArticleClient:
    def __init__(self) -> None:
        self.session_name = f"xq-crawler-gateway-{os.getpid()}"

    def list_user_articles(self, user_id: str, count: int) -> list[dict]:
        from scripts.opencli_extractor import get_user_articles

        try:
            articles = get_user_articles(user_id, count)
        except Exception as exc:
            raise SiteAccessError(
                AttemptStatus.NETWORK_ERROR,
                f"opencli user articles exception for {user_id}: {exc}",
            ) from exc
        if articles is None:
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
