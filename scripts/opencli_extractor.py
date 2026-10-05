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
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 风控页判定统一走 xueqiu_analyzer.waf（唯一实现，见 scripts/waf_bridge.py）。
# 这里原先自己维护一份 _ERROR_PAGE_PATTERNS：比 crawler_nodriver 那份少两项
# （滑动验证 / 请按住滑块）、多一项 "405"，而且 "405" 是当正文子串匹配的 ——
# 任何开头 500 字里出现 405 的正常文章都会被误判成错误页并重试。
from scripts.waf_bridge import looks_like_waf_content  # noqa: E402
from xueqiu_analyzer.opencli_call_logger import record_opencli_call  # noqa: E402
from xueqiu_analyzer.opencli_rate_limiter import acquire_opencli_slot  # noqa: E402

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


def is_user_articles_available() -> bool:
    """Check command registration locally without visiting xueqiu.com."""
    try:
        result = subprocess.run(
            ["opencli", "xueqiu", "user-articles", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0 and "user-articles" in result.stdout


def _run(*args: str, timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess:
    """Run opencli command, suppressing stderr noise."""
    cmd = ["opencli"] + list(args)
    logger.debug(f"opencli: {' '.join(cmd)}")
    source = os.environ.get("XUEQIU_CALL_SOURCE", "xueqiu-crawler:opencli_extractor")
    slot = acquire_opencli_slot(" ".join(args[:2]), cmd)
    started_at = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        record_opencli_call(
            cmd, started_at, error=exc, source=source, caller="_run", throttle=slot
        )
        raise
    record_opencli_call(
        cmd, started_at, result=result, source=source, caller="_run", throttle=slot
    )
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
    try:
        result = _run(
            "xueqiu", "user-articles",
            "--user_id", str(user_id),
            "--count", str(count),
            "-f", "json",
            timeout=30,
            check=True,
        )
    except RuntimeError as e:
        logger.error(f"opencli user-articles command failed: {e}")
        return None
    cleaned = _clean_output(result.stdout)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.error(f"Failed to parse user-articles JSON for {user_id}")
        return []


def get_article_content(url: str, session_name: str = "xq-crawler", max_retries: int = 2) -> dict:
    """Extract full article content from a xueqiu article URL.

    Returns dict with keys: url, title, content (markdown).
    If the page is a WAF/error page, retries up to max_retries times.
    """
    result = {"url": url, "title": "", "content": ""}

    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.warning(f"Retry {attempt}/{max_retries} for {url[-30:]}")
            time.sleep(3)

        # Open article page in browser session
        open_result = _run("browser", session_name, "open", url, timeout=30)
        if open_result.returncode != 0:
            logger.error(f"Browser open failed: {open_result.stderr.strip()[:200]}")
            continue

        # Wait for page to render
        time.sleep(4)

        # Get title
        title_result = _run("browser", session_name, "get", "title", timeout=10)
        if title_result.returncode == 0:
            raw_title = title_result.stdout.strip()
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
                continue

        # Check if content is an error page
        if _is_error_page(result["content"]):
            logger.warning(f"Error page detected for {url[-30:]} (attempt {attempt+1})")
            result["content"] = ""
            result["title"] = ""
            continue

        # Success — content looks good
        break

    return result


def _is_error_page(content: str) -> bool:
    """Check if extracted content is a WAF/error page.

    实现见 xueqiu_analyzer.waf.looks_like_waf_content —— 只有正文、没有可信标题，
    且沿用原有的「短于 100 字不判」规则（那是「没取到内容」，不是风控）。
    """
    return looks_like_waf_content(content)


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
