#!/bin/bash
# 雪球爬虫完整流程 v2 - XCrawl 版本

set -e

PROJECT_DIR="/root/.openclaw/workspace/xueqiu-crawler"
LOG_FILE="$PROJECT_DIR/logs/cron_xcrawl.log"
DATE=$(date +%Y-%m-%d)

echo "========================================" >> "$LOG_FILE"
echo "[$(date)] 开始执行雪球爬虫流程 v2 (XCrawl)" >> "$LOG_FILE"

cd "$PROJECT_DIR"

# 1. 检查登录态
echo "[1/5] 检查登录态..." >> "$LOG_FILE"
/usr/bin/python3 scripts/cookies.py --check >> "$LOG_FILE" 2>&1

# 2. 爬取新文章（XCrawl 版本）
echo "[2/5] 爬取新文章（XCrawl）..." >> "$LOG_FILE"
/usr/bin/python3 scripts/crawler_xcrawl.py --all --max 20 >> "$LOG_FILE" 2>&1 || {
    echo "⚠️  XCrawl 爬取失败，尝试 Playwright fallback" >> "$LOG_FILE"
    /usr/bin/python3 scripts/crawler.py >> "$LOG_FILE" 2>&1
}

# 3. 同步到 Link-Collector 知识库
echo "[3/5] 同步到 Link-Collector..." >> "$LOG_FILE"
/usr/bin/python3 scripts/import_to_link_collector.py --today >> "$LOG_FILE" 2>&1 || echo "同步失败（继续执行）" >> "$LOG_FILE"

# 4. 生成分析报告
echo "[4/5] 生成分析报告..." >> "$LOG_FILE"
export BAILIAN_API_KEY="sk-727ad633253f477d84255d434826aabd"
/usr/bin/python3 scripts/generate_report.py --limit 20 >> "$LOG_FILE" 2>&1

# 5. 发布到 IMA 笔记并发送链接
echo "[5/5] 发布到 IMA 笔记..." >> "$LOG_FILE"
/usr/bin/python3 scripts/publish_daily_report_v3.py >> "$LOG_FILE" 2>&1

echo "[$(date)] 流程执行完成" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"