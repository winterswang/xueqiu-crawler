from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from scripts.crawler_nodriver import XueqiuCrawlerNodriver


def test_missing_timeline_is_an_error_not_an_empty_success():
    crawler = object.__new__(XueqiuCrawlerNodriver)
    crawler.logger = MagicMock()
    crawler._wait_for_selector = MagicMock(return_value=asyncio.sleep(0, False))

    result = asyncio.run(crawler._parse_article_list("5739488179"))

    assert result is None
