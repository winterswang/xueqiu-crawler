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
from typing import Dict, List, Optional

from dotenv import load_dotenv
_dotenv_path = Path(__file__).resolve().parent.parent / '.env'
if _dotenv_path.exists():
    load_dotenv(_dotenv_path, override=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

import nodriver as uc


class WafDetectedError(Exception):
    """WAF 检测异常 — 用于触发浏览器重启"""
    pass

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
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


class XueqiuCrawlerNodriver:
    """雪球爬虫 — nodriver 版本"""

    def __init__(self, config_path: str = None):
        self.project_root = Path(__file__).parent.parent
        self.config = self._load_config(config_path)
        self.logger = setup_logging(self.config)
        self.accounts = self._load_accounts()
        self.data_dir = self.project_root / self.config.get('storage', {}).get('output_dir', 'data')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.data_dir / 'index.json'
        self.index = self._load_index()
        self.timeout = self.config.get('crawler', {}).get('timeout', 60) * 1000
        self.browser = None
        self.tab = None

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
            'articles': [{'article_id': a.get('article_id'), 'title': a.get('title', '')[:50],
                          'crawl_time': a.get('crawl_time')} for a in articles]
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
        config = uc.Config(headless=True, sandbox=False)
        self.browser = await uc.start(config=config)
        self.logger.info("nodriver 浏览器已启动")

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
        """检测是否触发了WAF验证"""
        title = await self._page_title()
        if "滑动验证" in title:
            self.logger.warning("检测到 WAF 滑动验证页面!")
            return True
        content = await self._page_content()
        if "aliyun_waf" in content:
            self.logger.warning("检测到 WAF 标记!")
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
                f'{k}={v}' for k, v in cookies.items()
                if k not in ('xq_a_token', 'xq_r_token', 'xq_id_token', 'u')
            )
            if cookie_pairs:
                await self.tab.evaluate(f"document.cookie = '{cookie_pairs}; domain=.xueqiu.com; path=/; SameSite=Lax'")
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
                            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
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

    async def _parse_article_list(self, user_id: str) -> List[dict]:
        """解析用户时间线，提取文章列表"""
        articles = []

        # 等待时间线加载
        found = await self._wait_for_selector('.timeline__item', timeout_seconds=15)
        if not found:
            self.logger.warning(f"未找到 .timeline__item (可能被WAF拦截)")
            return articles

        # 获取所有时间线条目的关键数据 (JSON.stringify 解决 nodriver RemoteObject 序列化)
        items_json = await self.tab.evaluate("""
            JSON.stringify(Array.from(document.querySelectorAll('.timeline__item')).map(function(item, i) {
                var links = Array.from(item.querySelectorAll('a'))
                    .map(function(a) { return a.href; })
                    .filter(function(h) { return /\\/\\d+\\/\\d+$/.test(h) && h.indexOf('#comment') === -1; });
                var titleEl = item.querySelector('.content, .status-content');
                var timeEl = item.querySelector('.time, .date');
                return {
                    link: links[0] || '',
                    title: titleEl && titleEl.innerText ? titleEl.innerText.trim().split('\\n')[0].substring(0, 100) : '',
                    time: timeEl && timeEl.innerText ? timeEl.innerText.trim() : '',
                    index: i
                };
            }))
        """)

        if not items_json or not isinstance(items_json, str):
            self.logger.warning(f"evaluate 未返回有效的 JSON 字符串: {type(items_json)}")
            return articles

        try:
            items_data = json.loads(items_json)
        except json.JSONDecodeError as e:
            self.logger.error(f"JSON 解析失败: {e}")
            return articles

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
            'title': '', 'author': '', 'publish_time': '',
            'content': '', 'likes': 0, 'comments': 0, 'is_column': False,
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
                for sel in ['.status-content', '.article-content', '.status__content', 'article']:
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

    async def _crawl_one_user(self, account: dict, max_articles: int) -> dict:
        """爬取单个用户（在共享浏览器上下文中）"""
        user_id = account.get('id')
        user_name = account.get('name', user_id)
        url = account.get('url', f'https://xueqiu.com/u/{user_id}')

        result = {'user_id': user_id, 'name': user_name, 'new_articles': 0, 'saved_articles': 0, 'waf_triggered': False}

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

            new_articles = [a for a in article_list
                            if a.get('article_id') and a['article_id'] not in all_known]
            self.logger.info(
                f"发现 {len(new_articles)} 篇新文章（共 {len(article_list)} 篇）"
            )

            # 获取每篇详情
            articles = []
            for i, article in enumerate(new_articles[:max_articles]):
                if not article['link']:
                    continue

                await self._random_delay()
                self.logger.info(f"详情 [{i+1}/{min(len(new_articles), max_articles)}]: {article['link'][-30:]}")

                try:
                    detail = await self._parse_article_detail(article['link'])
                except WafDetectedError:
                    self.logger.warning(f"详情页 WAF 触发，中止当前用户剩余 {min(len(new_articles), max_articles) - i} 篇文章")
                    result['waf_triggered'] = True
                    break

                if not detail:
                    continue

                article.update(detail)
                if not article.get('title') and article.get('list_title'):
                    article['title'] = article['list_title']
                if not article.get('content') and article.get('list_content'):
                    article['content'] = article['list_content']
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
                    'article_id': article_id, 'user_id': user_id,
                    'title': article.get('title', ''),
                    'author': article.get('author', ''),
                    'publish_time': article.get('publish_time', ''),
                    'crawl_time': article.get('crawl_time'),
                    'file_path': filepath, 'filepath': filepath,
                }
                self._save_index()
                articles.append(article)

            self._save_history(user_id, articles)
            result['new_articles'] = len(articles)
            result['saved_articles'] = len(articles)

        except WafDetectedError:
            self.logger.warning(f"WAF 触发，中止用户: {user_name}")
            result['waf_triggered'] = True
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
        """爬取所有配置用户 — nodriver 每5用户重启防WAF"""
        max_articles = max_articles or self.config.get('crawler', {}).get('max_articles', 20)
        restart_every = self.config.get('crawler', {}).get('browser_restart_interval', 5)

        stats = {
            'total_users': len(self.accounts),
            'total_new': 0, 'total_saved': 0,
            'users': []
        }

        await self._start_browser()
        await self._warmup()
        await self._inject_cookies()

        for i, account in enumerate(self.accounts):
            user_id = account.get('id')
            user_name = account.get('name', user_id)

            if not user_id:
                self.logger.warning(f"账号配置不完整: {account}")
                continue

            self.logger.info(f"\n{'='*50}")
            self.logger.info(f"爬取 [{i+1}/{len(self.accounts)}]: {user_name} ({user_id})")

            result = await self._crawl_one_user(account, max_articles)
            stats['total_new'] += result.get('new_articles', 0)
            stats['total_saved'] += result.get('saved_articles', 0)
            stats['users'].append(result)

            # 每 N 个用户重启浏览器（防 WAF 累积）
            # 或 WAF 触发后立即重启
            should_restart = (i + 1) % restart_every == 0 and i < len(self.accounts) - 1
            if result.get('waf_triggered') and i < len(self.accounts) - 1:
                self.logger.info(f"⚠️ 检测到 WAF，立即重启浏览器...")
                should_restart = True

            if should_restart:
                self.logger.info(f"🔄 重启浏览器（已处理 {i+1} 个用户）...")
                await self._reconnect_browser()

            # 用户间延迟
            if i < len(self.accounts) - 1:
                base_delay = self.config.get('crawler', {}).get('delay_min', 2)
                delay = base_delay + random.uniform(0, base_delay)
                self.logger.info(f"用户间延迟 {delay:.1f}s...")
                await self.tab.sleep(delay)

        await self._close_browser()

        self.logger.info(f"\n{'='*50}")
        self.logger.info(f"爬取完成!")
        self.logger.info(f"总用户: {stats['total_users']}")
        self.logger.info(f"新文章: {stats['total_new']}")

        # 保存统计
        successful = sum(1 for u in stats['users'] if 'saved_articles' in u)
        failed = sum(1 for u in stats['users'] if 'error' in u)
        crawl_stats = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'total_users': stats['total_users'],
            'successful': successful,
            'failed': failed,
            'new_articles': stats['total_new'],
        }
        stats_file = self.data_dir / '.last_crawl_stats.json'
        try:
            with open(stats_file, 'w', encoding='utf-8') as f:
                json.dump(crawl_stats, f, ensure_ascii=False)
        except OSError as e:
            self.logger.warning(f"保存统计失败: {e}")

        return stats

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
        print(f"\n结果: {len(result.get('users', []))} 用户, {result.get('total_new', 0)} 篇新文章")


def main():
    try:
        asyncio.run(main_async())
    except RuntimeError:
        pass  # nodriver subprocess cleanup (harmless)


if __name__ == '__main__':
    main()
