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
#   shadow  = 旧链路写 data/，gateway 同时写 data-gateway/ 并对比，只告警不阻断
# 例: CRAWL_ENGINE=shadow bash scripts/run_daily.sh --crawl-only

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
#   shadow  = 旧链路照常写 data/；gateway 同时写 data-gateway/ 并对比，只告警不阻断
# 刻意放在取锁之前：配错引擎应立刻失败，不留锁、不写生产日志。
CRAWL_ENGINE="${CRAWL_ENGINE:-legacy}"
case "$CRAWL_ENGINE" in
    legacy|gateway|shadow) ;;
    *) echo "未知 CRAWL_ENGINE: $CRAWL_ENGINE（可用: legacy / gateway / shadow）" >&2; exit 1 ;;
esac

SHADOW_DIR="$PROJECT_DIR/data-gateway"
GATEWAY_DB="$SHADOW_DIR/gateway.sqlite3"

# 资源清理函数：防止 OOM（僵尸 Chromium 进程）+ 删除锁文件
cleanup() {
    # 清理锁文件（无论成功失败都删除，避免下次被锁跳过）
    rm -f "$PROJECT_DIR/.cron_running.lock"
    
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
    LOCK_AGE_MIN=$(($(date +%s) - $(stat -c %Y "$LOCKFILE" 2>/dev/null || echo 0)))
    LOCK_AGE_MIN=$((LOCK_AGE_MIN / 60))
    
    if [ "$LOCK_AGE_MIN" -lt "$LOCK_WINDOW_MINUTES" ]; then
        echo "[$(date)] ⚠️ 前一次执行在 ${LOCK_AGE_MIN} 分钟内完成（保护窗口 ${LOCK_WINDOW_MINUTES} 分钟），跳过本次" >> "$LOG_FILE"
        exit 0
    else
        echo "[$(date)] 🔧 锁文件已过期 (${LOCK_AGE_MIN} 分钟 > ${LOCK_WINDOW_MINUTES} 分钟)，允许新执行" >> "$LOG_FILE"
    fi
fi

# 提前设置trap，保证任何退出（包括写锁后立即崩溃）都会清理锁
trap 'cleanup' EXIT

# 写入当前 PID 和时间戳
echo "$$ $(date +%s)" > "$LOCKFILE"

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
            # ── 影子目录必须在 legacy 开跑**之前**对齐 ──
            # 顺序反了的后果（2026-10-08 实测踩到）：legacy 先跑并写下今天的新文章，
            # 之后才做镜像，于是那些新文章被一并拷给 gateway，gateway 认为今天无事
            # 可做（duplicate/no_update、new_articles=0），对比退化成「拿标准答案改卷」
            # —— 文章层会显示完美一致，但那份 history 是 cp -a 拷来的（两侧 mtime
            # 完全相同即铁证），根本没有验证「两侧抓到的东西是否一致」。
            # 两侧必须从同一个「今天之前」的起点各自去抓，产出的集合才可比。
            if [ ! -f "$SHADOW_DIR/index.json" ]; then
                mkdir -p "$SHADOW_DIR"
                # 整体镜像 data/（排除 daily_reports/；.last_crawl_stats.json 由
                # gateway 自己写）。必须带各账号的 <id>/ 目录：gateway 的已知集合
                # 来自三处 —— index.json + history/ + <id>/*.md，而 index.json 是
                # **有损**的（曾因 OOM/SIGKILL 丢失，见 scripts/rebuild_index.py），
                # 存在文章只在 .md 里、不在 index 里。漏拷会让 gateway 把这些旧文
                # 当新文章重抓（2026-10-07 实测：4 篇去年/年初的老文被当成当天新文）。
                for item in "$PROJECT_DIR"/data/*; do
                    [ -e "$item" ] || continue
                    case "$(basename "$item")" in
                        daily_reports) continue ;;
                    esac
                    cp -a "$item" "$SHADOW_DIR/"
                done
                echo "[shadow] 已镜像 data/ 初始化影子目录已知状态（含各账号 .md）" >> "$LOG_FILE"
            fi

            # 生产链路照常跑（必须在镜像之后）
            $PYTHON_BIN scripts/crawler_nodriver.py --all --max 20 >> "$LOG_FILE" 2>&1

            # 影子运行与对比都只告警、不阻断日报（legacy 才是生产链路）
            if $PYTHON_BIN -m crawl_gateway --config config/sites.yaml \
                    --db "$GATEWAY_DB" run --site xueqiu --purpose daily-shadow \
                    --all-accounts --execute --data-dir "$SHADOW_DIR" >> "$LOG_FILE" 2>&1; then
                if $PYTHON_BIN scripts/compare_crawl_outputs.py \
                        --legacy-dir data --gateway-dir "$SHADOW_DIR" \
                        --gateway-db "$GATEWAY_DB" \
                        --out "logs/shadow_compare_$(date +%Y-%m-%d).json" >> "$LOG_FILE" 2>&1; then
                    echo "[shadow] ✅ 两侧产物无差异" >> "$LOG_FILE"
                else
                    echo "[shadow] ⚠️ 两侧产物有差异，见 logs/shadow_compare_$(date +%Y-%m-%d).json" >> "$LOG_FILE"
                fi
            else
                echo "[shadow] ⚠️ gateway 影子运行失败（不影响主流程）" >> "$LOG_FILE"
            fi
            ;;
    esac

    # 爬取完成后立即清理 Chrome，释放内存供后续 AI 分析使用
    echo "[清理] 释放浏览器资源..." >> "$LOG_FILE"
    cleanup_chromium
fi

# === 分析 + 发布阶段（--crawl-only 时跳过） ===
if [ "$MODE" != "crawl-only" ]; then
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
