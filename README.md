# 雪球专栏文章爬虫

自动爬取指定雪球用户的专栏文章，AI 分析 + 日报生成 + 推送 IMA 知识库。

## 功能

- ✅ XCrawl API 云端爬取（替代本地 Playwright）
- ✅ 自动过滤专栏文章，排除评论和短状态
- ✅ AI 质量分析 + 多维度评分
- ✅ 增量更新，避免重复爬取
- ✅ 支持多账号配置
- ✅ 每日凌晨 2:00 自动运行
- ✅ Info Card 生成 + IMA 知识库推送

## 架构

```
accounts.yaml (账号配置)
        ↓
crawler_xcrawl.py (XCrawl API 爬文章列表)
        ↓
index.json (文章索引: article_id → filepath)
        ↓
data/{user_id}/{article_id}.md (Markdown 文章)
        ↓
analyzer.py (AI 质量分析 + 评分 + 优先级)
        ↓
generate_report.py (汇总日报 Markdown)
        ↓
publish_daily_report_v3.py (Info Card + IMA 推送 + 飞书推送)
```

## 目录结构

```
xueqiu-crawler/
├── config/
│   ├── accounts.yaml      # 雪球账号列表
│   └── config.yaml        # 爬虫配置（延迟、超时、调度）
├── scripts/
│   ├── crawler.py         # Playwright 版本（备用）
│   ├── crawler_xcrawl.py  # XCrawl 云端爬虫（主用）
│   ├── analyzer.py         # AI 质量分析 + 评分
│   ├── generate_report.py  # 日报 Markdown 生成
│   ├── quality_check.py    # 文章质量检测
│   ├── publish_daily_report_v3.py  # 发布脚本（卡片+IMA+飞书）
│   ├── value_analyzer.py   # 价值分析辅助
│   └── cookies.py          # Cookie 管理
├── data/
│   ├── {user_id}/          # 按用户分目录存储文章
│   ├── index.json          # 全量文章索引（1.5MB/2447篇）
│   ├── daily_reports/      # 每日日报 Markdown
│   └── history/            # 历史爬取记录
├── docs/                   # 设计文档
├── output/                 # Info Card 输出
├── logs/
└── README.md
```

## 安装

```bash
pip install -r requirements.txt
```

## 配置

### 账号 (config/accounts.yaml)

```yaml
accounts:
  - id: "7680894870"
    name: 价值投资新经济
    description: 价值投资新经济
    enabled: true
  - id: "5739488179"
    name: Elon翻开每一页
    enabled: true
```

### 爬虫 (config/config.yaml)

```yaml
crawler:
  delay_min: 2
  delay_max: 5
  timeout: 30
  max_articles: 20
  max_retries: 3
schedule:
  enabled: true
  cron: "0 2 * * *"  # 每日凌晨2点
```

### XCrawl

XCrawl API Key 需配置在 `~/.xcrawl/config.json`：

```json
{"XCRAWL_API_KEY": "xc-xxx"}
```

## 使用

```bash
# 爬取所有用户
python scripts/crawler_xcrawl.py

# 生成今日日报
python scripts/generate_report.py

# 发布（生成卡片 + 推送IMA + 飞书）
python scripts/publish_daily_report_v3.py
```

## 定时任务

```bash
0 2 * * * cd /root/.openclaw/workspace/xueqiu-crawler && python scripts/crawler_xcrawl.py >> logs/cron.log 2>&1
```

## 代码质量评估（2026-05-22）

| 维度 | 评分 | 说明 |
|------|------|------|
| **架构** | 8/10 | 模块化清晰，爬取→分析→发布分离 |
| **代码质量** | 6/10 | 关键函数缺文档，部分命名不一致 |
| **工程化** | 5/10 | 无测试，多版本并存，零测试覆盖 |
| **可维护性** | 6/10 | 硬编码路径分散，配置不集中 |
| **数据安全** | 7/10 | 凭证部分外置，index.json 无写入锁 |

### 主要问题

#### P0 必须修复

1. **三个发布版本并存** — `publish_daily_report.py`(155行)、`v2`(558行)、`v3`(515行) 功能重叠，应删除旧版
2. **`check_article_quality` 重复定义** — `analyzer.py`（2项检测）和 `quality_check.py`（4项检测+作者+发布时间）逻辑不一致，应合并
3. **`index.json` 无写入保护** — 2447篇文章索引（1.5MB），并发写入会损坏数据

#### P1 重要

4. **硬编码路径** — `publish_daily_report_v3.py` 第38行固定 `/root/.openclaw/workspace/xueqiu-crawler`
5. **凭证分散** — IMA 凭证在脚本中硬编码，XCrawl key 在 `~/.xcrawl/config.json`，应统一到 `config/`
6. **零测试** — 关键函数（`classify_stock_market`、`check_article_quality`）无单元测试

#### P2 可改进

7. **`generate_report.py` 不可导入** — 只能 `python scripts/generate_report.py` 运行，不能被其他模块 import
9. **README 与实际代码脱节** — README 说用 Playwright，实际主流程是 XCrawl

### 修复建议优先级

1. 删除 `publish_daily_report.py` 和 `publish_daily_report_v2.py`
2. 合并 `analyzer.py` 和 `quality_check.py` 的 `check_article_quality`
3. 所有路径和凭证移到 `config/config.yaml`
4. 给 `classify_stock_market` / `check_article_quality` 写单元测试
5. `index.json` 加写入锁或迁移到 SQLite

### 整体评分

**7/10** — 核心流程完整，架构思路清晰，但工程化程度有待提高。