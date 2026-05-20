#!/bin/bash
# 雪球爬虫完整流程：爬取 -> 分析 -> 发布到 IMA 笔记 -> 发送链接

set -e

PROJECT_DIR="/root/.openclaw/workspace/xueqiu-crawler"
LOG_FILE="$PROJECT_DIR/logs/cron.log"
DATE=$(date +%Y-%m-%d)

echo "========================================" >> "$LOG_FILE"
echo "[$(date)] 开始执行雪球爬虫流程" >> "$LOG_FILE"

cd "$PROJECT_DIR"

# 1. 爬取新文章
echo "[1/4] 爬取新文章..." >> "$LOG_FILE"
/usr/bin/python3 scripts/crawler.py >> "$LOG_FILE" 2>&1

# 2. 同步到 Link-Collector 知识库
echo "[2/4] 同步到 Link-Collector..." >> "$LOG_FILE"
/usr/bin/python3 scripts/import_to_link_collector.py --today >> "$LOG_FILE" 2>&1 || echo "同步失败（继续执行）" >> "$LOG_FILE"

# 3. 生成分析报告（使用新的分析器）
echo "[3/4] 生成分析报告..." >> "$LOG_FILE"
export BAILIAN_API_KEY="sk-727ad633253f477d84255d434826aabd"
# MiniMax 配置（默认 provider）
export MINIMAX_API_KEY="sk-cp-TwBOoPIuSM5epDnpmYaydXtVQvk3ENBJWmUSYXYOpPRZJCzy8s_QKjgtFGeeThlsua8uYQ5Lv9L4uDNAfJp0PO6qaZ8-lo_LR-YhRZsIZ4RQI8FyWFCQH_Q"
export MINIMAX_BASE_URL="https://api.minimaxi.com/anthropic"
export ANALYZER_PROVIDER="minimax"
/usr/bin/python3 scripts/generate_report.py --limit 20 >> "$LOG_FILE" 2>&1

# 4. 发布到 IMA 笔记并发送链接
echo "[4/4] 发布到 IMA 笔记..." >> "$LOG_FILE"
/usr/bin/python3 scripts/publish_daily_report_v3.py >> "$LOG_FILE" 2>&1

echo "[$(date)] 流程执行完成" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"