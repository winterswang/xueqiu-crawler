from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from scripts.crawler_nodriver import XueqiuCrawlerNodriver


def test_opencli_mode_probes_at_most_three_non_columns(monkeypatch, tmp_path):
    crawler = object.__new__(XueqiuCrawlerNodriver)
    crawler.logger = MagicMock()
    crawler._opencli = object()
    crawler.index = {"articles": {}}
    crawler.data_dir = tmp_path
    crawler._get_history_article_ids = lambda _user_id: set()
    saved_histories = []
    crawler._save_history = lambda user_id, articles: saved_histories.append(
        (user_id, articles)
    )
    attempted = []

    def extract(article, _user_id, _user_name):
        attempted.append(article["article_id"])
        return bool(article.get("is_column"))

    crawler._extract_and_save_opencli = extract
    articles = [
        {
            "article_id": f"col-{i}",
            "url": f"https://xueqiu.com/1/col{i}",
            "is_column": True,
        }
        for i in range(2)
    ] + [
        {
            "article_id": f"other-{i}",
            "url": f"https://xueqiu.com/1/other{i}",
            "is_column": False,
        }
        for i in range(10)
    ]
    monkeypatch.setattr(
        "scripts.crawler_nodriver._opencli_get_list",
        lambda _user_id, count: articles[:count],
    )

    result = asyncio.run(
        crawler._crawl_one_user_opencli({"id": "1", "name": "test"}, 20)
    )

    assert attempted == ["col-0", "col-1", "other-0", "other-1", "other-2"]
    assert result["saved_articles"] == 2
    assert saved_histories[0][1][0]["article_id"] == "col-0"
