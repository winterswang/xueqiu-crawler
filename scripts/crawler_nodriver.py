#!/usr/bin/env python3
"""
雪球专栏文章爬虫 — nodriver 版本 (async)
绕过阿里云 WAF 滑动验证，替代 Playwright 实现

架构: 复用 XueliuCrawler 的业务逻辑，替换底层浏览器为 nodriver
"""

import os
import sys
import json
import yaml
import random
import logging
import hashlib
import re
import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

_dotenv_path = Path(__file__).resolve().parent.parent / '.env'
if _dotenv_path.exists():
    load_dotenv(_dotenv_path, override=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

# 风控页判定的唯一实现（analyzer 的 xueqiu_analyzer.waf）。
# 导入失败会当场 SystemExit —— 见 scripts/waf_bridge.py 的说明。
from scripts.waf_bridge import (  # noqa: E402
    contains_waf_text,
    has_waf_marker,
    is_error_page,
)

# 标题可用性（站点栏目 / 列表占位符不算标题）。与风控判定分开，见 title_guard.py。
from scripts.title_guard import resolve_title  # noqa: E402

import nodriver as uc

# OpenCLI fallback (zero-WAF via Chrome extension)
try:
    from scripts.opencli_extractor import is_available as _opencli_available
    from scripts.opencli_extractor import OpencliExtractor
    from scripts.opencli_extractor import get_user_articles as _opencli_get_list
    from scripts.opencli_extractor import WafBlockedError as _WafBlockedError

    _HAS_OPENCLI = True
except ImportError:
    _HAS_OPENCLI = False
    _WafBlockedError = None

    def _opencli_available() -> bool:
        return False


class WafDetectedError(Exception):
    """Raised when the browser hits a WAF block (slider or redirect)."""

    pass


# 风控页判定统一走 xueqiu_analyzer.waf（唯一实现，见 scripts/waf_bridge.py）。
# 这里原先有两套互不相同的模式表：本文件的 _ERROR_CONTENT_PATTERNS 与
# opencli_extractor 的 _ERROR_PAGE_PATTERNS —— 大小写行为还不一样。
_is_content_error = is_error_page

# 默认常量
DEFAULT_MAX_ARTICLES = 20
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 5]


def setup_logging(config: dict):
    """配置日志"""
    project_root = Path(__file__).parent.parent
    log_dir = project_root / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / 'crawler_nodriver.log'

    logging.basicConfig(
        level=getattr(logging, config.get('logging', {}).get('level', 'INFO')),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger(__name__)


class XueqiuCrawlerNodriver:
    """雪球爬虫 — nodriver 版本"""

    # 被 WAF 拦掉正文的篇数。类属性（不是 __init__ 里赋的）—— 测试会用
    # object.__new__ 绕过 __init__，放类上才不会 AttributeError。
    # 只在单个用户的爬取循环前后取差值；爬取是逐用户串行的，不会有并发写。
    _waf_blocked_articles = 0

    def __init__(self, config_path: str = None, force_nodriver: bool = False):
        self.project_root = Path(__file__).parent.parent
        self.config = self._load_config(config_path)
        self.logger = setup_logging(self.config)
        self.accounts = self._load_accounts()
        self.data_dir = self.project_root / self.config.get('storage', {}).get(
            'output_dir', 'data'
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.data_dir / 'index.json'
        self.index = self._load_index()
        self.timeout = self.config.get('crawler', {}).get('timeout', 60) * 1000
        self.browser = None
        self.tab = None

        # OpenCLI availability check
        # First: check binary and extension connection
        opencli_available = not force_nodriver and _HAS_OPENCLI and _opencli_available()
        self._use_opencli = False
        self._opencli = None
        if opencli_available:
            # Second: verify the command is registered. Use --help so the
            # preflight never spends a real xueqiu.com request or triggers WAF.
            try:
                from scripts.opencli_extractor import is_user_articles_available

                if is_user_articles_available():
                    self._use_opencli = True
                    self.logger.info("✅ OpenCLI 可用，启用 Chrome 扩展模式（零 WAF）")
                    self._opencli = OpencliExtractor()
                else:
                    self.logger.warning("⚠️ OpenCLI 预检失败，user-articles 命令不可用")
                    self.logger.warning("🔄 回退到 nodriver 模式")
            except Exception as e:
                self.logger.warning(
                    "⚠️ OpenCLI 预检失败，user-articles 命令不可用: %s", e
                )
                self.logger.warning("🔄 回退到 nodriver 模式")
        if not self._use_opencli:
            self.logger.info("ℹ️ OpenCLI 不可用，使用 nodriver 模式")

    # ============ Config/Index (同 Playwright 版本) ============

    def _load_config(self, config_path: str = None) -> dict:
        if config_path is None:
            config_path = self.project_root / 'config' / 'config.yaml'
        else:
            config_path = Path(config_path)
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        return {}

    def _load_accounts(self) -> List[dict]:
        accounts_path = self.project_root / 'config' / 'accounts.yaml'
        if accounts_path.exists():
            with open(accounts_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                return [a for a in data.get('accounts', []) if a.get('enabled', True)]
        return []

    def _load_index(self) -> dict:
        if self.index_file.exists():
            with open(self.index_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data.setdefault('articles', {})
            data.setdefault('last_update', None)
            data.setdefault('history', {})
            return data
        return {'articles': {}, 'last_update': None, 'history': {}}

    def _save_index(self):
        self.index['last_update'] = datetime.now().isoformat()
        tmp_path = self.index_file.with_suffix('.json.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(self.index, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.index_file)

    def _save_history(self, user_id: str, articles: List[dict]):
        history_dir = self.data_dir / 'history' / user_id
        history_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime('%Y-%m-%d')
        history_file = history_dir / f'{today}.json'
        history_data = {
            'date': today,
            'user_id': user_id,
            'article_count': len(articles),
            'articles': [
                {
                    'article_id': a.get('article_id'),
                    'title': a.get('title', '')[:50],
                    'crawl_time': a.get('crawl_time'),
                }
                for a in articles
            ],
        }
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        self.logger.info(f"历史快照已保存: {history_file}")

    def _get_history_article_ids(self, user_id: str) -> set:
        history_dir = self.data_dir / 'history' / user_id
        if not history_dir.exists():
            return set()
        article_ids = set()
        for history_file in sorted(history_dir.glob('*.json'), reverse=True)[:7]:
            with open(history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for article in data.get('articles', []):
                    article_ids.add(article.get('article_id'))
        return article_ids

    async def _random_delay(self):
        delay_min = self.config.get('crawler', {}).get('delay_min', 2)
        delay_max = self.config.get('crawler', {}).get('delay_max', 5)
        delay = random.uniform(delay_min, delay_max)
        self.logger.debug(f"等待 {delay:.1f} 秒...")
        await self.tab.sleep(delay) if self.tab else await asyncio.sleep(delay)

    # ============ nodriver 浏览器管理 ============

    async def _close_browser(self):
        """安全关闭浏览器"""
        if self.browser is None:
            return
        try:
            result = self.browser.stop()
            if result is not None and hasattr(result, '__await__'):
                await result
        except Exception as e:
            self.logger.debug(f"关闭浏览器异常(非关键): {e}")
        finally:
            self.browser = None
            self.tab = None

    async def _start_browser(self):
        """启动 nodriver 浏览器"""
        if self.browser is not None:
            return
        # macOS 上 Chrome 冷启动偶尔超过 nodriver 内置的 ~2.75s 连接窗口
        # （Linux 服务器上 Chrome 秒起无此问题），加重试兜底：
        # 首次尝试会预热 page cache/代码签名，重试基本必成。
        # 注意：这里必须抛普通 Exception 而非 RuntimeError —— main() 会静默吞掉 RuntimeError。
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                config = uc.Config(headless=True, sandbox=False)
                self.browser = await uc.start(config=config)
                self.logger.info("nodriver 浏览器已启动")
                return
            except Exception as e:
                self.logger.warning(
                    f"浏览器启动失败 (尝试 {attempt}/{max_attempts}): "
                    f"{type(e).__name__}: {str(e)[:100]}"
                )
                await self._close_browser()
                if attempt < max_attempts:
                    await asyncio.sleep(3)
        raise Exception(f"nodriver 浏览器启动失败（已重试 {max_attempts} 次）")

    async def _warmup(self):
        """访问首页预热会话"""
        self.tab = await self.browser.get("https://xueqiu.com")
        await self.tab.sleep(3)
        # 模拟人类：滚动
        await self.tab.evaluate("window.scrollBy(0, 300)")
        await self.tab.sleep(1)
        await self.tab.evaluate("window.scrollBy(0, -200)")
        self.logger.info("会话预热完成")

    async def _navigate(self, url: str, wait_seconds: float = 3):
        """导航到 URL"""
        self.tab = await self.browser.get(url)
        await self.tab.sleep(wait_seconds)

    async def _query_text(self, selector: str) -> Optional[str]:
        """获取元素文本"""
        try:
            result = await self.tab.evaluate(
                f"(function(){{ var el = document.querySelector('{selector}'); return el && el.innerText ? el.innerText.trim() : ''; }})()"
            )
            return result if result else None
        except Exception:
            return None

    async def _page_title(self) -> str:
        """获取页面标题"""
        return await self.tab.evaluate("document.title")

    async def _page_content(self) -> str:
        """获取页面完整 HTML"""
        return await self.tab.evaluate("document.documentElement.outerHTML")

    async def _detect_waf(self) -> bool:
        """检测是否触发了 WAF 验证（模式表见 xueqiu_analyzer.waf）。

        刻意不走 is_error_page：那条「空标题算错误页」的规则是为「过滤坏文章」
        设计的，用它来触发浏览器重启太激进 —— 一次没取到标题就重启不划算。
        """
        title = await self._page_title()
        content = await self._page_content()
        if (
            contains_waf_text(title)
            or contains_waf_text(content)
            or has_waf_marker(content)
        ):
            self.logger.warning("检测到 WAF 风控页（标题/正文/页面标记命中）")
            return True
        return False

    async def _wait_for_selector(self, selector: str, timeout_seconds: float = 15.0):
        """等待选择器出现"""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            count = await self.tab.evaluate(
                f"document.querySelectorAll('{selector}').length"
            )
            if count > 0:
                return True
            await self.tab.sleep(0.5)
        return False

    # ============ Cookie 管理 ============

    def _load_cookies_dict(self) -> dict:
        """从文件加载 cookies"""
        cookies_file = self.project_root / 'config' / 'xueqiu_cookies.json'
        if not cookies_file.exists():
            return {}
        try:
            with open(cookies_file, 'r') as f:
                data = json.load(f)
            return data.get('cookies', {})
        except Exception as e:
            self.logger.warning(f"加载 cookies 失败: {e}")
            return {}

    async def _inject_cookies(self):
        """注入 cookies（nodriver Chrome 自带 cookie jar，JS 注入仅限非 httpOnly）"""
        cookies = self._load_cookies_dict()
        if not cookies:
            return
        # nodriver 的 Chrome 使用真实浏览器 profile，cookie jar 已自动管理
        # JS 注入仅能设置非 httpOnly 的 cookie（如 acw_tc、设备 ID 等）
        try:
            cookie_pairs = '; '.join(
                f'{k}={v}'
                for k, v in cookies.items()
                if k not in ('xq_a_token', 'xq_r_token', 'xq_id_token', 'u')
            )
            if cookie_pairs:
                await self.tab.evaluate(
                    f"document.cookie = '{cookie_pairs}; domain=.xueqiu.com; path=/; SameSite=Lax'"
                )
        except Exception:
            pass
        self.logger.info(f"已注入 {len(cookies)} 个 cookies")

    # ============ 用户相关 ============

    async def _get_user_name(self, user_id: str) -> str:
        """从用户主页获取用户名"""
        try:
            name = await self._query_text('.user-name, .username, .profile__name')
            if name:
                self.logger.info(f"获取用户名: {name}")
                return name
            title = await self._page_title()
            if '的雪球专栏' in title:
                name = title.split('的雪球专栏')[0].strip()
                if name:
                    return name
        except Exception as e:
            self.logger.warning(f"获取用户名失败: {e}")
        return user_id

    def _update_account_name(self, user_id: str, name: str):
        """更新账号配置中的用户名"""
        try:
            accounts_path = self.project_root / 'config' / 'accounts.yaml'
            with open(accounts_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            for account in data.get('accounts', []):
                if account.get('id') == user_id:
                    if account.get('name') in ['待确认', user_id]:
                        account['name'] = name
                        self.logger.info(f"更新账号名称: {user_id} -> {name}")
                        with open(accounts_path, 'w', encoding='utf-8') as f:
                            yaml.dump(
                                data, f, default_flow_style=False, allow_unicode=True
                            )
                        return True
                    break
        except Exception as e:
            self.logger.warning(f"更新账号名称失败: {e}")
        return False

    # ============ 文章解析 ============

    def _extract_article_id(self, url: str) -> str:
        """从URL提取文章ID"""
        match = re.search(r'/\d+/(\d+)$', url)
        if match:
            return match.group(1)
        return hashlib.md5((url + str(time.time())).encode()).hexdigest()[:12]

    async def _parse_article_list(self, user_id: str) -> Optional[List[dict]]:
        """解析用户时间线，提取文章列表"""
        articles = []

        # 等待时间线加载
        found = await self._wait_for_selector('.timeline__item', timeout_seconds=15)
        if not found:
            self.logger.warning("未找到 .timeline__item (可能被WAF拦截)")
            return None

        # 获取所有时间线条目的关键数据 (JSON.stringify 解决 nodriver RemoteObject 序列化)
        # is_likely_column: 列表层专栏预判。雪球专栏正文容器带 content--longtext class，
        # 评论/短动态带 content--description。实测两类账号各 20 条 0 误判，可在列表层精准区分，
        # 用于让专栏优先占用抓取额度，避免评论刷屏挤掉真专栏（见 plan 2026-06-21）。
        items_json = await self.tab.evaluate("""
            JSON.stringify(Array.from(document.querySelectorAll('.timeline__item')).map(function(item, i) {
                var links = Array.from(item.querySelectorAll('a'))
                    .map(function(a) { return a.href; })
                    .filter(function(h) { return /\\/\\d+\\/\\d+$/.test(h) && h.indexOf('#comment') === -1; });
                var titleEl = item.querySelector('.content, .status-content');
                var timeEl = item.querySelector('.time, .date');
                var isColumn = !!item.querySelector('.timeline__item__content--longtext');
                return {
                    link: links[0] || '',
                    title: titleEl && titleEl.innerText ? titleEl.innerText.trim().split('\\n')[0].substring(0, 100) : '',
                    time: timeEl && timeEl.innerText ? timeEl.innerText.trim() : '',
                    is_likely_column: isColumn,
                    index: i
                };
            }))
        """)

        if not items_json or not isinstance(items_json, str):
            self.logger.warning(
                f"evaluate 未返回有效的 JSON 字符串: {type(items_json)}"
            )
            return None

        try:
            items_data = json.loads(items_json)
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON 解析失败: {e}")
            return None

        self.logger.info(f"找到 {len(items_data)} 条动态")

        for item in items_data:
            if not item.get('link'):
                continue
            article = {
                'user_id': user_id,
                'article_id': self._extract_article_id(item['link']),
                'title': item.get('title', ''),
                'content': item.get('content', ''),
                'publish_time': item.get('time', ''),
                'link': item['link'],
                'is_likely_column': item.get('is_likely_column', False),
                'likes': 0,
                'comments': 0,
            }
            articles.append(article)
            title_preview = article['title'][:30] if article['title'] else '无标题'
            self.logger.info(f"  [{len(articles)}] {title_preview}...")

        return articles

    async def _parse_article_detail(self, url: str) -> dict:
        """导航到文章详情页并解析内容"""
        detail = {
            'url': url,
            'title': '',
            'author': '',
            'publish_time': '',
            'content': '',
            'likes': 0,
            'comments': 0,
            'is_column': False,
        }

        try:
            await self._navigate(url, wait_seconds=3)

            # WAF 检测 — 触发浏览器重启
            if await self._detect_waf():
                self.logger.warning(f"文章详情页触发 WAF: {url[-30:]}")
                raise WafDetectedError(f"WAF detected at detail page: {url[-30:]}")

            # 从页面标题提取
            title = await self._page_title()
            if '雪球' in title:
                idx = title.find('雪球')
                title = title[:idx].strip().rstrip('-').rstrip('—').rstrip('–').strip()
            if len(title) > 100:
                title = title[:100] + '...'
            detail['title'] = title
            self.logger.info(f"标题: {title[:50]}...")

            # 作者
            author = await self._query_text('.article__bd__from a, .author-name')
            if author:
                for suffix in ['的雪球专栏', '的专栏']:
                    if suffix in author:
                        author = author.split(suffix)[0]
                detail['author'] = author

            # 时间
            pub_time = await self._query_text('.article__bd__from .date, .time, .date')
            if pub_time:
                detail['publish_time'] = pub_time

            # 正文
            content = await self._query_text('.article__bd__detail')
            if content:
                detail['content'] = content
                detail['is_column'] = True
                self.logger.info(f"正文: {len(content)} 字符")
            else:
                # 短动态 fallback
                for sel in [
                    '.status-content',
                    '.article-content',
                    '.status__content',
                    'article',
                ]:
                    text = await self._query_text(sel)
                    if text and len(text) > 20:
                        detail['content'] = text
                        self.logger.info(f"备选 {sel}: {len(text)} 字符")
                        break
                if not detail['content'] and detail['title']:
                    detail['content'] = detail['title']

            # 互动数据（从页面内嵌 JSON 提取）
            page_html = await self._page_content()
            like_m = re.search(r'"likeCount":(\d+)', page_html)
            if like_m:
                detail['likes'] = int(like_m.group(1))
            comment_m = re.search(r'"commentCount":(\d+)', page_html)
            if comment_m:
                detail['comments'] = int(comment_m.group(1))

        except WafDetectedError:
            # 爬1(20261009): 详情页 WAF 必须上抛走分级熔断 —— 此前被下面的
            # 宽 except 吞成解析失败, 详情级 WAF 处理全是死代码, 被拦文章
            # 静默丢弃且不计 blocked (审计价值投资线 P1#1)。
            raise
        except Exception as e:
            self.logger.error(f"解析文章详情失败: {e}")

        return detail

    # ============ 保存 ============

    def _save_as_markdown(self, article: dict, user_id: str) -> str:
        """保存为 Markdown 文件"""
        user_dir = self.data_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        article_id = article.get('article_id', 'unknown')
        filepath = user_dir / f"{article_id}.md"

        lines = [
            f"# {article.get('title', '无标题')}",
            "",
            f"> 作者：{article.get('author', '未知')} | 发布时间：{article.get('publish_time', '未知')}",
            f"> 点赞：{article.get('likes', 0)} | 评论：{article.get('comments', 0)}",
            f"> 原文链接：{article.get('url', article.get('link', ''))}",
            "",
            "---",
            "",
            article.get('content', ''),
            "",
            "---",
            "",
            f"*爬取时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        ]
        content = '\n'.join(lines)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        self.logger.info(f"保存文章: {filepath}")
        return str(filepath)

    # ============ 核心爬取逻辑 ============

    def _extract_and_save_opencli(
        self, article: dict, user_id: str, user_name: str
    ) -> bool:
        """提取单篇文章正文并保存。返回 True 表示保存成功。

        保存到磁盘 + 更新索引。WAF/无内容/重复 返回 False。
        """
        url = article.get('url', '')
        if not url:
            return False

        # 提取正文（浏览器级别导航）
        detail = self._opencli.get_article_content(url)

        # 适配器通道的风控标记必须排在最前面看。
        # 被拦时 `get_article_content` 会把 title/content **一起清空**，于是
        # resolve_title 也拿不到可用标题 —— 要是让下面的「无标题」分支先跑，一次
        # 真实的拦截就会被记成「跳过无标题」，`blocked_articles` 少计一篇，而那正是
        # 2026-10-09 那套可观测性要防的「安静日 vs 被拦日」盲区。
        if detail.get('waf_detected'):
            self._waf_blocked_articles += 1
            self.logger.warning(
                f"WAF 拦截(适配器): {str(detail.get('title') or '')[:30]} "
                f"({article.get('article_id', '')})"
            )
            return False

        # 跳过非专栏/回复类文章
        # 详情标题优先，但它可能是站点栏目（`雪球-聪明的投资者都在这里`）或
        # 列表占位符（`展开`）—— 那种情况下退回列表标题，而不是把站点栏目标题
        # 当成文章标题存下来（2026-10-09：index.json 里 7 条这样的标题）。
        title = resolve_title(detail.get('title'), article.get('title'))
        if title.startswith('回复@'):
            self.logger.info(f"跳过回复: {title[:30]}...")
            return False

        # 两边都没有可用标题 → 不落盘。内容通常只剩「来源：雪球App…」这种样板，
        # 日报侧（is_error_page 对空标题返回 True）本来也会把它扔掉，那就不必先
        # 写进 corpus 再被上游 sync_raw_articles_to_ima 拖进 IMA 知识库。
        if not title:
            self.logger.info(
                f"跳过无标题: {article.get('article_id', '')} "
                f"(详情={str(detail.get('title'))[:20]!r} "
                f"列表={str(article.get('title'))[:20]!r})"
            )
            return False

        # 合并文章信息
        merged = {
            'article_id': article.get('article_id', ''),
            'title': title,
            'author': article.get('author', user_name),
            'publish_time': article.get('time', ''),
            'content': detail.get('content', article.get('text', '')),
            'likes': article.get('likes', 0),
            'comments': article.get('replies', 0),
            'url': url,
            'link': url,
            'crawl_time': datetime.now().isoformat(),
            'is_column': True,
        }

        # 跳过无内容文章
        if not merged['content']:
            self.logger.info(f"跳过无内容: {merged['title'][:30]}")
            return False

        # 跳过 WAF/错误页面（405、访问阻断等）
        if _is_content_error(merged['title'], merged['content']):
            self._waf_blocked_articles += 1
            self.logger.warning(
                f"WAF 拦截: {merged['title'][:30]} ({merged['article_id']})"
            )
            return False

        # 去重检查
        article_id = merged['article_id']
        index_key = f"{user_id}_{article_id}"
        if index_key in self.index.get('articles', {}):
            self.logger.info(f"已存在，跳过: {article_id}")
            return False

        # 保存为 Markdown
        filepath = self._save_as_markdown(merged, user_id)

        # 更新索引
        self.index['articles'][index_key] = {
            'article_id': article_id,
            'user_id': user_id,
            'title': merged['title'],
            'author': merged['author'],
            'publish_time': merged['publish_time'],
            'crawl_time': merged['crawl_time'],
            'filepath': filepath,
        }
        self._save_index()
        return True

    async def _crawl_one_user_opencli(self, account: dict, max_articles: int) -> dict:
        """爬取单个用户 — OpenCLI 模式（Chrome 扩展）"""
        user_id = account.get('id')
        user_name = account.get('name', user_id)

        assert self._opencli is not None, "OpenCLI extractor not initialized"
        assert user_id is not None, "Account missing user_id"

        result = {
            'user_id': user_id,
            'name': user_name,
            'new_articles': 0,
            'saved_articles': 0,
            'new_articles_available': 0,  # API 返回的新文章数（含 WAF 拦截的）
            'blocked_articles': 0,  # 其中被 WAF 拦掉正文的篇数
        }
        blocked_before = self._waf_blocked_articles

        try:
            # 1. 获取文章列表（API 级别，不走浏览器导航）
            article_list = _opencli_get_list(str(user_id), count=max_articles)
            self.logger.info(f"OpenCLI 获取到 {len(article_list)} 篇文章")

            # 2. 增量去重（复用现有逻辑）
            history_ids = self._get_history_article_ids(user_id)
            indexed_ids = {
                info.get('article_id', '')
                for info in self.index.get('articles', {}).values()
                if info.get('user_id') == user_id
            }
            user_data_dir = self.data_dir / user_id
            filesystem_ids = set()
            if user_data_dir.exists():
                for f in user_data_dir.glob('*.md'):
                    filesystem_ids.add(f.stem)
            all_known = history_ids | indexed_ids | filesystem_ids

            new_articles = [
                a
                for a in article_list
                if a.get('article_id') and a['article_id'] not in all_known
            ]
            self.logger.info(
                f"发现 {len(new_articles)} 篇新文章（共 {len(article_list)} 篇）"
            )

            # 3. 提取正文并保存。专栏优先；非专栏多数是回复/短状态，
            # 只保留少量探测位，避免为明显不收录的内容消耗详情页请求。
            likely_columns = [a for a in new_articles if a.get('is_column')]
            likely_others = [a for a in new_articles if not a.get('is_column')]
            ordered_articles = likely_columns + likely_others
            other_probe_limit = 3
            other_probed = 0
            articles_saved = []
            for i, article in enumerate(ordered_articles[:max_articles]):
                url = article.get('url', '')
                if not url:
                    continue

                if not article.get('is_column'):
                    if other_probed >= other_probe_limit:
                        remaining = min(len(ordered_articles), max_articles) - i
                        self.logger.info(
                            f"OpenCLI 专栏已耗尽，剩余 {remaining} 条非专栏预判，提前结束"
                        )
                        break
                    other_probed += 1

                self.logger.info(
                    f"OpenCLI 详情 [{i + 1}/{min(len(ordered_articles), max_articles)}]: {url[-30:]}"
                )

                if self._extract_and_save_opencli(article, user_id, user_name):
                    articles_saved.append(article)

            result['new_articles_available'] = len(new_articles)

            # 4. 保存历史
            if articles_saved:
                self._save_history(user_id, articles_saved)

            result['new_articles'] = len(articles_saved)
            result['saved_articles'] = len(articles_saved)
            self.logger.info(f"用户 {user_name}: 保存 {len(articles_saved)} 篇文章")

        except Exception as e:
            self.logger.error(f"OpenCLI 爬取失败: {e}", exc_info=True)
            result['error'] = str(e)
            # 列表页就被风控拦住 = 账号级不可达。标出来让 gateway 的 nodriver
            # 适配器与外层都能按 BLOCKED_WAF 处理，而不是塌成普通异常。
            if _WafBlockedError is not None and isinstance(e, _WafBlockedError):
                result['waf_triggered'] = True
                result['waf_scope'] = 'account'

        result['blocked_articles'] = self._waf_blocked_articles - blocked_before
        return result

    async def _crawl_one_user(self, account: dict, max_articles: int) -> dict:
        """爬取单个用户（在共享浏览器上下文中）"""
        user_id = account.get('id')
        user_name = account.get('name', user_id)
        url = account.get('url', f'https://xueqiu.com/u/{user_id}')

        result = {
            'user_id': user_id,
            'name': user_name,
            'new_articles': 0,
            'saved_articles': 0,
            'waf_triggered': False,
            # 撞的是哪一层：'detail'=只是某篇正文被拦（列表页正常），
            # 'account'=用户页/时间线都拿不到。gateway 的 nodriver 适配器按它
            # 决定 AttemptScope —— 详情级不该触发站点级熔断。
            'waf_scope': '',
        }

        try:
            # 访问首页 + 用户页
            await self._navigate('https://xueqiu.com', wait_seconds=2)
            self.logger.info(f"访问用户主页: {url}")
            await self._navigate(url, wait_seconds=3)

            # WAF 检测
            if await self._detect_waf():
                self.logger.error(f"WAF 拦截，跳过用户: {user_name}")
                result['error'] = 'waf_blocked'
                return result

            # 获取用户名
            real_name = await self._get_user_name(user_id)
            if real_name and real_name != user_id:
                self._update_account_name(user_id, real_name)

            # 解析文章列表
            article_list = await self._parse_article_list(user_id)
            if article_list is None:
                result['error'] = 'timeline_not_found'
                return result

            # 增量去重
            history_ids = self._get_history_article_ids(user_id)
            indexed_ids = {
                info.get('article_id', '')
                for info in self.index.get('articles', {}).values()
                if info.get('user_id') == user_id
            }
            user_data_dir = self.data_dir / user_id
            filesystem_ids = set()
            if user_data_dir.exists():
                for f in user_data_dir.glob('*.md'):
                    filesystem_ids.add(f.stem)
            all_known = history_ids | indexed_ids | filesystem_ids

            new_articles = [
                a
                for a in article_list
                if a.get('article_id') and a['article_id'] not in all_known
            ]

            # 专栏优先窗口：按列表层预判排序，让专栏优先占用 max_articles 额度。
            # 背景：原逻辑取时间线前 N 条（专栏+评论混合），评论刷屏时真专栏会被挤出窗口漏抓。
            # 现改为：专栏预判条目排前，评论/短动态排后。详情页仍会用 is_column 二次确认。
            likely_columns = [a for a in new_articles if a.get('is_likely_column')]
            likely_others = [a for a in new_articles if not a.get('is_likely_column')]
            ordered_articles = likely_columns + likely_others
            self.logger.info(
                f"发现 {len(new_articles)} 篇新文章（共 {len(article_list)} 篇，"
                f"其中预判专栏 {len(likely_columns)} 篇）"
            )

            # 获取每篇详情 — 专栏优先，最多处理 max_articles 篇
            # 早停优化：排序后专栏在前。允许在专栏耗尽后额外探测少量非专栏条目
            # （容错 DOM 特征失效），超过阈值则停止，避免对大量评论发详情请求。
            OTHER_PROBE_LIMIT = 3  # 专栏耗尽后最多再探测几条非专栏（防预判漏报）
            other_probed = 0
            articles = []
            for i, article in enumerate(ordered_articles[:max_articles]):
                if not article['link']:
                    continue

                # 早停：进入非专栏区后，只探测有限几条就停（后面排序上全是评论）
                if not article.get('is_likely_column'):
                    if other_probed >= OTHER_PROBE_LIMIT:
                        self.logger.info(
                            f"专栏已耗尽，剩余 {min(len(ordered_articles), max_articles) - i} 条为非专栏预判，提前结束"
                        )
                        break
                    other_probed += 1

                await self._random_delay()
                self.logger.info(
                    f"详情 [{i + 1}/{min(len(ordered_articles), max_articles)}]: {article['link'][-30:]}"
                )

                try:
                    detail = await self._parse_article_detail(article['link'])
                except WafDetectedError:
                    self.logger.warning(
                        f"详情页 WAF 触发，中止当前用户剩余 {min(len(ordered_articles), max_articles) - i} 篇文章"
                    )
                    result['waf_triggered'] = True
                    result['waf_scope'] = 'detail'
                    break

                if not detail:
                    continue

                article.update(detail)
                if not article.get('title') and article.get('list_title'):
                    article['title'] = article['list_title']
                if not article.get('content') and article.get('list_content'):
                    article['content'] = article['list_content']
                # Fallback: use user_name if author is empty (selector failed on some page templates)
                if not article.get('author'):
                    article['author'] = user_name
                article['crawl_time'] = datetime.now().isoformat()

                # 跳过非专栏文章
                title = article.get('title', '')
                if title.startswith('回复@') or not article.get('is_column'):
                    self.logger.info(f"跳过非专栏: {title[:30]}...")
                    continue

                # 去重
                article_id = article.get('article_id', '')
                index_key = f"{user_id}_{article_id}"
                if index_key in self.index.get('articles', {}):
                    self.logger.info(f"已存在，跳过: {article_id}")
                    continue

                # 保存
                filepath = self._save_as_markdown(article, user_id)
                article['filepath'] = filepath

                self.index['articles'][index_key] = {
                    'article_id': article_id,
                    'user_id': user_id,
                    'title': article.get('title', ''),
                    'author': article.get('author', ''),
                    'publish_time': article.get('publish_time', ''),
                    'crawl_time': article.get('crawl_time'),
                    'filepath': filepath,
                }
                self._save_index()
                articles.append(article)

            self._save_history(user_id, articles)
            result['new_articles'] = len(articles)
            result['saved_articles'] = len(articles)

        except WafDetectedError:
            self.logger.warning(f"WAF 触发，中止用户: {user_name}")
            result['waf_triggered'] = True
            result['waf_scope'] = 'account'
        except Exception as e:
            self.logger.error(f"爬取用户 {user_id} 失败: {e}")
            import traceback

            traceback.print_exc()
            result['error'] = str(e)

        return result

    async def _reconnect_browser(self):
        """重启浏览器 — 防止 WAF 累积"""
        try:
            await self._close_browser()
            await asyncio.sleep(2)
            await self._start_browser()
            await self._warmup()
            await self._inject_cookies()
            self.logger.info("浏览器重启完成")
        except Exception as e:
            self.logger.error(f"浏览器重启失败: {e}")
            raise

    async def crawl_all_users(self, max_articles: int = None) -> dict:
        """爬取所有配置用户 — OpenCLI 优先，nodriver 兜底"""
        max_articles = max_articles or self.config.get('crawler', {}).get(
            'max_articles', 20
        )

        stats = {
            'total_users': len(self.accounts),
            'total_new': 0,
            'total_saved': 0,
            'total_blocked': 0,
            'users': [],
            'mode': 'opencli' if self._use_opencli else 'nodriver',
        }

        if self._use_opencli:
            # ── OpenCLI 模式：逐用户爬取 ──
            self.logger.info(f"🚀 OpenCLI 模式启动，共 {len(self.accounts)} 个用户")

            for i, account in enumerate(self.accounts):
                user_id = account.get('id')
                user_name = account.get('name', user_id)
                if not user_id:
                    continue

                self.logger.info(f"\n{'=' * 50}")
                self.logger.info(
                    f"爬取 [{i + 1}/{len(self.accounts)}]: {user_name} ({user_id})"
                )

                result = await self._crawl_one_user_opencli(account, max_articles)
                stats['total_new'] += result.get('new_articles', 0)
                stats['total_saved'] += result.get('saved_articles', 0)
                stats['total_blocked'] += result.get('blocked_articles', 0)
                stats['users'].append(result)

            self._opencli.close()
        else:
            # ── Nodriver 模式：原有逻辑 ──
            restart_every = self.config.get('crawler', {}).get(
                'browser_restart_interval', 5
            )

            await self._start_browser()
            await self._warmup()
            await self._inject_cookies()

            for i, account in enumerate(self.accounts):
                user_id = account.get('id')
                user_name = account.get('name', user_id)

                if not user_id:
                    self.logger.warning(f"账号配置不完整: {account}")
                    continue

                self.logger.info(f"\n{'=' * 50}")
                self.logger.info(
                    f"爬取 [{i + 1}/{len(self.accounts)}]: {user_name} ({user_id})"
                )

                result = await self._crawl_one_user(account, max_articles)
                stats['total_new'] += result.get('new_articles', 0)
                stats['total_saved'] += result.get('saved_articles', 0)
                stats['total_blocked'] += result.get('blocked_articles', 0)
                stats['users'].append(result)

                # 每 N 个用户重启浏览器（防 WAF 累积）
                should_restart = (i + 1) % restart_every == 0 and i < len(
                    self.accounts
                ) - 1
                if result.get('waf_triggered') and i < len(self.accounts) - 1:
                    self.logger.info("⚠️ 检测到 WAF，立即重启浏览器...")
                    should_restart = True

                if should_restart:
                    self.logger.info(f"🔄 重启浏览器（已处理 {i + 1} 个用户）...")
                    await self._reconnect_browser()

                # 用户间延迟
                if i < len(self.accounts) - 1:
                    base_delay = self.config.get('crawler', {}).get('delay_min', 2)
                    delay = base_delay + random.uniform(0, base_delay)
                    self.logger.info(f"用户间延迟 {delay:.1f}s...")
                    await self.tab.sleep(delay)

            await self._close_browser()

        self.logger.info(f"\n{'=' * 50}")
        self.logger.info("爬取完成!")
        self.logger.info(f"总用户: {stats['total_users']}")
        self.logger.info(f"新文章: {stats['total_new']}")
        if stats.get('total_blocked'):
            self.logger.warning(
                f"其中被 WAF 拦掉正文: {stats['total_blocked']} 篇"
                "（不计入 failed，别把这类轮次读成「安静日」）"
            )

        self._write_crawl_stats(stats)
        return stats

    def _write_crawl_stats(self, stats: dict) -> None:
        """把整轮统计落到 `.last_crawl_stats.json`（抽出来便于直接测字段）。"""
        successful = sum(
            1 for u in stats['users'] if 'saved_articles' in u and 'error' not in u
        )
        failed = sum(1 for u in stats['users'] if 'error' in u)
        crawl_stats = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'total_users': stats['total_users'],
            'successful': successful,
            'failed': failed,
            'new_articles': stats['total_new'],
            # 被 WAF 拦掉正文的篇数。**故意不写进 compare_crawl_outputs 的
            # STATS_FIELDS** —— 它只用于让人/巡检区分「安静日」与「被拦日」，
            # 不参与 legacy↔gateway 的对比判定。
            # 读取方请用 .get('blocked_articles', 0)，老文件没有这个键。
            'blocked_articles': stats['total_blocked'],
        }
        stats_file = self.data_dir / '.last_crawl_stats.json'
        try:
            with open(stats_file, 'w', encoding='utf-8') as f:
                json.dump(crawl_stats, f, ensure_ascii=False)
        except OSError as e:
            self.logger.warning(f"保存统计失败: {e}")

    async def crawl_user(self, user_id: str, max_articles: int = None) -> dict:
        """爬取单个用户"""
        if max_articles is None:
            max_articles = self.config.get('crawler', {}).get('max_articles', 20)

        account = next((a for a in self.accounts if a.get('id') == user_id), None)
        if not account:
            self.logger.error(f"未找到用户: {user_id}")
            return {}

        await self._start_browser()
        await self._warmup()
        await self._inject_cookies()

        result = await self._crawl_one_user(account, max_articles)

        await self._close_browser()
        return result


# ============ CLI ============


async def main_async():
    import argparse

    parser = argparse.ArgumentParser(description='雪球爬虫 (nodriver 版本)')
    parser.add_argument('--config', '-c', help='配置文件路径')
    parser.add_argument('--user', '-u', help='指定用户ID')
    parser.add_argument('--max', '-m', type=int, default=20, help='最大文章数')
    parser.add_argument('-a', '--all', action='store_true', help='爬取所有用户')
    args = parser.parse_args()

    crawler = XueqiuCrawlerNodriver(args.config)

    if args.user:
        result = await crawler.crawl_user(args.user, max_articles=args.max)
        print(f"\n结果: {result}")
    else:
        result = await crawler.crawl_all_users(max_articles=args.max)
        print(
            f"\n结果: {len(result.get('users', []))} 用户, {result.get('total_new', 0)} 篇新文章"
        )


def main():
    try:
        asyncio.run(main_async())
    except RuntimeError:
        pass  # nodriver subprocess cleanup (harmless)


if __name__ == '__main__':
    main()
