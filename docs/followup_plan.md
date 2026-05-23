# xueqiu-crawler 后续优化建议（详细版）

> 基于代码审查和 Phase1-4 优化结果，按优先级排列。

---

## 一、高优先级（1-2 天内可落地，收益显著）

### 1.1 `crawl_all_users` 串行 → 并发

**现状**：`crawler_xcrawl.py:645` 用一个 `for` 循环串行爬取 11 个用户，每个用户内还有一个 0.5 秒的 `time.sleep`（#549）。11 个用户 × 20 篇 × 0.5s ≈ **110 秒** 的任务间延迟。

**方案**：用 `concurrent.futures.ThreadPoolExecutor` 改为并发，控制 max_workers=3~5 避免触发 XCrawl 限流。

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def crawl_all_users(self, max_articles=20, fetch_detail=False):
    stats = {...}
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {
            executor.submit(self.crawl_user_incremental, a['id'], max_articles, fetch_detail): a
            for a in self.accounts
        }
        for future in as_completed(future_map):
            account = future_map[future]
            try:
                saved, new = future.result()
                # 更新 stats
            except Exception as e:
                # 记录单个用户失败
    return stats
```

**预期效果**：总耗时从 ~110s 降到 **20-30s**，同时单个用户失败不影响其他用户。

**风险**：XCrawl API 可能有并发限制。建议 max_workers 初始设为 3，观察 API 响应后再调大。

---

### 1.2 index.json 迁移 SQLite

**现状**：`index.json` 是 ~1.5MB 的大 JSON 文件，`_load_index` 全量读入内存（每次 O(2447)）、`_save_index` 全量写回（O(n) 序列化 + 磁盘 IO）。虽然有 filelock 保护写入，但：
- 查询单篇文章需要遍历全量 O(n)
- 无法做复杂查询（按作者、按日期范围、按分类）
- 1.5MB 全量加载在低配服务器上内存不友好

**方案**：用 SQLite 替代 JSON 文件。Python 内置 `sqlite3`，零依赖。

```sql
CREATE TABLE articles (
    id TEXT PRIMARY KEY,          -- "{user_id}_{article_id}"
    article_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    crawl_time TEXT NOT NULL,
    publish_time TEXT,
    file_path TEXT NOT NULL,
    word_count INTEGER DEFAULT 0,
    priority TEXT DEFAULT 'reference',
    has_detail INTEGER DEFAULT 0
);

CREATE TABLE crawl_history (
    date TEXT PRIMARY KEY,
    new_articles INTEGER,
    total_articles INTEGER
);
```

```python
# 迁移后的 _save_article
def _save_article(self, user_id, article):
    # 写入文章文件（不变）
    filepath = self._write_article_file(user_id, article)
    # 写入数据库
    self.db.execute("""
        INSERT OR REPLACE INTO articles
        (id, article_id, user_id, title, author, crawl_time, publish_time, file_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (f"{user_id}_{article['article_id']}", ...))
    self.db.commit()
```

**收益**：
- 事务安全，天然防并发损坏（SQLite 内置 WAL 模式）
- 查询灵活：`SELECT * FROM articles WHERE crawl_time LIKE '2026-05-23%'`
- 只读时无需全量加载：`SELECT title, file_path FROM articles WHERE user_id = ?`
- 支持 `LIKE`、`ORDER BY`、`GROUP BY` 等复杂查询

**迁移成本**：约 0.5 天。需要写 `convert_index_to_sqlite.py` 迁移脚本，确认数据完整性后再删除旧 JSON。

---

### 1.3 飞书推送改为群机器人 Webhook

**现状**：`publish_daily_report_v3.py:459-472` 通过写文件到 `/tmp/pending_feishu_daily.json`，再由外部进程（feishu-img-tool）消费。这不是真正的推送，而是一种"文件队列"机制：
- 外部消费进程需要保持运行
- 无法确认消息是否成功送达
- 飞书目标用户 ID (`ou_0451c7608...`) 硬编码

**方案**：直接调用飞书群机器人 Webhook。

```python
def send_feishu_webhook(webhook_url, title, content, image_url=None):
    """直接推送到飞书群机器人"""
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [
                {"tag": "markdown", "content": content},
                # 可选图片
            ]
        }
    }
    resp = requests.post(webhook_url, json=payload)
    resp.raise_for_status()
```

**配置**：`webhook_url` 放到 `config.yaml` 或 `.env`：
```yaml
notifications:
  feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
```

**收益**：
- 实时推送，无需外部进程
- 可确认送达（HTTP 200）
- Webhook URL 可配置，换群改配置即可
- 可以发互动卡片（富文本、按钮），不只是纯消息

---

## 二、中优先级（1-3 天，提升工程质量）

### 2.1 GitHub Actions CI 集成

**现状**：零 CI。合并代码时无法确认是否破坏了现有逻辑。

**方案**：在 `.github/workflows/` 下创建 CI 配置。

```yaml
# .github/workflows/ci.yml
name: CI

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          playwright install chromium
      - name: Run tests
        run: python tests/test_core.py
      - name: Lint check
        run: pip install ruff && ruff check scripts/ tests/
```

**收益**：
- 每次 push 自动运行 18 个测试
- 发现未预期回归
- 提升协作者信心

---

### 2.2 InfoCard 生成改进

**现状**：`publish_daily_report_v3.py:493` 调用 `render_infocard(html, card_path)` 生成 PNG 卡片，依赖外部渲染工具（`feishu-img-tool` 或 `puppeteer`），且卡片尺寸 1200×1800 固定。具体渲染逻辑在第 305-395 行。

**问题**：
- 渲染工具在服务器上才有，本地开发无法预览卡片
- 卡片 HTML 是字符串拼接，维护困难
- 没有错误回退：渲染失败 → 整个发布流程中断

**方案**：
- **Option A**：用 `playwright.sync_api` 替代外部工具渲染。`crawler.py` 已经依赖 Playwright，复用即可。
- **Option B**：卡片 HTML 用 Jinja2 模板替代字符串拼接。
- **Option C**：如果渲染失败，回退到纯文本 IMA 笔记 + 飞书消息（不传图片）。

```python
# Option A: Playwright 渲染
from playwright.sync_api import sync_playwright

def render_html_to_png(html_path, output_path):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 1800})
        page.goto(f"file://{html_path}")
        page.screenshot(path=output_path, full_page=True)
        browser.close()
```

```python
# Option B: Jinja2 模板
from jinja2 import Template

INFO_CARD_HTML = Template("""
<!DOCTYPE html>
<html>
<head><style>
  body { width: 1200px; ... }
</style></head>
<body>
  {% for article in articles %}
  <div class="card">
    <h2>{{ article.title }}</h2>
    <span class="stock">{{ article.stock }}</span>
    ...
  </div>
  {% endfor %}
</body>
</html>
""")
```

**收益**：
- 渲染不依赖外部工具
- 卡片模板更易维护
- 渲染失败时流程不中断

---

### 2.3 从 `requirements.txt` 升级到 `pyproject.toml`

**现状**：`requirements.txt` 是纯平铺列表，无版本锁定、无可选依赖分组、无工具配置（ruff、pytest）。

**方案**：

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "xueqiu-crawler"
version = "2.0.0"
description = "雪球专栏文章爬虫 - AI 分析 + 日报生成 + IMA 推送"
requires-python = ">=3.10"
dependencies = [
    "requests>=2.28.0",
    "pyyaml>=6.0",
    "filelock>=3.0.0",
    "anthropic>=0.30.0",
    "openai>=1.0.0",
    "python-dotenv>=1.0.0",
]

[project.optional-dependencies]
dev = ["pytest>=7.0", "ruff>=0.1.0"]
crawler = ["playwright>=1.40.0"]
all = ["xueqiu-crawler[dev,crawler]"]

[tool.ruff]
target-version = "py311"
line-length = 120
select = ["E", "F", "I", "W"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

**收益**：
- `pip install -e ".[dev]"` 一键安装所有开发依赖
- `ruff` 配置集中管理
- 可选依赖分组（`crawler` 组可选安装 Playwright）
- 项目元数据集中声明

---

## 三、工程化增强（3-5 天，持续维护）

### 3.1 结构化日志

**现状**：`crawler_xcrawl.py:58-73` 用标准 `logging` 模块 + 文件 Handler。日志是纯文本，需要 `grep` 才能检索特定用户/事件。

**方案**：改用 `structlog`，输出 JSON 格式日志。

```python
import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
)

log = structlog.get_logger()
log.info("crawl_user_start", user_id=user_id, max_articles=max_articles)
log.error("crawl_user_failed", user_id=user_id, error=str(e), duration_sec=elapsed)
```

**输出示例**：
```json
{"event": "crawl_user_start", "user_id": "5739488179", "max_articles": 20, "timestamp": "2026-05-23T02:00:00Z"}
{"event": "crawl_user_failed", "user_id": "6308001210", "error": "timeout", "duration_sec": 35.2, "timestamp": "2026-05-23T02:05:35Z"}
```

**收益**：
- 可直接导入 `jq`、`loki`、`elasticsearch` 等日志系统分析
- 查询单个用户某次失败：`jq 'select(.event == "crawl_user_failed")'`
- 统计每日爬取耗时：`jq 'select(.event == "crawl_all_complete") | .duration_sec'`

---

### 3.2 运行监控与告警

**现状**：`run_daily_xcrawl.sh` 将日志写入 `logs/cron_xcrawl.log`，但无人查看。爬虫失败、API Key 过期、XCrawl 额度耗尽等不会通知任何人。

**方案**：在流水线末尾加上健康检查。

```bash
# run_daily_xcrawl.sh 末尾
# 6. 健康检查：确认日报已生成，如有问题发飞书告警
TODAY_REPORT="data/daily_reports/$(date +%Y-%m-%d).md"
if [ ! -f "$TODAY_REPORT" ]; then
    echo "[$(date)] ❌ 日报生成失败！" >> "$LOG_FILE"
    # 飞书告警
    curl -s -X POST "$FEISHU_ALERT_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"❌ 雪球爬虫日报生成失败: $TODAY_REPORT 不存在\"}}"
    exit 1
fi

# 检查日志中的错误
ERROR_COUNT=$(grep -c "ERROR" "$LOG_FILE" | tail -1)
if [ "$ERROR_COUNT" -gt 5 ]; then
    curl -s -X POST "$FEISHU_ALERT_WEBHOOK" \
        -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"⚠️ 雪球爬虫日志有 $ERROR_COUNT 条错误，请查看 $LOG_FILE\"}}"
fi
```

**Python 端监控点**（可独立运行的健康检查脚本）：

```python
# scripts/health_check.py
def check_health():
    checks = []
    # 1. 检查 XCrawl 配置
    checks.append(("XCrawl API Key", XCrawlClient.is_configured()))
    # 2. 检查 index.json 完整性
    idx = load_index()
    checks.append(("index.json 文章数", len(idx.get('articles',[])) > 1000))
    # 3. 检查今日日报
    today_report = Path(f"data/daily_reports/{date.today()}.md")
    checks.append(("今日日报", today_report.exists()))
    # 4. 检查 cookies 是否过期
    checks.append(("Cookies 有效", not cookie_manager.is_expired()))
    return checks
```

**收益**：
- 失败后 5 分钟内收到飞书告警
- 可识别隐性问题（XCrawl 额度不足、API Key 过期）

---

### 3.3 使用 XCrawl 批量 API 替代逐个调用

**现状**：`crawl_all_users` 逐个调用 XCrawl API，每个 API call 消耗 credits。XCrawl 文档中有说明单次 scrape 支持多 URL。

**方案**：将同一批用户的文章列表抓取合并为一次 API 调用。

```python
# 当前：每个用户一次 API call（11 次）
for account in self.accounts:
    articles = self.crawl_article_list(account['id'], max_articles)

# 优化后：一次 API call 处理所有用户
def crawl_all_articles_list(self, max_articles=20):
    """批量爬取所有用户的文章列表，一次 XCrawl API 调用"""
    urls = [f"https://xueqiu.com/u/{a['id']}" for a in self.accounts]
    json_prompt = "提取所有页面的文章链接..."
    response = self.xcrawl.scrape(
        url=urls[0],  # 如果 XCrawl 不支持多 URL，退化为串行
        ...
    )
```

**注意**：需要确认 XCrawl API 是否支持多 URL 批量提交。如果不支持，保持当前串行或并发方案。

**收益**：如果支持批量，API 调用次数从 11 次降到 1 次，节省约 10× 的 credits。

---

### 3.4 引入 PR 模板和代码审查清单

**现状**：项目无 PR template，无代码审查 checklist。之前的代码审查是临时做的，日后每轮迭代应该标准化。

**方案**：创建 `.github/PULL_REQUEST_TEMPLATE.md`：

```markdown
## 变更内容

<!-- 简述本次 PR 做什么 -->

## 影响范围

- [ ] 爬虫层（scripts/crawler_*.py）
- [ ] 分析层（scripts/analyzer.py）
- [ ] 发布层（scripts/publish_*.py）
- [ ] 配置（config/）
- [ ] 依赖（requirements.txt）

## 质量门禁

- [ ] 通过测试：`python tests/test_core.py`
- [ ] 无新增未定义变量（`ruff check scripts/`）
- [ ] 已更新 PROJECT_RECORD.md 变更日志
- [ ] 已清理旧代码（不遗留重复功能）
```

---

## 四、远期可选（按需启动）

### 4.1 缓存 XCrawl 响应避免重复爬取

**现状**：即使增量更新避免了保存重复文章，`crawl_article_list` 每次仍然调用 XCrawl API。API credits 是按调用次数计费的。

**方案**：对 XCrawl 响应做短期缓存（TTL=6 小时）：

```python
import hashlib
import pickle
from pathlib import Path

class XCrawlCache:
    def __init__(self, cache_dir: str = 'data/.xcrawl_cache'):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, url: str) -> Optional[dict]:
        key = hashlib.md5(url.encode()).hexdigest()
        cache_file = self.cache_dir / f"{key}.pkl"
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < 6 * 3600:  # 6 小时内有效
                return pickle.loads(cache_file.read_bytes())
        return None

    def set(self, url: str, data: dict):
        key = hashlib.md5(url.encode()).hexdigest()
        cache_file = self.cache_dir / f"{key}.pkl"
        cache_file.write_bytes(pickle.dumps(data))
```

**收益**：对已缓存的用户不消耗 API credits（只检查 index.json 是否有新增即可）。

---

### 4.2 分析结果缓存

**现状**：`analyze_article` 每次都调 LLM，即使同一篇文章在不同日期被分析（例如用户手动重跑 generate_report）也重新分析。

**方案**：将分析结果缓存在文章同级目录下：

```python
# analyzer.py
CACHE_DIR = Path('data/.analysis_cache')

def get_cached_analysis(article_id: str, user_id: str) -> Optional[dict]:
    cache_file = CACHE_DIR / user_id / f"{article_id}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    return None

def save_analysis_cache(article_id: str, user_id: str, result: dict):
    cache_file = CACHE_DIR / user_id
    cache_file.mkdir(parents=True, exist_ok=True)
    (cache_file / f"{article_id}.json").write_text(json.dumps(result, ensure_ascii=False))
```

**收益**：
- 避免同一篇文章被多次分析
- 每日重跑时只分析当天新文章
- LLM API 费用降低

---

### 4.3 文章内容数据库化

**现状**：文章存为纯 Markdown 文件，路径 `data/{user_id}/{article_id}.md`。数量多了以后（目前 2447 篇）：
- 文件系统 inode 压力
- 全文搜索难（没有 `grep` 之外的选项）
- 迁移/备份需要遍历大量小文件

**方案**：把文章内容也迁到 SQLite（和 index 放在同一个库），正文用 TEXT 字段存储。

```sql
ALTER TABLE articles ADD COLUMN content TEXT;
```

或独立表：
```sql
CREATE TABLE article_contents (
    article_id TEXT PRIMARY KEY REFERENCES articles(id),
    content TEXT NOT NULL,
    word_count INTEGER
);
```

**收益**：
- 全文搜索：`SELECT * FROM articles WHERE content LIKE '%护城河%'`
- 单文件备份，无需 `tar` 遍历 2000+ 小文件
- 文件系统 inode 占用降到 1 个

**权衡**：SQLite 对大 TEXT 的查询性能不如文件系统直接读，但 2447 篇文章的规模下差距可以忽略。

---

## 五、阶段执行计划建议

```
第1轮（2天）：    1.1 并发爬取 + 1.3 飞书Webhook
第2轮（2天）：    1.2 SQLite 迁移（含数据迁移脚本）
第3轮（1天）：    2.1 CI + 2.3 pyproject.toml
第4轮（1天）：    2.2 InfoCard 渲染改进
第5轮（2天）：    3.1 structlog + 3.2 监控告警
第6轮（按需）：   3.3 XCrawl 批量 + 4.x 缓存/数据库化
```

每轮独立发 PR，互不阻塞。

---

## 六、整体收益预估

| 优化项 | 当前状态 | 优化后 | 关键指标 |
|--------|---------|--------|---------|
| 爬取并发 | ~110s 串行 | ~25s 并发 | 耗时 -77% |
| SQLite 迁移 | 1.5MB JSON 全量 | 按需查询 | 内存 -90%, 查询灵活 |
| 飞书 Webhook | 文件队列 | 实时推送 | 送达时间 -∞ |
| CI | 无 | push 自动测试 | 回归发现时间 -∞ |
| 结构化日志 | 纯文本 grep | JSON 可分析 | 问题定位 -70% |
| 监控告警 | 无人值守 | 飞书告警 | 响应时间 -95% |
| 响应缓存 | 每天重复调 API | 6h TTL 缓存 | API 成本 -50% |
| 分析缓存 | 每天重复调 LLM | 文章级缓存 | API 成本 -80% |
| 数据库化文章 | 2447 小文件 | 1 个 SQLite | inode -99.9% |
