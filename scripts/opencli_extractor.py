#!/usr/bin/env python3
"""
OpenCLI-based article extractor for xueqiu-crawler.

Uses opencli (https://github.com/jackwener/opencli) Chrome extension +
browser bridge to fetch xueqiu articles with zero WAF issues.
Falls back gracefully when opencli is not available.

Requirements:
    - opencli installed: npm i -g @jackwener/opencli
    - Chrome extension connected (opencli doctor)
    - xueqiu adapter ejected and user-articles command present
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

BROWSER_SESSION_PREFIX = "xq-crawler"


def is_available() -> bool:
    """Check if opencli is installed and the Chrome extension is connected."""
    if not shutil.which("opencli"):
        return False
    try:
        result = subprocess.run(
            ["opencli", "doctor"],
            capture_output=True, text=True, timeout=10,
        )
        return "[OK] Extension: connected" in result.stdout
    except Exception:
        return False


def _run(*args: str, timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess:
    """Run opencli command, suppressing stderr noise."""
    cmd = ["opencli"] + list(args)
    logger.debug(f"opencli: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"opencli failed: {result.stderr.strip()[:200]}")
    return result


def _clean_output(stdout: str) -> str:
    """Strip opencli update-notice noise from stdout."""
    lines = stdout.splitlines()
    cleaned = []
    skip = False
    for line in lines:
        if "Update available" in line:
            skip = True
            continue
        if skip and line.startswith("  Run:"):
            skip = False
            continue
        if not skip:
            cleaned.append(line)
    return "\n".join(cleaned)


def get_user_articles(user_id: str, count: int = 20) -> list[dict]:
    """Fetch article list for a xueqiu user via opencli adapter.

    Returns list of dicts with keys: article_id, title, author, time,
    likes, replies, url, text.
    """
    result = _run(
        "xueqiu", "user-articles",
        "--user_id", str(user_id),
        "--count", str(count),
        "-f", "json",
        timeout=30,
        check=True,
    )
    cleaned = _clean_output(result.stdout)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.error(f"Failed to parse user-articles JSON for {user_id}")
        return []


def get_article_content(url: str, session_name: str = "xq-crawler") -> dict:
    """Extract full article content from a xueqiu article URL.

    Returns dict with keys: url, title, content (markdown).
    """
    result = {"url": url, "title": "", "content": ""}

    # Open article page in browser session
    open_result = _run("browser", session_name, "open", url, timeout=30)
    if open_result.returncode != 0:
        logger.error(f"Browser open failed: {open_result.stderr.strip()[:200]}")
        return result

    # Wait for page to render
    time.sleep(4)

    # Get title
    title_result = _run("browser", session_name, "get", "title", timeout=10)
    if title_result.returncode == 0:
        raw_title = title_result.stdout.strip()
        # Strip " - 雪球" suffix
        result["title"] = re.sub(r"\s*[-–—]\s*雪球\s*$", "", raw_title).strip()

    # Extract markdown content
    extract_result = _run("browser", session_name, "extract", timeout=15)
    if extract_result.returncode == 0:
        cleaned = _clean_output(extract_result.stdout)
        try:
            data = json.loads(cleaned)
            result["content"] = data.get("content", "")
            result["title"] = result["title"] or data.get("title", "").replace(" - 雪球", "")
        except json.JSONDecodeError:
            logger.error(f"Failed to parse extract JSON for {url}")

    return result


def close_session(session_name: str = "xq-crawler"):
    """Release browser session."""
    try:
        _run("browser", session_name, "close", timeout=10)
    except Exception:
        pass


class OpencliExtractor:
    """High-level extractor that wraps opencli for the crawler."""

    def __init__(self):
        self._session_name = f"{BROWSER_SESSION_PREFIX}-{os.getpid()}"

    def get_user_articles(self, user_id: str, count: int = 20) -> list[dict]:
        return get_user_articles(user_id, count)

    def get_article_content(self, url: str) -> dict:
        result = get_article_content(url, self._session_name)
        return {
            "url": result["url"],
            "title": result["title"],
            "author": "",
            "publish_time": "",
            "content": result["content"],
            "likes": 0,
            "comments": 0,
            "is_column": bool(result["content"]),
        }

    def close(self):
        close_session(self._session_name)
