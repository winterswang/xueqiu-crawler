#!/usr/bin/env python3
"""
雪球专栏文章爬虫

功能:
- 爬取指定用户的专栏文章
- 保存为 Markdown 格式
- 支持增量更新
- 自动去重
"""

import os
import sys
import json
import yaml
import random
import logging
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from playwright.sync_api import sync_playwright, Page, BrowserContext
except ImportError:
    print("请先安装 playwright: pip install playwright && playwright install chromium")
    sys.exit(1)


# 配置日志
def setup_logging(config: dict):
    """配置日志"""
    log_dir = Path(config.get('storage', {}).get('output_dir', 'data')).parent / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = log_dir / 'crawler.log'
    
    logging.basicConfig(
        level=getattr(logging, config.get('logging', {}).get('level', 'INFO')),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


class XueqiuCrawler:
    """雪球爬虫"""
    
    def __init__(self, config_path: str = None):
        self.project_root = Path(__file__).parent.parent
        self.config = self._load_config(config_path)
        self.logger = setup_logging(self.config)
        self.accounts = self._load_accounts()
        self.data_dir = self.project_root / self.config.get('storage', {}).get('output_dir', 'data')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.data_dir / 'index.json'
        self.index = self._load_index()
        
    def _load_config(self, config_path: str = None) -> dict:
        """加载配置"""
        if config_path is None:
            config_path = self.project_root / 'config' / 'config.yaml'
        else:
            config_path = Path(config_path)
            
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        return {}
    
    def _load_accounts(self) -> List[dict]:
        """加载账号配置"""
        accounts_path = self.project_root / 'config' / 'accounts.yaml'
        if accounts_path.exists():
            with open(accounts_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                return [a for a in data.get('accounts', []) if a.get('enabled', True)]
        return []
    
    def _load_index(self) -> dict:
        """加载索引"""
        if self.index_file.exists():
            with open(self.index_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {'articles': {}, 'last_update': None, 'history': {}}
    
    def _save_index(self):
        """保存索引"""
        self.index['last_update'] = datetime.now().isoformat()
        with open(self.index_file, 'w', encoding='utf-8') as f:
            json.dump(self.index, f, ensure_ascii=False, indent=2)
    
    def _save_history(self, user_id: str, articles: List[dict]):
        """保存历史快照"""
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
                    'crawl_time': a.get('crawl_time')
                }
                for a in articles
            ]
        }
        
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False, indent=2)
        
        self.logger.info(f"历史快照已保存: {history_file}")
    
    def _get_history_article_ids(self, user_id: str) -> set:
        """获取历史文章ID集合"""
        history_dir = self.data_dir / 'history' / user_id
        if not history_dir.exists():
            return set()
        
        article_ids = set()
        # 读取所有历史快照
        for history_file in sorted(history_dir.glob('*.json'), reverse=True)[:7]:  # 最近7天
            with open(history_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for article in data.get('articles', []):
                    article_ids.add(article.get('article_id'))
        
        return article_ids
    
    def _random_delay(self):
        """随机延迟"""
        delay_min = self.config.get('crawler', {}).get('delay_min', 2)
        delay_max = self.config.get('crawler', {}).get('delay_max', 5)
        delay = random.uniform(delay_min, delay_max)
        self.logger.debug(f"等待 {delay:.1f} 秒...")
        import time
        time.sleep(delay)
    
    def _create_browser_context(self, playwright) -> BrowserContext:
        """创建浏览器上下文（带反检测）"""
        anti_detect = self.config.get('anti_detect', {})
        
        browser = playwright.chromium.launch(
            headless=self.config.get('crawler', {}).get('headless', True),
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ]
        )
        
        context = browser.new_context(
            viewport=anti_detect.get('viewport', {'width': 1920, 'height': 1080}),
            user_agent=anti_detect.get('user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'),
            locale=anti_detect.get('locale', 'zh-CN'),
        )
        
        # 绕过 webdriver 检测
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            window.chrome = { runtime: {} };
        """)
        
        return browser, context
    
    def _is_article(self, item) -> bool:
        """判断是否为专栏文章"""
        # 检查是否有文章标题
        title_elem = item.query_selector('.title, h3, h4, .article-title')
        if title_elem:
            title = title_elem.inner_text().strip()
            # 标题较长通常是文章
            if len(title) > 5:
                return True
        
        # 检查是否有"原文"链接（转发的不是原创文章）
        source_elem = item.query_selector('.source')
        if source_elem and '原文' in source_elem.inner_text():
            return False
            
        # 检查内容长度
        content_elem = item.query_selector('.content, .status-content, article')
        if content_elem:
            content = content_elem.inner_text()
            # 内容超过200字符可能是文章
            if len(content) > 200:
                return True
        
        return False
    
    def _extract_article_id(self, url: str) -> str:
        """从URL提取文章ID"""
        # 雪球文章URL格式: https://xueqiu.com/用户ID/文章ID
        # 例如: https://xueqiu.com/6308001210/312345678
        match = re.search(r'/\d+/(\d+)$', url)
        if match:
            return match.group(1)
        # 如果是用户主页，用时间戳生成唯一ID
        return hashlib.md5((url + str(time.time())).encode()).hexdigest()[:12]
    
    def _parse_article_list(self, page: Page, user_id: str) -> List[dict]:
        """解析文章列表"""
        articles = []
        
        # 等待页面加载
        page.wait_for_selector('.timeline__item', timeout=15000)
        
        items = page.query_selector_all('.timeline__item')
        self.logger.info(f"找到 {len(items)} 条动态")
        
        for i, item in enumerate(items):
            try:
                article = {
                    'user_id': user_id,
                    'article_id': '',
                    'title': '',
                    'content': '',
                    'publish_time': '',
                    'link': '',
                    'likes': 0,
                    'comments': 0,
                }
                
                # 获取文章链接 - 遍历所有链接，找到文章格式的
                article_links_in_item = []
                all_links = item.query_selector_all('a')
                for link_elem in all_links:
                    href = link_elem.get_attribute('href')
                    if href:
                        if not href.startswith('http'):
                            href = 'https://xueqiu.com' + href
                        # 检查是否是文章链接（格式：/用户ID/文章ID，且不包含#comment）
                        if re.match(r'https://xueqiu\.com/\d+/\d+$', href) and '#comment' not in href:
                            article_links_in_item.append(href)
                
                # 取第一个文章链接
                if article_links_in_item:
                    article['link'] = article_links_in_item[0]
                    article['article_id'] = self._extract_article_id(article['link'])
                
                # 获取标题 - 雪球专栏文章通常在 .article__title 或 .title 中
                title_elem = item.query_selector('.article__title, .title, h3, h4')
                if title_elem:
                    article['title'] = title_elem.inner_text().strip()
                
                # 如果没有标题，尝试从内容中提取第一行作为标题
                if not article['title']:
                    content_elem = item.query_selector('.content, .status-content, article')
                    if content_elem:
                        first_line = content_elem.inner_text().strip().split('\n')[0]
                        if len(first_line) > 5:
                            article['title'] = first_line[:100]
                
                # 获取内容
                content_elem = item.query_selector('.content, .status-content, article')
                if content_elem:
                    article['content'] = content_elem.inner_text().strip()
                
                # 获取时间
                time_elem = item.query_selector('.time, .date')
                if time_elem:
                    article['publish_time'] = time_elem.inner_text().strip()
                
                # 获取互动数据
                like_elem = item.query_selector('.like, [class*="like"]')
                if like_elem:
                    like_text = like_elem.inner_text()
                    like_match = re.search(r'(\d+)', like_text)
                    if like_match:
                        article['likes'] = int(like_match.group(1))
                
                # 过滤：只要有文章链接就保留，到详情页再判断
                if article['link']:
                    articles.append(article)
                    self.logger.info(f"  [{len(articles)}] {article['title'][:30] if article['title'] else '待获取标题'}...")
                
            except Exception as e:
                self.logger.warning(f"解析动态 {i+1} 失败: {e}")
                continue
        
        return articles
    
    def _parse_article_detail(self, page: Page, url: str) -> dict:
        """解析文章详情"""
        detail = {
            'url': url,
            'title': '',
            'author': '',
            'publish_time': '',
            'content': '',
            'likes': 0,
            'comments': 0,
        }
        
        try:
            # 先导航到文章页面
            page.goto(url, timeout=30000)
            page.wait_for_load_state('networkidle', timeout=15000)
            page.wait_for_timeout(2000)  # 额外等待
            
            # 从页面标题提取（格式：标题 - 雪球）
            page_title = page.title()
            self.logger.debug(f"页面标题: {page_title}")
            if page_title:
                # 统一处理：找到 "雪球" 并分割
                if '雪球' in page_title:
                    # 找到雪球的位置，取前面的部分
                    idx = page_title.find('雪球')
                    title = page_title[:idx].strip()
                    # 去掉可能的分隔符
                    title = title.rstrip('-').rstrip('—').rstrip('–').strip()
                    self.logger.debug(f"提取标题: {title[:50]}...")
                else:
                    title = page_title.strip()
                
                # 如果标题很长，截取前100字符
                if len(title) > 100:
                    title = title[:100] + '...'
                detail['title'] = title
                self.logger.info(f"提取标题: {title[:50]}...")
            
            # 获取作者
            author_elem = page.query_selector('.article__bd__from a, .user-name, .author-name, .status-content a[href*="/u/"]')
            if author_elem:
                detail['author'] = author_elem.inner_text().strip()
            
            # 获取时间
            time_elem = page.query_selector('.article__bd__from .date, .time, .date, .status-content .time')
            if time_elem:
                detail['publish_time'] = time_elem.inner_text().strip()
            
            # 获取正文 - 使用正确选择器
            content_elem = page.query_selector('.article__bd__detail')
            if content_elem:
                detail['content'] = content_elem.inner_text().strip()
                self.logger.info(f"提取正文: {len(detail['content'])} 字符")
            else:
                # 对于短状态，尝试其他选择器
                self.logger.warning("未找到 .article__bd__detail，尝试备选选择器")
                for sel in ['.status-content', '.article-content', '.status__content', 'article']:
                    elem = page.query_selector(sel)
                    if elem:
                        text = elem.inner_text().strip()
                        if len(text) > 20:
                            detail['content'] = text
                            self.logger.info(f"备选选择器 {sel}: {len(text)} 字符")
                            break
                
                # 如果还是没有内容，从页面标题获取（短状态）
                if not detail['content'] and detail['title']:
                    detail['content'] = detail['title']
                    self.logger.warning(f"使用标题作为内容: {detail['content'][:50]}...")
            
            # 获取互动数据
            page_text = page.content()
            like_match = re.search(r'"likeCount":(\d+)', page_text)
            if like_match:
                detail['likes'] = int(like_match.group(1))
            
            comment_match = re.search(r'"commentCount":(\d+)', page_text)
            if comment_match:
                detail['comments'] = int(comment_match.group(1))
            
        except Exception as e:
            self.logger.error(f"解析文章详情失败: {e}")
        
        return detail
    
    def _save_as_markdown(self, article: dict, user_id: str) -> str:
        """保存为 Markdown"""
        user_dir = self.data_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        
        article_id = article.get('article_id', 'unknown')
        filename = f"{article_id}.md"
        filepath = user_dir / filename
        
        # 构建 Markdown 内容
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
    
    def _get_user_name(self, page: Page, user_id: str) -> str:
        """从用户主页获取用户名称"""
        try:
            # 在用户主页上获取用户名
            name_elem = page.query_selector('.user-name, .username, .profile__name')
            if name_elem:
                name = name_elem.inner_text().strip()
                if name:
                    self.logger.info(f"获取用户名: {name}")
                    return name
            
            # 备选：从页面标题获取
            page_title = page.title()
            if '的雪球专栏' in page_title:
                name = page_title.split('的雪球专栏')[0].strip()
                if name:
                    return name
                    
        except Exception as e:
            self.logger.warning(f"获取用户名失败: {e}")
        
        return user_id  # 返回ID作为默认值
    
    def crawl_user(self, user_id: str, url: str) -> List[dict]:
        """爬取单个用户的文章"""
        self.logger.info(f"开始爬取用户: {user_id}")
        
        articles = []
        max_articles = self.config.get('crawler', {}).get('max_articles', 20)
        
        with sync_playwright() as p:
            browser, context = self._create_browser_context(p)
            page = context.new_page()
            
            try:
                # 先访问首页建立 cookies
                self.logger.info("访问雪球首页...")
                page.goto('https://xueqiu.com', timeout=30000)
                page.wait_for_timeout(2000)
                
                # 访问用户主页
                self.logger.info(f"访问用户主页: {url}")
                page.goto(url, timeout=30000)
                page.wait_for_timeout(3000)
                
                # 解析文章列表
                article_list = self._parse_article_list(page, user_id)
                
                # 遍历每篇文章获取详情
                for i, article in enumerate(article_list[:max_articles]):
                    if article['link']:
                        self._random_delay()
                        
                        self.logger.info(f"获取文章详情 [{i+1}/{min(len(article_list), max_articles)}]: {article['link']}")
                        
                        detail = self._parse_article_detail(page, article['link'])
                        
                        # 合并信息
                        article.update(detail)
                        article['crawl_time'] = datetime.now().isoformat()
                        
                        # 检查是否已存在
                        article_id = article.get('article_id', '')
                        if article_id in self.index.get('articles', {}):
                            self.logger.info(f"文章已存在，跳过: {article_id}")
                            continue
                        
                        # 保存为 Markdown
                        filepath = self._save_as_markdown(article, user_id)
                        article['filepath'] = filepath
                        
                        # 更新索引
                        self.index['articles'][article_id] = {
                            'title': article.get('title', ''),
                            'author': article.get('author', ''),
                            'publish_time': article.get('publish_time', ''),
                            'user_id': user_id,
                            'crawl_time': article.get('crawl_time'),
                            'filepath': filepath,
                        }
                        
                        articles.append(article)
                
                self._save_index()
                
            except Exception as e:
                self.logger.error(f"爬取用户 {user_id} 失败: {e}")
                import traceback
                traceback.print_exc()
                
            finally:
                browser.close()
        
        self.logger.info(f"用户 {user_id} 爬取完成，获取 {len(articles)} 篇新文章")
        return articles
    
    def run(self):
        """运行爬虫"""
        self.logger.info("="*50)
        self.logger.info("雪球爬虫启动")
        self.logger.info("="*50)
        
        all_articles = []
        
        for account in self.accounts:
            user_id = account.get('id')
            url = account.get('url')
            
            if not user_id or not url:
                self.logger.warning(f"账号配置不完整: {account}")
                continue
            
            articles = self.crawl_user(user_id, url)
            all_articles.extend(articles)
            
            # 用户间延迟
            if account != self.accounts[-1]:
                self._random_delay()
        
        self.logger.info("="*50)
        self.logger.info(f"爬取完成，共获取 {len(all_articles)} 篇新文章")
        self.logger.info("="*50)
        
        return all_articles


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='雪球专栏文章爬虫')
    parser.add_argument('--config', '-c', help='配置文件路径')
    parser.add_argument('--user', '-u', help='指定用户ID')
    
    args = parser.parse_args()
    
    crawler = XueqiuCrawler(args.config)
    
    if args.user:
        # 爬取指定用户
        for account in crawler.accounts:
            if account.get('id') == args.user:
                crawler.crawl_user(args.user, account.get('url'))
                break
        else:
            print(f"未找到用户: {args.user}")
    else:
        # 爬取所有用户
        crawler.run()


if __name__ == '__main__':
    main()