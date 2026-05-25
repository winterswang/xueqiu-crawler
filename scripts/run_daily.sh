#!/bin/bash
# 雪球爬虫完整流程 v5 - Playwright 版本（4 步流水线 + 资源清理）
# 凭证通过 .env 文件或环境变量加载（见 .env.example）

set -e

# 根据脚本位置自动推断项目目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/cron_daily.log"
DATE=$(date +%Y-%m-%d)

# 资源清理函数：防止 OOM（僵尸 Chromium 进程）
cleanup_chromium() {
    # 杀掉所有残留的 Chromium headless shell 和 Playwright driver
    pkill -f "chromium_headless_shell" 2>/dev/null || true
    pkill -f "playwright/driver" 2>/dev/null || true
    # 等待进程退出
    sleep 1
}

# trap 确保即使脚本被中断也清理资源
trap cleanup_chromium EXIT

# 加载 .env 文件（如有）
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

echo "========================================" >> "$LOG_FILE"
echo "[$(date)] 开始执行雪球爬虫流程 v5 (Playwright)" >> "$LOG_FILE"

# 确保日志目录存在
mkdir -p "$PROJECT_DIR/logs"

cd "$PROJECT_DIR"

# 1. 检查登录态
echo "[1/4] 检查登录态..." >> "$LOG_FILE"
/usr/bin/python3 scripts/cookies.py --check >> "$LOG_FILE" 2>&1 || echo "Cookies 未配置（Playwright 将直接尝试）" >> "$LOG_FILE"

# 2. 爬取新文章（Playwright）
echo "[2/4] 爬取新文章（Playwright）..." >> "$LOG_FILE"
/usr/bin/python3 scripts/crawler.py --all --max 20 >> "$LOG_FILE" 2>&1

# 爬取完成后立即清理 Chromium，释放内存供后续 AI 分析使用
echo "[清理] 释放 Playwright 浏览器资源..." >> "$LOG_FILE"
cleanup_chromium

# 3. 生成分析报告（MINIMAX_API_KEY 从 .env 或环境变量读取）
echo "[3/4] 生成分析报告..." >> "$LOG_FILE"
/usr/bin/python3 scripts/generate_report.py --limit 20 >> "$LOG_FILE" 2>&1

# 4. 发布到 IMA 笔记并发送链接
echo "[4/4] 发布到 IMA 笔记..." >> "$LOG_FILE"
/usr/bin/python3 scripts/publish_daily_report.py >> "$LOG_FILE" 2>&1

echo "[$(date)] 流程执行完成" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"
