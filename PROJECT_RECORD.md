# 雪球专栏文章爬虫 — 项目记录

> 本文件作为项目的结构化知识库，用于后续迭代、调试和升级。  
> 最后更新：2026-05-24 | 反检测增强 + headless 新模式  

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
│                                         (只能 CLI, 不可 import)   │
│  publish_daily_report_v3.py (516行) 最新版: InfoCard → IMA → 飞书 │
│  publish_daily_report_v2.py (558行) ⛔ 应删除 (旧版)              │
│  publish_daily_report.py (156行)    ⛔ 应删除 (最旧版)             │
└───────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌───────────────────────────────────────────────────────────────────┐
│                      集成层 (Integration Layer)                    │
│                                                                   │
│  run_daily.sh                         ⛔ 应删除 (旧版)            │
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
| `publish_daily_report_v2.py` | 558 | ⛔ 应删除 | 旧版，功能被 v3 覆盖，依赖外部 INFOCARD_SKILL |
| `publish_daily_report.py` | 156 | ⛔ 应删除 | 最旧版，无 InfoCard，仅 IMA+飞书 |

### 3.4 工具与集成

| 文件 | LOC | 状态 | 说明 |
|------|-----|------|------|
| `run_daily.sh` | — | ⛔ 应删除 | Playwright 旧版流程编排 |

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
| `PROJECT_RECORD.md` | 本文件，项目完整知识库 |
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

## 六、代码审查：特性清单

| 特性 | 实现位置 | 说明 |
|------|---------|------|
| ✅ 多账号配置 | `config/accounts.yaml` | 11 个账号，支持 enabled/disabled |
| ✅ 多 AI Provider | `analyzer.py:237-261` | MiniMax / 阿里云百炼双通道 |
| ✅ 7 维评分系统 | `analyzer.py:88-197` | 120分制 (内容30+关键词25+相关性20+安全边际15+归类10+观点10+标题10) |
| ✅ 三级优先级 | `analyzer.py:200-229` | 必读≥60 / 值得关注30-59 / 参考<30 |
| ✅ 股票市场分类 | `analyzer.py:35-53` | A股/港股/美股/日股/其他自动识别 |
| ✅ 市场分组报告 | `analyzer.py:545-556` | 日报按市场展示股票 |
| ✅ LLM 响应容错 | `analyzer.py:384-426` | 4 种 JSON 解析策略 (fenced→raw_decode→strip→fallback) |
| ✅ InfoCard 生成 | `publish_daily_report_v3.py` | 1200×1800 HTML 信息卡片 |
| ✅ IMA 笔记推送 | `publish_daily_report_v3.py` | 腾讯 IMA OpenAPI |
| ✅ 飞书推送 | `publish_daily_report_v3.py:419-450` | 消息写入 /tmp/pending 文件 |
| ✅ cron 定时调度 | `config.yaml` | 每日凌晨 2:00 无人值守 |
| ✅ 登录态管理 | `cookies.py` | 30 天过期检查，手动刷新 |

---

## 七、代码审查：优点

### 架构设计
1. **三层分离清晰** — 爬取→分析→发布，各层职责单一，管道式数据流直观
3. **增量更新设计轻量** — index.json 独立于正文文件，查询快速，每天只爬增量

### 工程实践
5. **结构化 Prompt** — 分析 prompt 分"信息提炼"和"深度评价"两部分，维度覆盖商业模式/管理层/竞争格局/风险/后续关注
6. **LLM 输出防御** — `_parse_response` 4 层 JSON 解析 fallback，对 LLM 的幻觉和格式漂移做了充分的容错
7. **raw_decode 处理截断** — `analyzer.py:394-400` 用 `json.JSONDecoder.raw_decode` 从截断 JSON 中提取第一个完整对象
9. **市场分类规则合理** — 港股 `.HK`、美股纯大写≤5字符、A股6位数字，覆盖主流格式

### 部署考虑
11. **.gitignore 覆盖敏感文件** — `data/*.json`、`logs/`、`.env` 等不提交
12. **定时调度独立** — crontab 与代码解耦，cron 表达式在 config 中可配

---

## 八、代码审查：问题清单

### P0 — 必须修复

| # | 问题 | 位置 | 严重程度 | 建议方案 |
|---|------|------|---------|---------|
| 1 | 三个发布版本并存 | `publish_daily_report.py/v2/v3` | 维护负担 | 确认 v3 稳定后删除旧版 |
| 2 | `check_article_quality` 重复定义 | `analyzer.py:65` vs `quality_check.py:14` | 逻辑不一致 | 合并到 `analyzer.py`，`quality_check.py` 做 wrapper 或删除 |
| 4 | `analyzer.py:413` 引用未定义变量 `e` | `_parse_response` fallback | 运行时错误 | 统一包在 `try-except` 中 |

### P1 — 重要

| # | 问题 | 位置 | 说明 | 建议方案 |
|---|------|------|------|---------|
| 5 | 硬编码服务器路径 | `publish_daily_report_v3.py:38` (+ v2:20, v1:20) | 迁移必崩 | 全部改为 `PROJECT_ROOT / 子路径` |
| 7 | MiniMax ThinkingBlock 当文本解析 | `analyzer.py:293-297` | 分析结果被思考过程污染 | 跳过 ThinkingBlock，只取 TextBlock |
| 8 | AI 模型名硬编码 | `analyzer.py:285` MiniMax-M2.7, `:301` glm-5 | 换模型需改代码 | 抽到 `config.yaml` |
| 9 | 内容截断无标记 | `analyzer.py:336 content[:4000]` | 分析可能偏倚 | 截断时加 `[内容截断...]` 标记 |
| 10 | 零测试覆盖 | 全项目 | 无单元测试/集成测试 | 优先给核心函数写测试 |
| 11 | `requirements.txt` 不全 | `anthropic`、`openai` 缺失 | 新环境安装失败 | 补充到 requirements |

### P2 — 可改进

| # | 问题 | 位置 | 说明 | 建议方案 |
|---|------|------|------|---------|
| 12 | `generate_report.py` 不可 import | `main()` 是唯一入口 | 无法在其他脚本复用 | 抽核心逻辑到可 import 函数 |
| 13 | `generate_report.py` 无错误处理 | `get_today_articles` 无 try-except | index.json 损坏时直接崩溃 | 加 graceful 容错 |
| 15 | 美股分类规则宽松 | `analyzer.py:44` `isupper() and ≤5` | 中文拼音缩写可能误判 | 加正则白名单 |
| 16 | 两个爬虫输出契约不统一 | `filepath` vs `file_path` | 混用时数据异常 | 定义统一字段接口 |
| 18 | `value_analyzer.py` 无文档记录 | 隐式工具 | 维护人不知其用途 | 在 README 中说明 |
| 20 | `analyzer.py` provider 初始化散落 | `#237-261` 多个 if-else | 新增 provider 时易遗漏 | 工厂模式或配置驱动 |

---

## 九、技术栈

| 层 | 技术 | 版本/说明 | 在代码中的位置 |
|----|------|----------|---------------|
| 爬虫备用 | Playwright | 本地 Chromium ~500MB | `crawler.py:29` |
| AI 分析 | GLM-5 (百炼) / MiniMax M2.7 | 双 provider 切换 | `analyzer.py:237-261` |
| 辅助分析 | qwen-plus (阿里云) | value_analyzer 专用 | `value_analyzer.py:59` |
| 发布渠道 | IMA 知识库 OpenAPI | 腾讯笔记平台 | `publish_daily_report_v3.py:49` |
| 发布渠道 | 飞书消息 | 写入 /tmp/pending_feishu 文件 | `publish_daily_report_v3.py:419` |
| 依赖管理 | `requirements.txt` | 仅列出3个包（不全） | 缺少 anthropic, openai |

---

## 十、操作指南

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
# 爬取单个用户 20 篇文章

# 爬取所有用户

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
```

---

## 十一、开发规范

1. **改前先读** `PROJECT_RECORD.md` + `README.md`，了解全局架构
2. **先清理再添加** — 优先完成 P0/P1 债务，再开发新功能
3. **路径统一** — `PROJECT_ROOT / 子路径`，禁止绝对路径
4. **凭证统一** — `.env` + `python-dotenv`，不写死在脚本或 shell 中
5. **测试先行** — 核心函数必须写单元测试（`tests/` 目录）
6. **MR 前** — 更新本记录中的模块清单、问题状态、变更日志
7. **命名一致** — `snake_case`，函数/变量/文件统一风格
8. **接口契约** — 两个爬虫版本输出数据字段必须一致

---

## 十二、优化与修复计划

### 第一阶段：快速清理 (P0 紧急修复)

```
目标：消除数据损坏风险 + 版本碎片整顿
预计工作量：1-2 天
```

| 步骤 | 内容 | 涉及文件 | 预期效果 |
|------|------|---------|---------|
| 1.1 | 修复 `analyzer.py:413` 未定义变量 `e` | `analyzer.py` | 消除潜在运行时崩溃 |
| 1.2 | 删 `publish_daily_report.py` + `publish_daily_report_v2.py` | 清理 workspace | 版本减少 2/3 |
| 1.3 | 删 `run_daily.sh` | 清理 workspace | 消除旧流程误导 |
| 1.5 | 合并 `check_article_quality`（2项→4项） | `analyzer.py` + `quality_check.py` | 逻辑一致 |

### 第二阶段：路径与凭证统一 (P1 加固)

```
目标：消除硬编码依赖 + 安全加固
预计工作量：1-2 天
```

| 步骤 | 内容 | 涉及文件 | 预期效果 |
|------|------|---------|---------|
| 2.1 | 替换 `publish_daily_report_v3.py` 的绝对路径为 `PROJECT_ROOT` | `publish_daily_report_v3.py` | 任意路径可运行 |
| 2.3 | 引入 `.env` + `python-dotenv`，统一所有凭证 | 所有脚本 | 单点管理凭证 |
| 2.4 | 补充 `requirements.txt` 缺失依赖 (`anthropic`, `openai`, `python-dotenv`) | `requirements.txt` | 全新安装一次成功 |
| 2.5 | AI 模型名抽到 `config.yaml` | `analyzer.py` | 换模型改配置即可 |

### 第三阶段：AI 分析与容错增强 (P1 质量)

```
目标：提高 AI 分析准确性 + 错误恢复能力
预计工作量：1-2 天
```

| 步骤 | 内容 | 涉及文件 | 预期效果 |
|------|------|---------|---------|
| 3.1 | MiniMax ThinkingBlock 过滤，只取 TextBlock | `analyzer.py` | 分析结果不被思考过程污染 |
| 3.2 | `_build_prompt` 截断处加 `[内容截断...]` 标记 | `analyzer.py` | 模型知道数据不完整 |
| 3.3 | `generate_report.py` 加 try-except 容错 | `generate_report.py` | 局部失败不影响全流程 |
| 3.4 | 美股分类规则用白名单正则 + 市场代码后缀 | `analyzer.py` | 减少误判 |
| 3.5 | `generate_report.py` 抽核心逻辑为可 import 函数 | `generate_report.py` | 可被其他脚本调用 |

### 第四阶段：工程化与测试 (P2 规范)

```
目标：建立质量门禁，稳定可维护
预计工作量：2-3 天
```

| 步骤 | 内容 | 涉及文件 | 预期效果 |
|------|------|---------|---------|
| 4.1 | 写 `classify_stock_market`、`check_article_quality`、`calculate_priority_score` 单元测试 | `tests/` | 核心函数有保障 |
| 4.5 | `analyzer.py` provider 初始化用工厂模式 | `analyzer.py` | 新增 provider 更简单 |

### 第五阶段：远期优化 (可选/按需)

| 步骤 | 内容 | 预期效果 |
|------|------|---------|
| 5.1 | `index.json` 迁移 SQLite | 事务安全、查询灵活、支持并发 |
| 5.3 | 日报推送到飞书群机器人替代文件落盘 | 实时通知 |
| 5.4 | 引入 `structlog` 替代标准 logging | 日志结构化，便于搜索 |
| 5.5 | GitHub Actions CI + pytest 自动运行 | 合并前自动验证 |

---

## 十三、问题修复状态总览

> PR #3 合并后，所有问题的当前状态与建议。

### 已修复（Section 八 — 代码问题 + Section 九 — 架构问题）

| # | 问题 | 状态 | 说明 |
|---|------|------|------|
| A5 | 作者字段传递断裂 | ✅ 已修复 | index.json 保存时补全 author + publish_time（PR #6） |
| 1 | 三个发布版本并存 | ✅ 已删除 | publish_daily_report.py / v2 已移除 |
| 2 | check_article_quality 重复 | ✅ 已合并 | 统一到 analyzer.py，quality_check.py 做 wrapper |
| 3 | index.json 无写入保护 | ✅ filelock | FileLock(timeout=10) 保护读写 |
| 4 | analyzer.py:413 未定义变量 e | ✅ 已修复 | 统一 try-except 包裹 |
| 5 | 硬编码服务器路径 | ✅ PROJECT_ROOT | 全部改为动态路径 |
| 6 | 凭证分散 + shell 明文 | ✅ .env | 移除明文 key，统一凭证管理 |
| 7 | ThinkingBlock 污染 | ✅ 已过滤 | 只取 TextBlock |
| 8 | 模型名硬编码 | ✅ config.yaml | 模型名抽到配置 |
| 9 | 内容截断无标记 | ✅ 已加 | 追加 [注：内容已截断] |
| 10 | 零测试覆盖 | ✅ 18 tests | tests/test_core.py |
| 11 | requirements.txt 不全 | ✅ 已补全 | 增加 4 个缺失包 |
| 12 | generate_report.py 不可 import | ✅ 可 import | 新增 generate_today_report() |
| 13 | generate_report.py 无错误处理 | ✅ try-except | 文件 IO 全部容错 |
| 15 | 美股分类规则宽松 | ✅ 白名单 | 常见美股代码白名单 |

### 未修复（建议跳过）

| # | 问题 | 理由 |
|---|------|------|
| 14 | crawl_all_users 串行 | 凌晨 2 点运行，110s → 25s 无实际收益 |
| 17 | 重试装饰器嵌套深 | 代码风格问题，不影响功能 |
| 18 | value_analyzer.py 无文档 | 隐式工具，不参与主流程 |
| 19 | README 与实际脱节 | 文档维护问题，非功能缺陷 |
| 20 | provider 初始化散落 | config.yaml 已部分缓解 |

### 未修复（建议关注）

| # | 问题 | 影响 |
|---|------|------|
| 16 | 两个爬虫输出契约不统一（filepath vs file_path） | 混用时数据格式异常 |

### 推荐优先处理（Section 九）

| 优 | # | 问题 | 影响 | 难度 |
|----|---|------|------|------|
| 🔴 | C1/C2 | ~~评分权重偏机械~~ | 已清理：移除已失效的 value_alignment/safety_margin（PR #4） | ✅ 已修复 |
| 🔴 | A5 | ~~作者字段传递断裂~~ | 已修复：index.json 保存时补全 author + publish_time（PR #6） | ✅ 已修复 |
| 🟡 | C3 | 4000 字符截断影响长文 | 长文分析不完整 | 低 |
| 🟡 | C7 | 报告按文章排列而非按标的 | 同标的信息分散，阅读效率低 | 中 |
| 🔵 | A2 | 分析结果无积累 | 无法做历史查询和趋势追踪 | 高 |

---

## 十四、变更日志

| 日期 | 版本 | 变更人 | 说明 |
|------|------|--------|------|
| 2026-05-23 | 1.0 | System | 初始项目记录建立 |
| 2026-05-23 | 2.0 | System | 代码审查整合：17个问题详情、优点清单、5阶段优化计划 |
| 2026-05-23 | 2.1 | System | 执行 Phase1-4 优化，17 项修复；tests/test_core.py (18 tests) |
| 2026-05-23 | 2.2 | System | 架构与质量深度审查（A1-A5/B1-B4/C1-C7） |
| 2026-05-23 | 2.3 | System | PR #3 合入 main（6d0bcc5），删除旧版脚本 3 个 |
| 2026-05-23 | 2.4 | System | 新增问题修复状态总览（Section 十三） |
| 2026-05-23 | 2.5 | System | PR #4 合入：移除已失效的 value_alignment/safety_margin 评分维度 |
| 2026-05-23 | 2.6 | System | 同步远程更新：MiniMax ThinkingBlock 修复；PR #4 合入后的文档更新 |
| 2026-05-23 | 2.7 | System | 修复 A5：index.json 保存时补全 author（PR #6）；A5 状态改为已修复 |
| 2026-05-23 | 2.8 | System | 更新后续方向梳理：4 项高效果/低投入、2 项中效果/中投入、6 项建议暂缓 |
| 2026-05-23 | 2.9 | System | PR #7：完成 C3/C5/C6/A1 四项优化；高效果低投入项全部清零 |
| 2026-05-23 | 2.10 | System | PR #6 + PR #7 双合
| 2026-05-23 | 2.11 | System | PR #10：Playwright 主用 + crawl_stats；PR #11：废弃 XCrawl |
| 2026-05-24 | 2.12 | System | 反检测增强 + Chromium new headless 模式 + _load_index 健壮性修复 |

---

*本文件在每次重要迭代后更新。*

