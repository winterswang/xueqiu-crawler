#!/usr/bin/env python3
"""
雪球专栏文章爬虫 - XCrawl 版本

基于 XCrawl API 的云端爬取方案，替代本地 Playwright 版本。

功能:
- 使用 XCrawl API 爬取雪球用户主页文章列表
- 支持文章详情爬取
- 增量更新，避免重复爬取
- 完善的错误处理和重试机制
- 与现有数据格式兼容
"""

import os
import sys
import json
import yaml
import time
import logging
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from functools import wraps

# 添加项目根目录到 path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import requests
except ImportError:
    print("请先安装 requests: pip install requests")
    sys.exit(1)


# ============================================================
# 配置和常量
# ============================================================

XCRAWL_API_URL = "https://run.xcrawl.com/v1/scrape"
XCRAWL_CONFIG_FILE = Path.home() / ".xcrawl" / "config.json"

# 重试配置
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 5]  # 秒

# 爬取配置
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_ARTICLES = 20


# ============================================================
# 日志配置
# ============================================================

def setup_logging(config: dict) -> logging.Logger:
    """配置日志"""
    log_dir = PROJECT_ROOT / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    
    log_file = log_dir / 'crawler_xcrawl.log'
    
    logging.basicConfig(
        level=getattr(logging, config.get('logging', {}).get('level', 'INFO')),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


# ============================================================
# XCrawl API 客户端
# ============================================================

class XCrawlClient:
    """XCrawl API 客户端"""
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or self._load_api_key()
        if not self.api_key:
            raise ValueError("XCrawl API Key 未配置，请检查 ~/.xcrawl/config.json")
    
    def _load_api_key(self) -> Optional[str]:
        """从配置文件加载 API Key"""
        if XCRAWL_CONFIG_FILE.exists():
            with open(XCRAWL_CONFIG_FILE, 'r') as f:
                config = json.load(f)
                return config.get('XCRAWL_API_KEY')
        return None
    
    @staticmethod
    def is_configured() -> bool:
        """检查 XCrawl 是否已配置"""
        return XCRAWL_CONFIG_FILE.exists()
    
    def scrape(
        self,
        url: str,
        formats: List[str] = None,
        json_prompt: str = None,
        cookies: Dict = None,
        headers: Dict = None,
        js_render: bool = True,
        timeout: int = DEFAULT_TIMEOUT
    ) -> Dict:
        """
        调用 XCrawl Scrape API
        
        Args:
            url: 目标 URL
            formats: 输出格式，如 ["markdown", "json"]
            json_prompt: JSON 提取提示词
            cookies: 请求 cookies
            headers: 请求头
            js_render: 是否启用 JS 渲染
            timeout: 超时时间
        
        Returns:
            API 响应结果
        """
        # 构建请求体
        body = {
            "url": url,
            "mode": "sync",
            "output": {
                "formats": formats or ["markdown"]
            }
        }
        
        # JS 渲染配置
        if js_render:
            body["js_render"] = {
                "enabled": True,
                "wait_until": "networkidle"
            }
        
        # 请求配置
        request_config = {}
        if cookies:
            request_config["cookies"] = cookies
        if headers:
            request_config["headers"] = headers
        
        if request_config:
            body["request"] = request_config
        
        # JSON 提取配置
        if json_prompt and "json" in (formats or []):
            body["output"]["json"] = {"prompt": json_prompt}
        
        # 发送请求
        response = requests.post(
            XCRAWL_API_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            },
            json=body,
            timeout=timeout
        )
        
        response.raise_for_status()
        return response.json()


# ============================================================
# 重试装饰器
# ============================================================

def retry(max_retries: int = MAX_RETRIES, delays: List[int] = RETRY_DELAYS):
    """重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if i < max_retries - 1:
                        delay = delays[i] if i < len(delays) else delays[-1]
                        logging.warning(f"第 {i+1} 次重试，等待 {delay} 秒... 错误: {e}")
                        time.sleep(delay)
            raise last_error
        return wrapper
    return decorator


# ============================================================
# 雪球爬虫类
# ============================================================

class XueqiuXCrawlCrawler:
    """雪球爬虫 - XCrawl 版本"""
    
    def __init__(self, config_path: str = None):
        self.config = self._load_config(config_path)
        self.logger = setup_logging(self.config)
        self.accounts = self._load_accounts()
        self.xcrawl = XCrawlClient()
        
        # 数据目录
        self.data_dir = PROJECT_ROOT / self.config.get('storage', {}).get('output_dir', 'data')
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 索引文件
        self.index_file = self.data_dir / 'index.json'
        self.index = self._load_index()
        
        # Cookies 管理
        self.cookies_file = PROJECT_ROOT / 'config' / 'xueqiu_cookies.json'
    
    # --------------------------------------------------------
    # 配置加载
    # --------------------------------------------------------
    
    def _load_config(self, config_path: str = None) -> dict:
        """加载配置"""
        if config_path is None:
            config_path = PROJECT_ROOT / 'config' / 'config.yaml'
        else:
            config_path = Path(config_path)
        
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        return {}
    
    def _load_accounts(self) -> List[dict]:
        """加载账号配置"""
        accounts_path = PROJECT_ROOT / 'config' / 'accounts.yaml'
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
    
    # --------------------------------------------------------
    # Cookies 管理
    # --------------------------------------------------------
    
    def load_cookies(self) -> Optional[Dict]:
        """加载保存的 cookies"""
        if self.cookies_file.exists():
            with open(self.cookies_file, 'r') as f:
                data = json.load(f)
            
            # 检查是否过期
            expires_at = data.get('expires_at')
            if expires_at:
                if datetime.now() > datetime.fromisoformat(expires_at):
                    self.logger.warning("Cookies 已过期，请重新登录")
                    return None
            
            return data.get('cookies')
        return None
    
    def save_cookies(self, cookies: Dict, expire_days: int = 30):
        """保存 cookies"""
        data = {
            'cookies': cookies,
            'created_at': datetime.now().isoformat(),
            'expires_at': datetime.now().replace(
                day=datetime.now().day + expire_days
            ).isoformat()
        }
        
        with open(self.cookies_file, 'w') as f:
            json.dump(data, f, indent=2)
        
        self.logger.info(f"Cookies 已保存到 {self.cookies_file}")
    
    # --------------------------------------------------------
    # 文章列表爬取
    # --------------------------------------------------------
    
    @retry()
    def crawl_article_list(
        self,
        user_id: str,
        max_articles: int = DEFAULT_MAX_ARTICLES
    ) -> List[dict]:
        """
        爬取用户主页的文章列表
        
        Args:
            user_id: 雪球用户 ID
            max_articles: 最大文章数
        
        Returns:
            文章列表
        """
        url = f"https://xueqiu.com/u/{user_id}"
        
        self.logger.info(f"爬取文章列表: {url}")
        
        # JSON 提取提示词 - 简化版
        json_prompt = """
提取页面中的文章链接，返回 JSON 对象：
{
  "articles": [
    {
      "link": "文章完整链接",
      "title": "文章标题或第一句话"
    }
  ]
}

要求：
1. 只要格式为 https://xueqiu.com/数字/长数字 的文章链接
2. 排除用户主页链接（只有用户ID没有文章ID的）
3. 排除 PDF、图片等非文章链接
4. 去重
"""
        
        # 加载 cookies
        cookies = self.load_cookies()
        
        try:
            # 调用 XCrawl API
            response = self.xcrawl.scrape(
                url=url,
                formats=["json", "markdown"],
                json_prompt=json_prompt,
                cookies=cookies,
                js_render=True
            )
            
            # 解析结果
            if response.get('status') == 'completed':
                data = response.get('data', {})
                
                # 获取 JSON 提取结果
                articles_json = data.get('json', {})
                
                # DEBUG: 打印原始返回
                self.logger.info(f"XCrawl JSON 返回类型: {type(articles_json)}")
                if isinstance(articles_json, dict):
                    self.logger.info(f"articles 字段: {articles_json.get('articles', [])[:3]}")
                
                # 支持两种格式：{"articles": [...]} 或 [...]
                if isinstance(articles_json, dict):
                    articles_list = articles_json.get('articles', [])
                elif isinstance(articles_json, list):
                    articles_list = articles_json
                else:
                    self.logger.warning(f"JSON 提取结果格式异常: {type(articles_json)}")
                    return []
                
                articles = []
                seen_ids = set()  # 去重
                
                for item in articles_list[:max_articles * 2]:  # 多取一些，因为会过滤
                    link = item.get('link', '')
                    
                    # 从链接中提取用户ID和文章ID
                    # 格式: https://xueqiu.com/用户ID/文章ID
                    import re
                    match = re.search(r'/(\d+)/(\d+)$', link)
                    if not match:
                        continue
                    
                    link_user_id = match.group(1)
                    article_id = match.group(2)
                    
                    # 只保留目标用户的文章（排除转发）
                    if link_user_id != user_id:
                        self.logger.debug(f"跳过转发文章: {link} (用户 {link_user_id})")
                        continue
                    
                    # 去重
                    if article_id in seen_ids:
                        continue
                    seen_ids.add(article_id)
                    
                    # 过滤无效链接
                    if '/u/' in link or 'stockn' in link or len(article_id) < 8:
                        continue
                    
                    article = {
                        'user_id': user_id,
                        'article_id': article_id,
                        'title': item.get('title', '').strip() or f"文章 {article_id}",
                        'link': link,
                        'publish_time': item.get('publish_time', ''),
                        'likes': int(item.get('likes', 0) or 0),
                        'comments': int(item.get('comments', 0) or 0),
                        'summary': item.get('summary', '')[:100],
                        'crawl_time': datetime.now().isoformat(),
                        'crawl_source': 'xcrawl'
                    }
                    
                    articles.append(article)
                    
                    if len(articles) >= max_articles:
                        break
                
                self.logger.info(f"获取到 {len(articles)} 篇文章")
                return articles
            else:
                self.logger.error(f"XCrawl 爬取失败: {response.get('status')}")
                return []
                
        except requests.exceptions.RequestException as e:
            self.logger.error(f"XCrawl API 调用失败: {e}")
            raise
    
    # --------------------------------------------------------
    # 文章详情爬取
    # --------------------------------------------------------
    
    @retry()
    def crawl_article_detail(self, article_url: str) -> dict:
        """
        爬取文章详情
        
        Args:
            article_url: 文章 URL
        
        Returns:
            文章详情
        """
        self.logger.info(f"爬取文章详情: {article_url}")
        
        cookies = self.load_cookies()
        
        try:
            response = self.xcrawl.scrape(
                url=article_url,
                formats=["markdown"],
                cookies=cookies,
                js_render=True
            )
            
            if response.get('status') == 'completed':
                data = response.get('data', {})
                markdown = data.get('markdown', '')
                
                return {
                    'url': article_url,
                    'content': markdown,
                    'content_type': 'markdown',
                    'word_count': len(markdown),
                    'crawl_time': datetime.now().isoformat(),
                    'crawl_source': 'xcrawl'
                }
            else:
                self.logger.error(f"XCrawl 爬取失败: {response.get('status')}")
                return None
                
        except requests.exceptions.RequestException as e:
            self.logger.error(f"XCrawl API 调用失败: {e}")
            raise
    
    # --------------------------------------------------------
    # 增量更新
    # --------------------------------------------------------
    
    def get_existing_article_ids(self, user_id: str) -> set:
        """获取已爬取的文章 ID"""
        existing_ids = set()
        
        # 从索引中获取
        for article_key, article_data in self.index.get('articles', {}).items():
            if article_data.get('user_id') == user_id:
                existing_ids.add(article_data.get('article_id'))
        
        return existing_ids
    
    def crawl_user_incremental(
        self,
        user_id: str,
        max_articles: int = DEFAULT_MAX_ARTICLES,
        fetch_detail: bool = False
    ) -> Tuple[int, int]:
        """
        增量爬取用户文章
        
        Args:
            user_id: 用户 ID
            max_articles: 最大文章数
            fetch_detail: 是否爬取详情
        
        Returns:
            (新文章数, 总爬取数)
        """
        self.logger.info(f"增量爬取用户: {user_id}")
        
        # 获取已有文章 ID
        existing_ids = self.get_existing_article_ids(user_id)
        self.logger.info(f"已有文章数: {len(existing_ids)}")
        
        # 爬取文章列表
        articles = self.crawl_article_list(user_id, max_articles)
        
        # 过滤新文章
        new_articles = [
            a for a in articles 
            if a['article_id'] not in existing_ids
        ]
        
        self.logger.info(f"新文章数: {len(new_articles)}")
        
        # 爬取详情并保存
        saved_count = 0
        for article in new_articles[:max_articles]:
            try:
                # 爬取详情（可选）
                if fetch_detail:
                    detail = self.crawl_article_detail(article['link'])
                    if detail:
                        article['content'] = detail.get('content', '')
                        article['word_count'] = detail.get('word_count', 0)
                
                # 保存文章
                self._save_article(user_id, article)
                saved_count += 1
                
                # 更新索引
                article_key = f"{user_id}_{article['article_id']}"
                self.index['articles'][article_key] = {
                    'article_id': article['article_id'],
                    'user_id': user_id,
                    'title': article['title'],
                    'crawl_time': article['crawl_time'],
                    'file_path': f"data/{user_id}/{article['article_id']}.md"
                }
                
                # 延迟避免过快
                time.sleep(0.5)
                
            except Exception as e:
                self.logger.error(f"保存文章失败 {article['article_id']}: {e}")
                continue
        
        # 保存索引
        self._save_index()
        
        return saved_count, len(new_articles)
    
    # --------------------------------------------------------
    # 文章保存
    # --------------------------------------------------------
    
    def _save_article(self, user_id: str, article: dict) -> Path:
        """保存文章到文件"""
        # 创建用户目录
        user_dir = self.data_dir / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        
        # 文件路径
        article_id = article.get('article_id', 'unknown')
        filepath = user_dir / f"{article_id}.md"
        
        # 构建 Markdown 内容
        lines = [
            f"# {article.get('title', '无标题')}",
            "",
            f"> 作者ID: {user_id}",
            f"> 发布时间: {article.get('publish_time', '未知')}",
            f"> 点赞: {article.get('likes', 0)} | 评论: {article.get('comments', 0)}",
            f"> 原文链接: {article.get('link', '')}",
            "",
            "---",
            "",
            article.get('summary', ''),
            "",
        ]
        
        # 如果有详情内容
        if article.get('content'):
            lines.extend([
                "---",
                "",
                article.get('content', ''),
                "",
            ])
        
        lines.extend([
            "---",
            "",
            f"*爬取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
            f"*爬取方式: XCrawl*"
        ])
        
        # 写入文件
        content = '\n'.join(lines)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        self.logger.info(f"保存文章: {filepath}")
        return filepath
    
    # --------------------------------------------------------
    # 批量爬取
    # --------------------------------------------------------
    
    def crawl_all_users(
        self,
        max_articles: int = DEFAULT_MAX_ARTICLES,
        fetch_detail: bool = False
    ) -> dict:
        """
        爬取所有配置用户
        
        Args:
            max_articles: 每个用户最大文章数
            fetch_detail: 是否爬取详情
        
        Returns:
            爬取统计
        """
        stats = {
            'total_users': len(self.accounts),
            'total_new': 0,
            'total_saved': 0,
            'users': []
        }
        
        for account in self.accounts:
            user_id = account.get('id')
            user_name = account.get('name', user_id)
            
            self.logger.info(f"\n{'='*50}")
            self.logger.info(f"爬取用户: {user_name} ({user_id})")
            
            try:
                saved, new = self.crawl_user_incremental(
                    user_id, 
                    max_articles,
                    fetch_detail
                )
                
                stats['total_new'] += new
                stats['total_saved'] += saved
                stats['users'].append({
                    'user_id': user_id,
                    'name': user_name,
                    'new_articles': new,
                    'saved_articles': saved
                })
                
            except Exception as e:
                self.logger.error(f"爬取用户 {user_id} 失败: {e}")
                stats['users'].append({
                    'user_id': user_id,
                    'name': user_name,
                    'error': str(e)
                })
        
        # 打印统计
        self.logger.info(f"\n{'='*50}")
        self.logger.info(f"爬取完成！")
        self.logger.info(f"总用户数: {stats['total_users']}")
        self.logger.info(f"新文章数: {stats['total_new']}")
        self.logger.info(f"保存文章数: {stats['total_saved']}")
        
        return stats


# ============================================================
# CLI 入口
# ============================================================

def main():
    """CLI 入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='雪球爬虫 - XCrawl 版本')
    parser.add_argument('-u', '--user', help='指定用户 ID')
    parser.add_argument('-a', '--all', action='store_true', help='爬取所有用户')
    parser.add_argument('-m', '--max', type=int, default=DEFAULT_MAX_ARTICLES, help='最大文章数')
    parser.add_argument('-d', '--detail', action='store_true', help='爬取文章详情')
    parser.add_argument('--check', action='store_true', help='检查 XCrawl 配置')
    
    args = parser.parse_args()
    
    # 检查配置
    if args.check:
        if XCrawlClient.is_configured():
            print("✅ XCrawl 已配置")
            client = XCrawlClient()
            print(f"   API Key: {client.api_key[:20]}...")
        else:
            print("❌ XCrawl 未配置，请创建 ~/.xcrawl/config.json")
        return
    
    # 创建爬虫实例
    try:
        crawler = XueqiuXCrawlCrawler()
    except ValueError as e:
        print(f"错误: {e}")
        return
    
    # 执行爬取
    if args.user:
        # 单用户爬取
        saved, new = crawler.crawl_user_incremental(
            args.user, 
            args.max,
            args.detail
        )
        print(f"\n爬取完成: 新文章 {new} 篇，保存 {saved} 篇")
        
    elif args.all:
        # 所有用户爬取
        stats = crawler.crawl_all_users(args.max, args.detail)
        print(f"\n爬取完成:")
        print(f"  总用户数: {stats['total_users']}")
        print(f"  新文章数: {stats['total_new']}")
        print(f"  保存文章数: {stats['total_saved']}")
        
    else:
        parser.print_help()


if __name__ == '__main__':
    main()