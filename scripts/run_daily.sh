#!/bin/bash
# 雪球爬虫完整流程 v9 - nodriver 版本（5 步流水线 + WAF 绕过 + 多次 cron 重试）
# 凭证通过 .env 文件或环境变量加载（见 .env.example）
#
# 用法:
#   bash scripts/run_daily.sh               # 完整流程（爬取 + 分析 + 发布）
#   bash scripts/run_daily.sh --crawl-only  # 仅爬取（不含分析/发布）
#   bash scripts/run_daily.sh --retry-failed # 重试上次失败的账号
#   bash scripts/run_daily.sh --skip-crawl  # 跳过爬取（仅分析 + 发布）

set -e

MODE="full"
case "${1:-}" in
    --crawl-only) MODE="crawl-only" ;;
    --retry-failed) MODE="retry-failed" ;;
    --skip-crawl) MODE="skip-crawl" ;;
esac

# 根据脚本位置自动推断项目目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/cron_daily.log"
DATE=$(date +%Y-%m-%d)

# 资源清理函数：防止 OOM（僵尸 Chromium 进程）
cleanup_chromium() {
    # nodriver 使用 google-chrome，Playwright 使用 chromium_headless_shell
    pkill -f "google-chrome.*headless" 2>/dev/null || true
    pkill -f "chromium_headless_shell" 2>/dev/null || true
    pkill -f "playwright/driver" 2>/dev/null || true
    sleep 1
  
    # 清理 nodriver 临时 profile（超过 1 小时的）
    find /root/.cache/openclaw -maxdepth 1 -name 'uc_*' -type d -mmin +60 -exec rm -rf {} \; 2>/dev/null || true
}

# 加载 .env 文件（如有）— 强制导出所有变量
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
    # 显式导出关键变量（防御性）
    export MINIMAX_API_KEY MINIMAX_BASE_URL BAILIAN_API_KEY 2>/dev/null || true
fi

# 内存检查：低于 500MB 可用时告警
AVAILABLE_MEM=$(awk '/^MemAvailable:/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo "unknown")
echo "[$(date)] 可用内存: ${AVAILABLE_MEM}MB" >> "$LOG_FILE"
if [ "$AVAILABLE_MEM" != "unknown" ] && [ "$AVAILABLE_MEM" -lt 500 ]; then
    echo "[$(date)] ⚠️ 可用内存不足 500MB (${AVAILABLE_MEM}MB)，先清理缓存..." >> "$LOG_FILE"
    sync && echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
    cleanup_chromium
fi

# 防止并发执行（lockfile）
LOCKFILE="$PROJECT_DIR/.cron_running.lock"
if [ -f "$LOCKFILE" ]; then
    LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null)
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[$(date)] ⚠️ 前一次执行尚未完成 (PID=$LOCK_PID)，跳过本次" >> "$LOG_FILE"
        exit 0
    else
        echo "[$(date)] 🔧 清理过期锁文件 (PID=$LOCK_PID 已不存在)" >> "$LOG_FILE"
        rm -f "$LOCKFILE"
    fi
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"; cleanup_chromium' EXIT

echo "========================================" >> "$LOG_FILE"
echo "[$(date)] 开始执行雪球爬虫流程 v9 (nodriver)" >> "$LOG_FILE"

# 确保日志目录存在
mkdir -p "$PROJECT_DIR/logs"

cd "$PROJECT_DIR"

# Python 解释器：默认用 PATH 中的 python3，可通过环境变量覆盖
# 例: PYTHON_BIN=/path/to/python3 bash scripts/run_daily.sh
PYTHON_BIN="${PYTHON_BIN:-python3}"

# === 爬取阶段（--skip-crawl 时跳过） ===
if [ "$MODE" != "skip-crawl" ]; then
    if [ "$MODE" = "retry-failed" ]; then
        # ── 重试失败账号 ──
        echo "[retry] 重试上次失败的账号..." >> "$LOG_FILE"
        $PYTHON_BIN scripts/crawler_nodriver.py --retry-failed >> "$LOG_FILE" 2>&1
    else
        # 1. 检查登录态
        echo "[1/4] 检查登录态..." >> "$LOG_FILE"
        $PYTHON_BIN scripts/cookies.py --check >> "$LOG_FILE" 2>&1 || echo "Cookies 未配置（nodriver 将直接尝试）" >> "$LOG_FILE"

        # 2. 爬取新文章（nodriver — 绕过阿里云 WAF）
        echo "[2/4] 爬取新文章（nodriver）..." >> "$LOG_FILE"
        $PYTHON_BIN scripts/crawler_nodriver.py --all --max 20 >> "$LOG_FILE" 2>&1
    fi

    # 爬取完成后立即清理 Chrome，释放内存供后续 AI 分析使用
    echo "[清理] 释放浏览器资源..." >> "$LOG_FILE"
    cleanup_chromium
fi

# === 分析 + 发布阶段（--crawl-only 时跳过） ===
if [ "$MODE" != "crawl-only" ]; then
    # 3. 生成分析报告（MINIMAX_API_KEY 从 .env 或环境变量读取）
    echo "[3/4] 生成分析报告..." >> "$LOG_FILE"
    $PYTHON_BIN scripts/generate_report.py --limit 50 >> "$LOG_FILE" 2>&1

    # 4. 发布到 IMA 笔记并发送链接
    echo "[4/4] 发布到 IMA 笔记..." >> "$LOG_FILE"
    $PYTHON_BIN scripts/publish_daily_report.py >> "$LOG_FILE" 2>&1
fi

echo "[$(date)] 流程执行完成" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"
