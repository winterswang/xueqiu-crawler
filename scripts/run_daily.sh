#!/bin/bash
# 雪球爬虫完整流程 v9 - nodriver 版本（6 步流水线 + WAF 绕过）
# 凭证通过 .env 文件或环境变量加载（见 .env.example）
#
# 用法:
#   bash scripts/run_daily.sh               # 完整流程（爬取 + 分析 + 发布）
#   bash scripts/run_daily.sh --crawl-only  # 仅爬取（不含分析/发布）
#   bash scripts/run_daily.sh --skip-crawl  # 跳过爬取（仅分析 + 发布）
#
# 爬取引擎由 CRAWL_ENGINE 环境变量选择（默认 legacy）：
#   legacy  = 旧链路写 data/（现状）
#   gateway = crawl-gateway 写 data/（切换后用）
#   shadow  = 旧链路写 data/（生产链路本身不变）；**影子（gateway）不再挂在本脚本里**
#             —— 它已独立成 scripts/run_shadow.sh，由调度器排在**另一个窗口**（09:30）
# 例: CRAWL_ENGINE=shadow bash scripts/run_daily.sh --crawl-only
#
# 影子为什么必须挪出去（2026-10-13 实测）：
#   雪球的风控墙不是「冷却时间」，而是**约 80 次页面请求/滚动小时**的额度。生产
#   一遍 64 次刚好过；同一窗口里再打第二遍必然踩线 —— 当天生产 64 次全过，影子
#   续打到**第 86 次**被拦，11 个账号全黑、对比整轮作废。原来那套「影子挂在 EXIT
#   trap、只隔 3 分钟」的做法（甚至延后 10–15 分钟）都跨不过这个额度。

set -e

MODE="full"
case "${1:-}" in
    --crawl-only) MODE="crawl-only" ;;
    --skip-crawl) MODE="skip-crawl" ;;
    "") ;;
    *) echo "未知参数: $1（可用: --crawl-only / --skip-crawl）" >&2; exit 1 ;;
esac

# 根据脚本位置自动推断项目目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/cron_daily.log"
DATE=$(date +%Y-%m-%d)

# 加载 .env 文件（如有）— 强制导出所有变量
# 必须在读取 CRAWL_ENGINE 之前：切换/回滚方式就是往 .env 里写 CRAWL_ENGINE。
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
    # 显式导出关键变量（防御性）
    # 注意：模型 id 不再走环境变量——唯一来源是 config/config.yaml
    # （见 scripts/llm_config.py 与 PROJECT_LOG D-009），故不在此 export。
    export MINIMAX_API_KEY MINIMAX_BASE_URL BAILIAN_API_KEY ARK_API_KEY ARK_CODING_BASE_URL 2>/dev/null || true
fi

# 爬取引擎（见 docs/crawl_gateway_migration_plan.md Step 3/4）
#   legacy  = 旧链路，写 data/（默认，现状）
#   gateway = crawl-gateway 写 data/
#   shadow  = 旧链路照常写 data/（生产链路本身不变）；影子由 scripts/run_shadow.sh
#             在另一个窗口独立跑（见文件头）
# 刻意放在取锁之前：配错引擎应立刻失败，不留锁、不写生产日志。
CRAWL_ENGINE="${CRAWL_ENGINE:-legacy}"
case "$CRAWL_ENGINE" in
    legacy|gateway|shadow) ;;
    *) echo "未知 CRAWL_ENGINE: $CRAWL_ENGINE（可用: legacy / gateway / shadow）" >&2; exit 1 ;;
esac

# 释放并发锁。只由 EXIT trap 的**末尾**调用。
#
# 为什么从 cleanup 里拆出来（2026-10-12）：cleanup 会在爬取阶段结束时被调用一次
# 去杀浏览器进程，而锁必须留到整轮业务真正结束（步骤 3–6 还在这把锁的保护范围内），
# 否则来一次并发调用就会两轮同时打雪球。
release_lock() {
    # 只删自己的锁: 锁过期被下一实例接管后,无条件删除会拆掉它的锁
    if grep -q "^$$ " "$PROJECT_DIR/.cron_running.lock" 2>/dev/null; then
        rm -f "$PROJECT_DIR/.cron_running.lock"
    fi
}

# 资源清理函数：防止 OOM（僵尸 Chromium 进程）。可重复调用。
cleanup() {
    # nodriver 使用 google-chrome，Playwright 使用 chromium_headless_shell
    pkill -f "google-chrome.*headless" 2>/dev/null || true
    pkill -f "chromium_headless_shell" 2>/dev/null || true
    pkill -f "playwright/driver" 2>/dev/null || true
    sleep 1

    # 清理 nodriver 临时 profile（超过 1 小时的）
    # 这是**旧 OpenClaw 宿主**的 cache 位置；当前主机用 opencli，其状态在 ~/.opencli/，
    # 且 opencli 源码里不存在 uc_ 前缀目录 —— 所以本行在当前主机上是空操作。
    # 保留是因为它无害，且若运行时回退到 OpenClaw 仍然有效；不再写死 /root，
    # 以便换主机/换用户后依然指向对的地方（旧 Linux 主机上 HOME=/root，语义等价）。
    find "${XDG_CACHE_HOME:-$HOME/.cache}/openclaw" -maxdepth 1 -name 'uc_*' -type d -mmin +60 -exec rm -rf {} \; 2>/dev/null || true
}

# 兼容旧函数名
cleanup_chromium() {
    cleanup
}

# 内存检查：低于 500MB 可用时告警
AVAILABLE_MEM=$(awk '/^MemAvailable:/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo "unknown")
echo "[$(date)] 可用内存: ${AVAILABLE_MEM}MB" >> "$LOG_FILE"
if [ "$AVAILABLE_MEM" != "unknown" ] && [ "$AVAILABLE_MEM" -lt 500 ]; then
    echo "[$(date)] ⚠️ 可用内存不足 500MB (${AVAILABLE_MEM}MB)，先清理缓存..." >> "$LOG_FILE"
    sync && echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
    cleanup_chromium
fi

# 防止并发执行（lockfile）— 保护窗口 1 小时
LOCKFILE="$PROJECT_DIR/.cron_running.lock"
LOCK_WINDOW_MINUTES=60

if [ -f "$LOCKFILE" ]; then
    # 检查锁文件修改时间是否在保护窗口内
    # 跨平台 mtime: Linux stat -c %Y / macOS stat -f %m
    # (20261009 修复: BSD stat -c 报 illegal option → 兜底 echo 0 → 锁龄恒为巨值,锁完全失效)
    LOCK_MTIME=$(stat -c %Y "$LOCKFILE" 2>/dev/null || stat -f %m "$LOCKFILE" 2>/dev/null || echo 0)
    LOCK_AGE_MIN=$(($(date +%s) - LOCK_MTIME))
    LOCK_AGE_MIN=$((LOCK_AGE_MIN / 60))
    
    if [ "$LOCK_AGE_MIN" -lt "$LOCK_WINDOW_MINUTES" ]; then
        echo "[$(date)] ⚠️ 前一次执行在 ${LOCK_AGE_MIN} 分钟内完成（保护窗口 ${LOCK_WINDOW_MINUTES} 分钟），跳过本次" >> "$LOG_FILE"
        exit 0
    else
        echo "[$(date)] 🔧 锁文件已过期 (${LOCK_AGE_MIN} 分钟 > ${LOCK_WINDOW_MINUTES} 分钟)，允许新执行" >> "$LOG_FILE"
    fi
fi

# 提前设置trap，保证任何退出（包括写锁后立即崩溃）都会清理锁
# 顺序：cleanup（收尾杀 Chrome）→ release_lock（必须在最后）。
# `|| true` 不能省：EXIT trap 里前一条命令返回非 0 时，后面的命令**不会**执行，
# 残留的就是锁文件（2026-10-12 实测）。
trap 'cleanup || true; release_lock' EXIT

# 写入当前 PID 和时间戳
printf '%s %s\n' "$$" "$(date +%s)" > "$LOCKFILE.tmp" && mv -f "$LOCKFILE.tmp" "$LOCKFILE"

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
    # 1. 检查登录态
    echo "[1/6] 检查登录态..." >> "$LOG_FILE"
    $PYTHON_BIN scripts/cookies.py --check >> "$LOG_FILE" 2>&1 || echo "Cookies 未配置（nodriver 将直接尝试）" >> "$LOG_FILE"

    # 2. 爬取新文章
    echo "[2/6] 爬取新文章（engine=$CRAWL_ENGINE）..." >> "$LOG_FILE"
    case "$CRAWL_ENGINE" in
        legacy)
            # nodriver — 绕过阿里云 WAF
            $PYTHON_BIN scripts/crawler_nodriver.py --all --max 20 >> "$LOG_FILE" 2>&1
            ;;
        gateway)
            # 与旧链路保持一致：爬取失败即中止整个流水线（现状行为）
            $PYTHON_BIN -m crawl_gateway verify --site xueqiu >> "$LOG_FILE" 2>&1
            $PYTHON_BIN -m crawl_gateway run --site xueqiu --purpose daily \
                --all-accounts --execute --data-dir data >> "$LOG_FILE" 2>&1
            ;;
        shadow)
            # 生产链路与 legacy 完全一致（写 data/）。影子（gateway）**不在这里** ——
            # 它已独立成 scripts/run_shadow.sh，由调度器排在另一个窗口。
            # 曾经是挂在本脚本 EXIT trap 里紧接着跑（只隔 3 分钟），2026-10-13 实测
            # 证明必然撞墙：当天生产 64 次请求全过，影子续打到第 86 次被拦、11 个
            # 账号全黑，对比整轮作废。
            $PYTHON_BIN scripts/crawler_nodriver.py --all --max 20 >> "$LOG_FILE" 2>&1
            ;;
    esac

    # 爬取完成后立即清理 Chrome，释放内存供后续 AI 分析使用。
    # cleanup 的 pkill 模式是 **headless 专用**（google-chrome.*headless /
    # chromium_headless_shell / playwright/driver），而 opencli 走的是扩展模式、
    # 宿主是用户正常打开的 Chrome，不在其中 —— 所以这里不会误杀 opencli 的宿主。
    # ⚠️ 若将来出现「headless 跑 opencli」的宿主，这一句要重新评估。
    # 并发锁不在这里释放（见 release_lock）。
    echo "[清理] 释放浏览器资源..." >> "$LOG_FILE"
    cleanup_chromium
fi

# === 分析 + 发布阶段（--crawl-only 时跳过） ===
if [ "$MODE" != "crawl-only" ]; then
    # 2.5 抓取首页热门贴 + 打分（产出 data/hot_posts/{今天}/hot_top10.json）。
    # 失败不影响主流程：generate_report 读不到就自动降级为无热门段的日报。
    echo "[2.5/6] 抓取首页热门贴..." >> "$LOG_FILE"
    $PYTHON_BIN scripts/hot_posts.py >> "$LOG_FILE" 2>&1 \
        || echo "热门贴抓取失败（不影响主流程，日报无热门段）" >> "$LOG_FILE"

    # 3. 生成分析报告（MINIMAX_API_KEY 从 .env 或环境变量读取）
    echo "[3/6] 生成分析报告..." >> "$LOG_FILE"
    $PYTHON_BIN scripts/generate_report.py --limit 50 >> "$LOG_FILE" 2>&1

    # 4. 发布到 IMA 笔记并发送链接
    echo "[4/6] 发布到 IMA 笔记..." >> "$LOG_FILE"
    IMA_NOTE_URL=$($PYTHON_BIN scripts/publish_daily_report.py 2>&1 | grep -oE 'https://ima\.qq\.com/note/[a-zA-Z0-9]+' | head -1)
    echo "IMA 笔记: $IMA_NOTE_URL" >> "$LOG_FILE"
    
    # 5. 增量同步当日爬取的原始文章到IMA雪球内容知识库（非阻塞，失败不影响主流程）
    echo "[5/6] 同步原始文章到IMA知识库..." >> "$LOG_FILE"
    $PYTHON_BIN scripts/sync_raw_articles_to_ima.py >> "$LOG_FILE" 2>&1 || echo "原始文章同步失败（不影响主流程）" >> "$LOG_FILE"
    
    # 6. 生成飞书摘要
    echo "[6/6] 生成飞书推送摘要..." >> "$LOG_FILE"
    echo "========================================" >> "$LOG_FILE"
    echo "📊 价值投资日报 - $(date +%Y-%m-%d)" >> "$LOG_FILE"
    IMA_NOTE_URL="$IMA_NOTE_URL" $PYTHON_BIN scripts/push_feishu.py 2>&1 | tee -a "$LOG_FILE"
    echo "========================================" >> "$LOG_FILE"
fi

echo "[$(date)] 流程执行完成" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"
