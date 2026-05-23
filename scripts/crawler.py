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
from functools import wraps
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


# 默认常量
DEFAULT_MAX_ARTICLES = 20
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 5]


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
        self.timeout = self.config.get('crawler', {}).get('timeout', 60) * 1000  # ms
        
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
    
    def _load_cookies(self, context):
        """加载已保存的 cookies 到浏览器上下文"""
        cookies_file = self.project_root / 'config' / 'xueqiu_cookies.json'
        if not cookies_file.exists():
            return
        try:
            with open(cookies_file, 'r') as f:
                data = json.load(f)
            cookies = data.get('cookies', {})
            if cookies:
                cookie_list = [
                    {"name": k, "value": v, "domain": ".xueqiu.com", "path": "/"}
                    for k, v in cookies.items()
                ]
                context.add_cookies(cookie_list)
                self.logger.info(f"已加载 {len(cookie_list)} 个 cookies")
        except Exception as e:
            self.logger.warning(f"加载 cookies 失败: {e}")
    
    def _random_delay(self):
        """随机延迟"""
        delay_min = self.config.get('crawler', {}).get('delay_min', 2)
        delay_max = self.config.get('crawler', {}).get('delay_max', 5)
        delay = random.uniform(delay_min, delay_max)
        self.logger.debug(f"等待 {delay:.1f} 秒...")
        import time
        time.sleep(delay)
    
    def _create_browser_context(self, playwright) -> BrowserContext:
        """创建浏览器上下文（全面反检测）"""
        anti_detect = self.config.get('anti_detect', {})
        
        # 随机 User-Agent（降低指纹一致性）
        ua_pool = anti_detect.get('user_agents', [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0',
        ])
        user_agent = random.choice(ua_pool)
        
        viewport = anti_detect.get('viewport', {'width': 1920, 'height': 1080})
        # 随机微调分辨率（真实用户不会精确到 1920x1080）
        viewport['width'] += random.randint(-10, 10)
        
        browser = playwright.chromium.launch(
            headless="new",  # Chromium new headless mode, harder to detect
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-features=IsolateOrigins,site-per-process',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-infobars',
                '--disable-setuid-sandbox',
                '--disable-web-security',
                '--disable-features=VizDisplayCompositor',
                '--ignore-certificate-errors',
                f'--window-size={viewport["width"]},{viewport["height"]}',
            ]
        )
        
        context = browser.new_context(
            viewport=viewport,
            user_agent=user_agent,
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
            permissions=['geolocation'],
            geolocation={'latitude': 31.2304, 'longitude': 121.4737},  # 上海
            extra_http_headers=anti_detect.get('extra_headers', {
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'Sec-Ch-Ua': '"Google Chrome";v="131", "Chromium";v="131", "Not?A_Brand";v="24"',
                'Sec-Ch-Ua-Mobile': '?0',
                'Sec-Ch-Ua-Platform': '"Windows"',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Upgrade-Insecure-Requests': '1',
            })
        )
        
        # 全面反检测脚本
        context.add_init_script("""
// ===== 核心反检测 =====
Object.defineProperty(navigator, 'webdriver', { get: () => false });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };

// ===== Navigator 伪装 =====
Object.defineProperty(navigator, 'plugins', { 
    get: () => {
        const arr = new Array(5);
        arr.item = i => arr[i];
        arr.namedItem = n => null;
        arr.refresh = () => {};
        return arr;
    }
});
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.' });
Object.defineProperty(navigator, 'productSub', { get: () => '20030107' });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
Object.defineProperty(navigator, 'language', { get: () => 'zh-CN' });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });

// ===== Permissions =====
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);
Object.defineProperty(Notification, 'permission', { get: () => 'default' });

// ===== WebGL 伪装 (常用指纹向量) =====
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';      // UNMASKED_VENDOR_WEBGL
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';  // UNMASKED_RENDERER_WEBGL
    return getParameter.call(this, parameter);
};

// ===== Canvas 指纹扰动 =====
const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    const context = this.getContext('2d');
    if (context) {
        const imageData = context.getImageData(0, 0, this.width, this.height);
        // 在第一个像素加微小噪声
        if (imageData.data.length > 0) {
            imageData.data[0] = imageData.data[0] ^ 1;
        }
    }
    return originalToDataURL.apply(this, arguments);
};

// ===== 隐藏 Headless 特征 =====
if (navigator.connection) {
    Object.defineProperty(navigator.connection, 'rtt', { get: () => 100 });
}

// ===== 覆盖 iframe contentWindow 检测 =====
const originalCreateElement = document.createElement.bind(document);
document.createElement = function(tagName, options) {
    const element = originalCreateElement(tagName, options);
    if (tagName.toLowerCase() === 'iframe') {
        const originalGet = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow').get;
        Object.defineProperty(element, 'contentWindow', {
            get: function() {
                const win = originalGet.call(this);
                if (win && !win.chrome) {
                    win.chrome = { runtime: {} };
                }
                return win;
            }
        });
    }
    return element;
};

// ===== 屏幕属性 =====
Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
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
            page.goto(url, timeout=self.timeout)
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
            author_elem = page.query_selector('.article__bd__from a, .author-name, .status-content a[href*="/u/"]')
            if author_elem:
                author = author_elem.inner_text().strip()
                # 清理：去掉 "的雪球专栏" 等后缀
                for suffix in ['的雪球专栏', '的专栏']:
                    if suffix in author:
                        author = author.split(suffix)[0]
                        break
                detail['author'] = author
            if not detail['author']:
                # Fallback: 使用用户主页名字
                user_name_elem = page.query_selector('.user-name')
                if user_name_elem:
                    name = user_name_elem.inner_text().strip()
                    for suffix in ['的雪球专栏', '的专栏']:
                        if suffix in name:
                            name = name.split(suffix)[0]
                            break
                    detail['author'] = name
            
            # 获取时间
            time_elem = page.query_selector('.article__bd__from .date, .time, .date, .status-content .time')
            if time_elem:
                detail['publish_time'] = time_elem.inner_text().strip()
            
            # 获取正文 - 使用正确选择器
            content_elem = page.query_selector('.article__bd__detail')
            if content_elem:
                detail['content'] = content_elem.inner_text().strip()
                self.logger.info(f"提取正文: {len(detail['content'])} 字符")
                # 标记为专栏文章
                detail['is_column'] = True
            else:
                # 对于短状态，尝试其他选择器
                self.logger.warning("未找到 .article__bd__detail，非专栏文章")
                detail['is_column'] = False
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
    
    def _update_account_name(self, user_id: str, name: str):
        """更新账号配置中的用户名"""
        try:
            accounts_path = self.project_root / 'config' / 'accounts.yaml'
            with open(accounts_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            # 更新账号名称
            for account in data.get('accounts', []):
                if account.get('id') == user_id:
                    if account.get('name') in ['待确认', user_id]:
                        account['name'] = name
                        self.logger.info(f"更新账号名称: {user_id} -> {name}")
                        
                        # 保存配置
                        with open(accounts_path, 'w', encoding='utf-8') as f:
                            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
                        return True
                    break
        except Exception as e:
            self.logger.warning(f"更新账号名称失败: {e}")
        
        return False
    
    def _check_and_handle_login(self, page: Page) -> bool:
        """检查是否需要登录，如果需要则展示二维码"""
        try:
            # 检查是否有登录提示或验证码
            page_content = page.content()
            
            # 检查是否在验证页面
            if 'Verification' in page.title() or '验证' in page_content:
                self.logger.warning("检测到验证页面，需要人工验证")
                
                # 尝试查找二维码
                qr_elem = page.query_selector('img.qrcode, img[src*="qrcode"], canvas')
                if qr_elem:
                    # 获取二维码图片
                    qr_src = qr_elem.get_attribute('src')
                    if qr_src:
                        self.logger.info("发现二维码，请扫描登录")
                        # 这里可以保存二维码图片或发送给用户
                        return True
                
                # 查找登录按钮
                login_btn = page.query_selector('button.login, a.login, .login-btn')
                if login_btn:
                    self.logger.info("点击登录按钮...")
                    login_btn.click()
                    page.wait_for_timeout(2000)
                    
                    # 检查是否有二维码
                    qr_elem = page.query_selector('img.qrcode, img[src*="qrcode"]')
                    if qr_elem:
                        self.logger.info("请扫描二维码登录")
                        return True
            
            return False
            
        except Exception as e:
            self.logger.warning(f"检查登录状态失败: {e}")
            return False
    
    def crawl_user(self, user_id: str, url: str) -> List[dict]:
        """爬取单个用户的文章"""
        self.logger.info(f"开始爬取用户: {user_id}")
        
        articles = []
        max_articles = self.config.get('crawler', {}).get('max_articles', 20)
        
        with sync_playwright() as p:
            browser, context = self._create_browser_context(p)
            page = context.new_page()
            
            try:
                # 加载已保存的 cookies
                self._load_cookies(context)
                
                # 先访问首页建立 cookies
                self.logger.info("访问雪球首页...")
                page.goto('https://xueqiu.com', timeout=self.timeout)
                page.wait_for_timeout(2000)
                
                # 访问用户主页
                self.logger.info(f"访问用户主页: {url}")
                page.goto(url, timeout=self.timeout)
                page.wait_for_timeout(3000)
                
                # 检查是否需要登录/验证
                if self._check_and_handle_login(page):
                    self.logger.warning("需要人工验证，等待...")
                    page.wait_for_timeout(10000)  # 等待用户扫描
                
                # 获取用户名并更新配置
                user_name = self._get_user_name(page, user_id)
                if user_name and user_name != user_id:
                    self._update_account_name(user_id, user_name)
                
                # 解析文章列表
                article_list = self._parse_article_list(page, user_id)
                
                # 获取历史文章ID，用于增量判断
                history_ids = self._get_history_article_ids(user_id)
                if history_ids:
                    self.logger.info(f"历史已爬取 {len(history_ids)} 篇文章")
                
                # 过滤出新增文章
                new_articles = []
                for article in article_list:
                    article_id = article.get('article_id', '')
                    if article_id and article_id not in history_ids:
                        new_articles.append(article)
                
                self.logger.info(f"发现 {len(new_articles)} 篇新文章（共 {len(article_list)} 篇）")
                
                # 遍历每篇新文章获取详情
                for i, article in enumerate(new_articles[:max_articles]):
                    if article['link']:
                        self._random_delay()
                        
                        self.logger.info(f"获取文章详情 [{i+1}/{min(len(article_list), max_articles)}]: {article['link']}")
                        
                        detail = self._parse_article_detail(page, article['link'])
                        
                        # 合并信息
                        article.update(detail)
                        article['crawl_time'] = datetime.now().isoformat()
                        
                        # 判断是否是专栏文章（非评论/回复）
                        is_column = article.get('is_column', False)
                        title = article.get('title', '')
                        
                        # 过滤：标题以"回复@"开头的不是专栏文章
                        if title.startswith('回复@') or not is_column:
                            self.logger.info(f"跳过非专栏文章: {title[:30]}...")
                            continue
                        
                        # 检查是否已存在
                        article_id = article.get('article_id', '')
                        if article_id in self.index.get('articles', {}):
                            self.logger.info(f"文章已存在，跳过: {article_id}")
                            continue
                        
                        # 保存为 Markdown
                        filepath = self._save_as_markdown(article, user_id)
                        article['filepath'] = filepath
                        
                        # 更新索引（统一用 user_id_article_id 作为 key）
                        index_key = f"{user_id}_{article_id}"
                        self.index['articles'][index_key] = {
                            'article_id': article_id,
                            'user_id': user_id,
                            'title': article.get('title', ''),
                            'author': article.get('author', ''),
                            'publish_time': article.get('publish_time', ''),
                            'crawl_time': article.get('crawl_time'),
                            'file_path': filepath,
                            'filepath': filepath,
                        }
                        
                        articles.append(article)
                
                self._save_index()
                
                # 保存历史快照
                self._save_history(user_id, articles)
                
            except Exception as e:
                self.logger.error(f"爬取用户 {user_id} 失败: {e}")
                import traceback
                traceback.print_exc()
                
            finally:
                try:
                    browser.close()
                except Exception:
                    pass  # EPIPE / 浏览器已崩溃
        
        self.logger.info(f"用户 {user_id} 爬取完成，获取 {len(articles)} 篇新文章")
        return articles
    
    def crawl_all_users(self, max_articles: int = None, fetch_detail: bool = True) -> dict:
        """
        爬取所有配置用户（Playwright）
        
        Args:
            max_articles: 每个用户最大文章数
            fetch_detail: 是否爬取详情
        
        Returns:
            爬取统计
        """
        max_articles = max_articles or self.config.get('crawler', {}).get('max_articles', 20)
        
        stats = {
            'total_users': len(self.accounts),
            'total_new': 0,
            'total_saved': 0,
            'users': []
        }
        
        for account in self.accounts:
            user_id = account.get('id')
            user_name = account.get('name', user_id)
            url = account.get('url', f'https://xueqiu.com/u/{user_id}')
            
            if not user_id:
                self.logger.warning(f"账号配置不完整: {account}")
                continue
            
            self.logger.info(f"\n{'='*50}")
            self.logger.info(f"爬取用户: {user_name} ({user_id})")
            
            try:
                articles = self.crawl_user(user_id, url)
                stats['total_new'] += len(articles)
                stats['total_saved'] += len(articles)
                stats['users'].append({
                    'user_id': user_id,
                    'name': user_name,
                    'new_articles': len(articles),
                    'saved_articles': len(articles)
                })
                
            except Exception as e:
                self.logger.error(f"爬取用户 {user_id} 失败: {e}")
                stats['users'].append({
                    'user_id': user_id,
                    'name': user_name,
                    'error': str(e)
                })
            
            # 用户间延迟
            if account != self.accounts[-1]:
                self._random_delay()
        
        # 打印统计
        self.logger.info(f"\n{'='*50}")
        self.logger.info(f"爬取完成！")
        self.logger.info(f"总用户数: {stats['total_users']}")
        self.logger.info(f"新文章数: {stats['total_new']}")
        self.logger.info(f"保存文章数: {stats['total_saved']}")
        
        # 保存爬取统计供日报使用
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
            self.logger.info(f"爬取统计已保存: {stats_file}")
        except OSError as e:
            self.logger.warning(f"保存爬取统计失败: {e}")
        
        return stats
    
    def run(self):
        """运行爬虫（兼容旧接口）"""
        self.logger.info("="*50)
        self.logger.info("雪球爬虫启动")
        self.logger.info("="*50)
        return self.crawl_all_users()


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='雪球专栏文章爬虫 (Playwright)')
    parser.add_argument('--config', '-c', help='配置文件路径')
    parser.add_argument('--user', '-u', help='指定用户ID')
    parser.add_argument('-a', '--all', action='store_true', help='爬取所有用户')
    parser.add_argument('-m', '--max', type=int, default=20, help='最大文章数')
    parser.add_argument('-d', '--detail', action='store_true', help='爬取文章详情（默认已启用）')
    
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
        crawler.crawl_all_users(max_articles=args.max)


if __name__ == '__main__':
    main()