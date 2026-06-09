# 雪球专栏文章爬虫 — 项目记录

> 本文件作为项目的技术参考手册，涵盖架构、模块、数据结构、配置和操作规范。  
> 动态追踪（特性/Bug/TODO/版本发布/技术债务）请参阅 `PROJECT_LOG.md`。  
> 最后更新：2026-05-24

---

## 一、项目身份

| 字段 | 值 |
|------|-----|
| 项目名 | xueqiu-crawler |
| 仓库 | `https://github.com/winterswang/xueqiu-crawler` |
| 语言 | Python 3 |
| 定位 | 自动爬取雪球指定用户的专栏文章 → AI 质量分析 → 日报生成 → IMA/飞书推送 |
| 部署 | Linux 服务器，crontab 每日凌晨 2:00 无人值守运行 |
| 代码审查综合评分 | **6.5/10**（架构清晰，但工程化问题积累较多） |

---

## 二、系统架构

### 2.1 架构总览

```
┌───────────────────────────────────────────────────────────────────┐
│                      爬虫层 (Crawler Layer)                        │
│                                                                   │
│  crawler.py (~880行)     ← 主用，Playwright 本地浏览器爬取        │
│       └─ XueqiuCrawler: 文章列表爬取、详情爬取、增量更新          │
│       └─ crawl_all_users: 串行遍历11个账号 + 爬取统计保存         │
│                                                                   │
│  login.py (295行)        ← 新增，雪球自动登录脚本                 │
│       └─ 通过 OpenClaw browser 自动完成登录、提取 cookies          │
│                                                                   │
│  cookies.py (262行)      ← 登录态管理：检查/导入/刷新/过期        │
└───────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌───────────────────────────────────────────────────────────────────┐
│                         数据层 (Data Layer)                        │
│                                                                   │
│  data/{user_id}/{article_id}.md   ← 文章原文 (纯 Markdown)       │
│  data/index.json                   ← 索引 (2447篇, 1.5MB)        │
│  data/daily_reports/{date}.md     ← 每日日报输出                  │
│  data/history/                     ← 历史爬取记录                 │
│  config/accounts.yaml             ← 11个雪球账号配置              │
│  config/config.yaml               ← 爬虫与调度配置                │
│  config/xueqiu_cookies.json       ← cookies (30天过期)            │
└───────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌───────────────────────────────────────────────────────────────────┐
│                      分析层 (Analysis Layer)                       │
│                                                                   │
│  analyzer.py (781行) ← 主用 AI 分析                               │
│       └─ classify_stock_market()    股票市场分类                  │
│       └─ check_article_quality()    质量检测 (2项)                │
│       └─ calculate_priority_score() 7维评分 (120分制)            │
│       └─ classify_priority()        三级优先级                    │
│       └─ ArticleAnalyzer 类         调用 GLM-5 / MiniMax          │
│       └─ generate_daily_report()    组装日报 Markdown             │
│                                                                   │
│  quality_check.py (128行) ⚠️ 与 analyzer 的 check_article_quality │
│                             重复，检测项不同 (4项 vs 2项)          │
│  value_analyzer.py (317行) 独立框架，不参与主流程                 │
└───────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌───────────────────────────────────────────────────────────────────┐
│                   报告与发布层 (Report Layer)                      │
│                                                                   │
│  generate_report.py (126行)         调用 AI 分析 + 写日报         │
│  publish_daily_report_v3.py (516行) 最新版: InfoCard → IMA → 飞书 │
└───────────────────────────────────────────────────────────────────┘
```

### 2.2 数据流

```
雪球网页 ──[Playwright]──→ 文章列表[{id, title, url}]
                                │
                  ┌── 过滤已存在的 ID (index.json) ──┐
                  │  爬取详情 (可选 --detail)         │
                  │  保存 data/{uid}/{aid}.md          │
                  └── 更新 index.json ───────────────┘
                                │
                                ▼  analyzer.py
                      GLM-5 / MiniMax M2.7 深度分析
                      ├─ 质量检测(>200字)
                      ├─ 7维评分 (120分制)
                      ├─ 优先级分类 (必读/关注/参考)
                      └─ 深度评价 (商业模式/管理层/风险/竞争/后续)
                                │
                                ▼  generate_report.py
                      data/daily_reports/{date}.md
                      ├─ 概览统计
                      ├─ 股票按市场分组
                      ├─ 操作参考/信号
                      └─ 文章详情 (按优先级排列)
                                │
                                ▼  publish_daily_report_v3.py
                      InfoCard(1200×1800 html)
                      └─ IMA 笔记推送 (腾讯知识库)
                      └─ 飞书消息推送 (含链接)
```

### 2.3 每日执行流

```
run_daily.sh (凌晨 2:00 cron 触发)
  │
  ├─ [1/4] login.py / cookies.py --check
  │         检查 cookies 状态，过期则自动登录 (Playwright)
  │
  ├─ [2/4] crawler.py --all -m 20
  │         ├─ 遍历 11 个账号 (串行)
  │         ├─ 每个账号抓取最多 20 篇文章
  │         ├─ 过滤已有 ID (增量)
  │         ├─ 保存新文章 + 更新索引
  │         └─ 保存 crawl_stats 到 data/.last_crawl_stats.json
  │
  ├─ [3/4] generate_report.py --limit 20
  │         ├─ get_today_articles() 读取 index.json
  │         ├─ ArticleAnalyzer.analyze_article() 逐篇 AI 分析
  │         └─ generate_daily_report() 写日报文件
  │
  └─ [4/4] publish_daily_report_v3.py
            ├─ 从日报提取必读文章信息
            ├─ 生成 InfoCard HTML (1200×1800)
            ├─ 保存为截图/HTML
            ├─ 创建 IMA 笔记
            └─ 发送飞书消息 (含笔记链接 + 摘要)
```

---

## 三、模块清单

### 3.1 爬虫层

| 文件 | LOC | 状态 | 类/函数 | 说明 |
|------|-----|------|---------|------|
| `crawler.py` | ~820 → ~880 | ✅ 唯一 | Playwright 本地浏览器爬取，含 crawl_all_users + crawl_stats |
| `cookies.py` | 262 | ✅ 在用 | `CookieManager` | 登录态：检查/导入/刷新/过期 (30天) |
| `login.py` | 295 | ✅ 新增 | Playwright 自动登录 | 浏览器自动化完成雪球登录，提取 cookies |

### 3.2 分析层

| 文件 | LOC | 状态 | 关键函数 | 说明 |
|------|-----|------|---------|------|
| `analyzer.py` | 781 | ✅ 主用 | `classify_stock_market`, `check_article_quality`, `calculate_priority_score`(7维), `classify_priority`(三级), `generate_daily_report`, `ArticleAnalyzer` | GLM-5/MiniMax AI 分析，支持双 provider |
| `quality_check.py` | 128 | ⚠️ 重复 | `check_article_quality`(4项), `QualityLogger` | 与 analyzer 同名函数重叠，检测项不同 |
| `value_analyzer.py` | 317 | ⚠️ 隐式 | `ValueInvestmentAnalyzer`, `generate_investment_report` | 独立框架，使用 qwen-plus，不参与主流程 |

### 3.3 报告与发布层

| 文件 | LOC | 状态 | 说明 |
|------|-----|------|------|
| `generate_report.py` | 126 | ✅ 主用 | 组装日报 Markdown，只能 CLI 运行，不可 import |
| `publish_daily_report_v3.py` | 516 | ✅ 主用 | InfoCard(1200×1800) + IMA OpenAPI + 飞书推送 |

### 3.4 工具与集成

| 文件 | LOC | 状态 | 说明 |
|------|-----|------|------|
| `run_daily.sh` | — | ✅ 在用 | 每日 cron 执行编排 |

### 3.5 数据与配置

| 路径 | 类型 | 说明 |
|------|------|------|
| `config/accounts.yaml` | YAML | 11 个雪球账号 (id/name/url/enabled/description) |
| `config/config.yaml` | YAML | 爬虫延迟(2-5s)、超时(30s)、max_articles(20)、cron、日志级别 |
| `config/xueqiu_cookies.json` | JSON | 登录 cookies, created_at, expires_at (30天) |
| `data/index.json` | JSON | 索引: ~1.5MB, 2447条记录, {article_id → {title, user_id, crawl_time, file_path}} |
| `data/{user_id}/{article_id}.md` | Markdown | 文章正文 (含元数据头部和爬取脚注) |
| `data/daily_reports/{date}.md` | Markdown | 日报输出 (概览/股票分组/优先级/详情/总结) |
| `.env.example` | env | 凭证模板 (BAILIAN_API_KEY, GITHUB_TOKEN) |

### 3.6 文档

| 文件 | 说明 |
|------|------|
| `README.md` | 主 README（含自评 7/10，代码质量问题清单） |
| `PROJECT_LOG.md` | 开发跟踪文档（特性/Bug/TODO/版本发布/技术债务） |
| `PROJECT_RECORD.md` | 本文件，技术参考手册 |
| `docs/report_design.md` | 日报格式设计 + GLM-5 分析 Prompt 模板 |
| `docs/infocard_spec.md` | 信息卡片设计规范 V2.0 |

---

## 四、数据结构

### 4.1 index.json 结构

```json
{
  "articles": {
    "{user_id}_{article_id}": {
      "article_id": "123456789",
      "user_id": "5739488179",
      "title": "文章标题",
      "crawl_time": "2026-03-11T02:00:00",
      "file_path": "data/5739488179/123456789.md"
    }
  },
  "last_update": "2026-03-11T02:15:00",
  "history": {
    "2026-03-11": {
      "new_articles": 15,
      "total": 2447
    }
  }
}
```

### 4.2 文章 Markdown 格式

```
# 标题
> 作者ID: {user_id}
> 发布时间: {publish_time}
> 点赞: {likes} | 评论: {comments}
> 原文链接: {url}
---
{summary}
---
{content (仅 --detail 时)}
---
*爬取时间: {datetime}*
```

### 4.3 cookies 结构

```json
{
  "cookies": { "key": "value", ... },
  "created_at": "2026-03-10T...",
  "expires_at": "2026-04-09T..."
}
```

---

## 五、配置参考

| 配置项 | 位置 | 当前值 | 来源 | 说明 |
|--------|------|--------|------|------|
| MiniMax API Key | `MINIMAX_API_KEY` env | 环境相关 | `analyzer.py:244` | 备用 AI provider |
| IMA Client ID | `~/.config/ima/client_id` | 环境相关 | `publish_daily_report_v3.py:42` | IMA 知识库 |
| IMA API Key | `~/.config/ima/api_key` | 环境相关 | `publish_daily_report_v3.py:43` | IMA 知识库 |
| cookies 有效期 | `cookies.py:25` | 30 天 | 硬编码 | 过期需重新登录 |
| AI 分析模型 | 硬编码 | GLM-5 / MiniMax-M2.7 | `analyzer.py:285,301` | 不可配置 |

---

## 六、代码审查：优点

### 架构设计
1. **三层分离清晰** — 爬取→分析→发布，各层职责单一，管道式数据流直观
2. **增量更新设计轻量** — index.json 独立于正文文件，查询快速，每天只爬增量

### 工程实践
3. **结构化 Prompt** — 分析 prompt 分"信息提炼"和"深度评价"两部分，维度覆盖商业模式/管理层/竞争格局/风险/后续关注
4. **LLM 输出防御** — `_parse_response` 4 层 JSON 解析 fallback，对 LLM 的幻觉和格式漂移做了充分的容错
5. **raw_decode 处理截断** — `analyzer.py:394-400` 用 `json.JSONDecoder.raw_decode` 从截断 JSON 中提取第一个完整对象
6. **市场分类规则合理** — 港股 `.HK`、美股纯大写≤5字符、A股6位数字，覆盖主流格式

### 部署考虑
7. **.gitignore 覆盖敏感文件** — `data/*.json`、`logs/`、`.env` 等不提交
8. **定时调度独立** — crontab 与代码解耦，cron 表达式在 config 中可配

---

## 七、技术栈

| 层 | 技术 | 版本/说明 | 在代码中的位置 |
|----|------|----------|---------------|
| 爬虫备用 | Playwright | 本地 Chromium ~500MB | `crawler.py:29` |
| AI 分析 | GLM-5 (百炼) / MiniMax M2.7 | 双 provider 切换 | `analyzer.py:237-261` |
| 辅助分析 | qwen-plus (阿里云) | value_analyzer 专用 | `value_analyzer.py:59` |
| 发布渠道 | IMA 知识库 OpenAPI | 腾讯笔记平台 | `publish_daily_report_v3.py:49` |
| 发布渠道 | 飞书消息 | 写入 /tmp/pending_feishu 文件 | `publish_daily_report_v3.py:419` |
| 依赖管理 | `requirements.txt` | 仅列出3个包（不全） | 缺少 anthropic, openai |

---

## 八、操作指南

### 本地开发

```bash
# 安装依赖 (建议补充后执行)
pip install -r requirements.txt
pip install anthropic openai python-dotenv  # 缺失的依赖

# 配置 AI 凭证（二选一）
export BAILIAN_API_KEY="sk-xxx"           # 百炼/阿里云
export MINIMAX_API_KEY="xxx"              # MiniMax

# 配置 IMA（推送用）
mkdir -p ~/.config/ima
echo "client_id" > ~/.config/ima/client_id
echo "api_key" > ~/.config/ima/api_key
```

### 常用命令

```bash
# 爬取所有用户
python scripts/crawler.py --all -m 20

# 生成当日日报
python scripts/generate_report.py --limit 20

# 发布
python scripts/publish_daily_report_v3.py

# 检查 cookies
python scripts/cookies.py --check
```

### 部署

服务器路径（旧，建议 PROJECT_ROOT 动态化）：
```
/root/.openclaw/workspace/xueqiu-crawler
```

crontab：
```cron
0 2 * * * cd /root/.openclaw/workspace/xueqiu-crawler && bash scripts/run_daily.sh >> logs/cron_daily.log 2>&1
```

---

## 九、开发规范

1. **改前先读** `PROJECT_RECORD.md` + `README.md`，了解全局架构
2. **先清理再添加** — 优先完成 P0/P1 债务，再开发新功能
3. **路径统一** — `PROJECT_ROOT / 子路径`，禁止绝对路径
4. **凭证统一** — `.env` + `python-dotenv`，不写死在脚本或 shell 中
5. **测试先行** — 核心函数必须写单元测试（`tests/` 目录）
6. **MR 前** — 更新本记录中的模块清单
7. **命名一致** — `snake_case`，函数/变量/文件统一风格
8. **接口契约** — 两个爬虫版本输出数据字段必须一致

---

*本文件为技术参考手册。动态追踪（特性/Bug/TODO/版本发布/技术债务）请参阅 `PROJECT_LOG.md`。*
