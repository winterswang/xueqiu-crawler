#!/usr/bin/env python3
"""
OpenCLI-based article extractor for xueqiu-crawler.

Uses opencli (https://github.com/jackwener/opencli) Chrome extension +
browser bridge to fetch xueqiu articles with zero WAF issues.
Falls back gracefully when opencli is not available.

Requirements:
    - opencli installed: npm i -g @jackwener/opencli
    - Chrome extension connected (opencli doctor)
    - xueqiu-adapters plugin installed (`opencli-adapters/install.sh`)
"""

import functools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

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


class WafBlockedError(RuntimeError):
    """opencli 适配器撞上了风控页 —— 这不是「没有数据」.

    适配器的 BLOCK_GUARD 命中时会抛 CommandExecutionError（消息含「风控验证页」），
    opencli 以非 0 退出、消息进 stderr。列表命令这里原本把它和普通失败一起吞成
    None/[]，调用方只能映射成 http_error —— 而 http_error 不在
    `circuit_breaker.hard_failure_statuses` 里，于是「账号级」这个口径在 opencli
    路径上根本没有可达信号，熔断器攒不满。单独抛出来让调用方能正确分级。
    """


# 适配器 BLOCK_GUARD 命中时的消息（见 opencli-adapters/*.js 与
# scripts/sync_waf_patterns.py 的同源校验）。
_WAF_BLOCKED_MARKER = "风控验证页"


def is_available() -> bool:
    """Check if opencli is installed and the Chrome extension is connected."""
    if not shutil.which("opencli"):
        return False
    try:
        result = subprocess.run(
            ["opencli", "doctor"],
            capture_output=True,
            text=True,
            timeout=10,
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


@functools.lru_cache(maxsize=1)
def is_article_available() -> bool:
    """`opencli web article` 是否注册（本地探测，不发站点请求）。

    这是正文抓取的**主通道**（opencli-adapters/article.js）。进程内缓存 ——
    crawler 每篇文章都会问一次，不能每篇起一个子进程。
    测试里改过返回值后要 `is_article_available.cache_clear()`。
    """
    try:
        result = subprocess.run(
            ["opencli", "web", "article", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0 and "article" in result.stdout


def _run(
    *args: str, timeout: int = 30, check: bool = False
) -> subprocess.CompletedProcess:
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


# opencli 的升级提示在命令输出**之后**由退出钩子打印（update-check.js），
# 两种形态各占两行：
#     "  Update available: v1.8.8 → v1.8.9" / "  Run: npm install -g @jackwener/opencli"
#     "  Extension update available: ..."     / "  Download: https://..."
_UPDATE_NOTICE_HEADS = ("Update available", "Extension update available")
_UPDATE_NOTICE_COMMANDS = ("Run:", "Download:")


def _clean_output(stdout: str) -> str:
    """去掉 opencli 的升级提示.

    必须**按行首锚定**，不能按子串匹配 —— JSON 里正文是单独一行，正文中出现
    "Update available" 字样时，旧的子串实现会把那一行连同其后所有行一起丢掉，
    JSON 就此截断：`_attempt_via_adapter` 解析失败 → 三轮重试 → 最后返回
    waf_detected=False、content=""，**整篇文章静默丢失**。
    """
    kept: list[str] = []
    drop_command_line = False
    for line in stdout.splitlines():
        head = line.lstrip()
        if head.startswith(_UPDATE_NOTICE_HEADS):
            drop_command_line = True
            continue
        if drop_command_line and head.startswith(_UPDATE_NOTICE_COMMANDS):
            drop_command_line = False
            continue
        drop_command_line = False
        kept.append(line)
    return "\n".join(kept)


def get_user_articles(user_id: str, count: int = 20) -> list[dict]:
    """Fetch article list for a xueqiu user via opencli adapter.

    Returns list of dicts with keys: article_id, title, author, time,
    likes, replies, url, text.
    """
    try:
        result = _run(
            "xueqiu",
            "user-articles",
            "--user_id",
            str(user_id),
            "--count",
            str(count),
            "-f",
            "json",
            timeout=30,
            check=True,
        )
    except RuntimeError as e:
        # 撞风控要能被调用方认出来（列表都拿不到 = 账号级失败），不能和普通失败
        # 一起塌成 None —— None 在 gateway 侧被映射成 http_error，不在硬失败名单里。
        if _WAF_BLOCKED_MARKER in str(e):
            logger.warning(f"opencli user-articles 撞风控页: {user_id}")
            raise WafBlockedError(str(e)) from e
        logger.error(f"opencli user-articles command failed: {e}")
        return None
    cleaned = _clean_output(result.stdout)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.error(f"Failed to parse user-articles JSON for {user_id}")
        return []


def get_article_content(
    url: str, session_name: str = "xq-crawler", max_retries: int = 2
) -> dict:
    """Extract full article content from an article URL.

    Returns dict with keys: url, title, content (markdown), waf_detected.
    主通道是 `opencli web article`（见 opencli-adapters/article.js）；该命令未注册
    （插件丢失）时回退到旧的 `opencli browser open/get/extract` 直连序列。
    If the page is a WAF/error page, retries up to max_retries times.

    两条通道的正文长度不完全相同：`browser extract` 按 20000 字分块且只返回第一块，
    旧直连路径对超过约 2 万字的文章是**截断**的（信封里的 next_start_char 从没被续读）；
    适配器一次返回全文。短文两路逐字相同。
    """
    result = {"url": url, "title": "", "content": ""}
    waf_detected = False
    use_adapter = is_article_available()

    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.warning(f"Retry {attempt}/{max_retries} for {url[-30:]}")
            time.sleep(3)

        attempt_result = (
            _attempt_via_adapter(url)
            if use_adapter
            else _attempt_via_browser(url, session_name)
        )
        if attempt_result is None:  # 本轮失败，值得再试
            continue

        result["title"] = attempt_result["title"]
        result["content"] = attempt_result["content"]

        # 适配器撞风控会直接给出 waf_detected；直连路径没有这个信号，只能看正文。
        # 两条路都再过一次 _is_error_page（短于 100 字不判 —— 那是"没取到"不是风控）。
        if attempt_result["waf_detected"] or _is_error_page(result["content"]):
            logger.warning(
                f"Error page detected for {url[-30:]} (attempt {attempt + 1})"
            )
            waf_detected = True
            result["content"] = ""
            result["title"] = ""
            continue

        # Success — content looks good
        waf_detected = False
        break

    return {**result, "waf_detected": waf_detected}


def _clean_title(raw: str) -> str:
    """去掉雪球标题尾缀（原文与适配器通道共用同一条规则）。"""
    return re.sub(r"\s*[-–—]\s*雪球\s*$", "", raw).strip()


def _attempt_via_adapter(url: str) -> dict | None:
    """一次 `opencli web article` 调用。返回 None = 本轮失败、值得重试。

    风控用专属错误码 BLOCKED_WAF 报出来（见 article.js 的注释），只出现在 stderr；
    不能把它和普通抓取失败混为一谈，否则调用方会去重试一个需要人工过滑块的状态。
    """
    try:
        call = _run("web", "article", url, "-f", "json", timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        # 这条命令把「导航 + 渲染等待 + 最多 8 个选择器的抽取 + markdown 转换」
        # 压在一次调用里，旧路径是三步各有各的预算（30/10/15 秒）。超时必须
        # 在**这里**吃掉：_run 会原样重抛，而上游 crawl_gateway 只把
        # SiteAccessError 认作站点异常 —— 裸异常会打断整个账号的文章循环。
        logger.warning(f"web article 超时/失败: {url[-40:]} {exc}")
        return None
    if call.returncode != 0:
        stderr = call.stderr or ""
        if "BLOCKED_WAF" in stderr:
            return {"url": url, "title": "", "content": "", "waf_detected": True}
        logger.error(f"web article failed: {stderr.strip()[:200]}")
        return None

    try:
        rows = json.loads(_clean_output(call.stdout))
    except json.JSONDecodeError:
        logger.error(f"Failed to parse web article JSON for {url}")
        return None
    row = rows[0] if isinstance(rows, list) and rows else {}
    return {
        "url": url,
        "title": _clean_title(str(row.get("title") or "")),
        "content": row.get("content") or "",
        "waf_detected": False,
    }


def _attempt_via_browser(url: str, session_name: str) -> dict | None:
    """回退路径：旧的 opencli browser open/get/extract 直连。None = open 失败。

    只在 `opencli web article` 未注册时才走到这里。序列、超时、等待时长都与引入
    适配器之前逐字一致 —— 留着它是为了插件丢失时还能抓，不是为了日常使用。
    """
    open_result = _run("browser", session_name, "open", url, timeout=30)
    if open_result.returncode != 0:
        logger.error(f"Browser open failed: {open_result.stderr.strip()[:200]}")
        return None

    # Wait for page to render
    time.sleep(4)

    title = ""
    title_result = _run("browser", session_name, "get", "title", timeout=10)
    if title_result.returncode == 0:
        title = _clean_title(title_result.stdout.strip())

    content = ""
    extract_result = _run("browser", session_name, "extract", timeout=15)
    if extract_result.returncode == 0:
        try:
            data = json.loads(_clean_output(extract_result.stdout))
        except json.JSONDecodeError:
            logger.error(f"Failed to parse extract JSON for {url}")
            return None
        content = data.get("content", "")
        title = title or _clean_title(data.get("title", ""))

    return {"url": url, "title": title, "content": content, "waf_detected": False}


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
