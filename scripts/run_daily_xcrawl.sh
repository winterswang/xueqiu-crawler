#!/bin/bash
# 雪球爬虫完整流程 v2 - XCrawl 版本
# 凭证通过 .env 文件或环境变量加载（见 .env.example）

set -e

# 根据脚本位置自动推断项目目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/cron_xcrawl.log"
DATE=$(date +%Y-%m-%d)

# 加载 .env 文件（如有）
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

echo "========================================" >> "$LOG_FILE"
echo "[$(date)] 开始执行雪球爬虫流程 v2 (XCrawl)" >> "$LOG_FILE"

# 确保日志目录存在
mkdir -p "$PROJECT_DIR/logs"

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

# 4. 生成分析报告（BAILIAN_API_KEY 从 .env 或环境变量读取）
echo "[4/5] 生成分析报告..." >> "$LOG_FILE"
/usr/bin/python3 scripts/generate_report.py --limit 20 >> "$LOG_FILE" 2>&1

# 5. 发布到 IMA 笔记并发送链接
echo "[5/5] 发布到 IMA 笔记..." >> "$LOG_FILE"
/usr/bin/python3 scripts/publish_daily_report_v3.py >> "$LOG_FILE" 2>&1

echo "[$(date)] 流程执行完成" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"