"""防回归：(已删除的) OpenCLI「失败账号」机制不得复活。

2026-10-05 的早报巡检里，一条 "均被 WAF 拦截" 日志把排障带偏一轮：OpenCLI 模式
17/17 全部成功、零 WAF，却写下 11 个"失败账号"。根因是旧启发式
`saved_articles == 0 and new_articles_available > 0` —— 而保存 0 篇在 OpenCLI
下是常态（新帖大量是 `回复@` 回复帖、无正文、重复已存在）。

该机制整体是死代码（--retry-failed 无任何调度调用），已随 PR 删除。这里守住两点：
1. 「有可用文章但保存 0 篇」不再产生任何失败账号记录；
2. --retry-failed 残留调用显式失败，而不是静默退化成全量运行。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

from scripts.crawler_nodriver import XueqiuCrawlerNodriver

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_opencli_crawler(tmp_path: Path) -> XueqiuCrawlerNodriver:
    """构造一个只走 OpenCLI 分支的实例（不启动浏览器）。"""
    crawler = object.__new__(XueqiuCrawlerNodriver)
    crawler.logger = MagicMock()
    crawler.config = {"crawler": {"max_articles": 20}}
    crawler.accounts = [{"id": "1", "name": "tester"}]
    crawler._use_opencli = True
    crawler._opencli = MagicMock()
    crawler.data_dir = tmp_path
    return crawler


def test_zero_saved_articles_writes_no_failed_accounts(tmp_path, monkeypatch):
    """「有 20 篇可用、保存 0 篇」是回复帖常态，不该产生失败账号文件或 WAF 日志。"""
    crawler = _make_opencli_crawler(tmp_path)

    async def fake_crawl(account, max_articles):
        return {
            "user_id": account["id"],
            "name": account["name"],
            "new_articles": 0,
            "saved_articles": 0,
            "new_articles_available": 20,  # 旧启发式的触发条件
        }

    monkeypatch.setattr(crawler, "_crawl_one_user_opencli", fake_crawl)

    stats = asyncio.run(crawler.crawl_all_users(max_articles=20))

    assert stats["total_new"] == 0
    assert not (tmp_path / ".failed_accounts.json").exists()
    logged = " ".join(str(call) for call in crawler.logger.info.call_args_list)
    assert "WAF" not in logged


def test_cli_rejects_retry_failed():
    """--retry-failed 已移除；残留调用方必须显式报错，不得静默跑全量。"""
    proc = subprocess.run(
        [sys.executable, "scripts/crawler_nodriver.py", "--retry-failed"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert "retry-failed" in proc.stderr
