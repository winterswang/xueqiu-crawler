#!/bin/bash
# 影子轮（gateway）独立运行 v1 —— 对齐基线 → gateway 抓一轮 → 与 legacy 当天产物对比
#
# 为什么独立成脚本（2026-10-13 实测）:
#   雪球的风控墙不是「冷却时间」，而是**约 80 次页面请求/滚动小时**的额度。生产一遍
#   64 次刚好过；同一窗口里再打第二遍必然踩线 —— 当天：生产 64 次全过，影子续打到
#   第 86 次被拦，11 个账号全黑、对比整轮作废。「影子挂在 run_daily.sh 的 EXIT trap
#   里、只隔 3 分钟」乃至「延后 10–15 分钟」都跨不过这个额度，因为它按滚动小时算。
#   所以影子改成**独立的固定窗口**：调度器每天 09:30 跑本脚本，与生产的 08:00 不同小时。
#
# 用法:
#   bash scripts/run_shadow.sh              # 基线同步 → 抓 → 对比
#   bash scripts/run_shadow.sh --skip-sync  # 跳过基线同步（只调试抓取时用）
#
# 产物（**只写 data-gateway/ 与 logs/**，绝不写 data/）:
#   data-gateway/                     gateway 抓取结果（含 gateway.sqlite3 台账）
#   logs/shadow_compare_<date>.json   两侧对比结论
#   logs/shadow_daily.log             本脚本日志（**不写 cron_daily.log**：那是生产链路的
#                                     日志，巡检脚本读它判断生产状态，别掺影子输出）
#
# 退出码:
#   0 = 跑完并产出了对比（**两侧有差异也算 0** —— 差异是业务结论，不是脚本故障）
#   1 = 脚本自身失败（基线同步失败 / gateway 跑不起来 / 对比无法得出结论）→ 需要人看
#   0 = 主动跳过（生产链路还在跑，或本脚本上一轮还没结束）—— 日志里会写明原因

set -e

SKIP_SYNC=0
case "${1:-}" in
    --skip-sync) SKIP_SYNC=1 ;;
    "") ;;
    *) echo "未知参数: $1（可用: --skip-sync）" >&2; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/shadow_daily.log"
DATE=$(date +%Y-%m-%d)

SHADOW_DIR="$PROJECT_DIR/data-gateway"
GATEWAY_DB="$SHADOW_DIR/gateway.sqlite3"

mkdir -p "$PROJECT_DIR/logs"
cd "$PROJECT_DIR"

# Python 解释器：显式传 PYTHON_BIN 时用它（生产的 Hermes 任务就是这么传 venv 的）；
# 否则**自己解析**，优先仓库 venv、退回 PATH 里的 python3。
# 为什么不像 run_daily.sh 那样只认 `python3`：本脚本由调度器直接执行（script 模式，
# cwd=项目根），而调度配置里只有一条 command、没有单独传 env 的地方 —— 要是把解释器
# 写进 command 前缀（PYTHON_BIN=... bash ...），就依赖"宿主一定是经 shell 执行"这个
# 未文档化的前提。自己解析最稳，也让手工跑与调度跑行为一致。
if [ -z "${PYTHON_BIN:-}" ]; then
    if [ -x "$PROJECT_DIR/venv/bin/python" ]; then
        PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
    else
        PYTHON_BIN="python3"
    fi
fi

# .env 与 run_daily.sh 同一套来源
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

log() { echo "[$(date '+%F %T')] [shadow] $*" >> "$LOG_FILE"; }

# 跨平台锁龄（分钟）：Linux stat -c %Y / macOS stat -f %m
lock_age_minutes() {
    local file="$1"
    local mtime
    mtime=$(stat -c %Y "$file" 2>/dev/null || stat -f %m "$file" 2>/dev/null || echo 0)
    echo $(( ($(date +%s) - mtime) / 60 ))
}

# ── 守卫 1：生产链路还在跑就别开始 ──
# 影子与生产打的是同一批详情页。生产（08:00）拖长时若影子照点开跑，就又变成
# 「同窗口两遍」—— 正是本脚本要消灭的那个形态。锁龄 < 60 分钟一律当"还在跑"。
PROD_LOCK="$PROJECT_DIR/.cron_running.lock"
if [ -f "$PROD_LOCK" ]; then
    AGE=$(lock_age_minutes "$PROD_LOCK")
    if [ "$AGE" -lt 60 ]; then
        log "⚠️ 生产链路仍在运行（.cron_running.lock 锁龄 ${AGE} 分钟），本轮跳过"
        exit 0
    fi
fi

# ── 守卫 2：本脚本自身的并发锁（保护窗口 90 分钟） ──
# 基线同步 + 抓取一共约 15 分钟；给足余量，避免调度抖动或手动补跑撞在一起。
LOCKFILE="$PROJECT_DIR/.shadow_running.lock"
LOCK_WINDOW_MINUTES=90
if [ -f "$LOCKFILE" ]; then
    AGE=$(lock_age_minutes "$LOCKFILE")
    if [ "$AGE" -lt "$LOCK_WINDOW_MINUTES" ]; then
        log "⚠️ 影子轮已在运行中（锁龄 ${AGE} 分钟 / 窗口 ${LOCK_WINDOW_MINUTES} 分钟），本轮跳过"
        exit 0
    fi
    log "🔧 影子锁已过期（${AGE} 分钟 > ${LOCK_WINDOW_MINUTES} 分钟），接管"
fi

release_lock() {
    # 只删自己的锁：锁过期被下一实例接管后，无条件删除会拆掉它的锁
    if grep -q "^$$ " "$LOCKFILE" 2>/dev/null; then
        rm -f "$LOCKFILE"
    fi
}
trap 'release_lock' EXIT

printf '%s %s\n' "$$" "$(date +%s)" > "$LOCKFILE.tmp" && mv -f "$LOCKFILE.tmp" "$LOCKFILE"

log "======== 影子轮开始（date=$DATE，PYTHON_BIN=$PYTHON_BIN）========"

# ── 1/3 基线同步 ──
# 只把 legacy 侧「今天之前」的已知状态对齐过来（今天的一律不碰，两侧必须各自
# 独立发现今天的新文）。不做这一步的后果：每被墙一天影子就永久欠账，下一轮把
# 旧文当成今天的新文重抓（2026-10-13 实测 +25% 请求量、8 篇假差异）。
if [ "$SKIP_SYNC" != "1" ]; then
    log "[1/3] 对齐基线（legacy「今天之前」的已知状态 → 影子目录）"
    if ! $PYTHON_BIN scripts/sync_shadow_baseline.py \
            --legacy-dir data --shadow-dir "$SHADOW_DIR" --date "$DATE" >> "$LOG_FILE" 2>&1; then
        log "❌ 基线同步失败，本轮中止 —— 带着错误基线抓出来的对比没有意义"
        exit 1
    fi
else
    log "[1/3] 已跳过基线同步（--skip-sync）"
fi

# ── 2/3 gateway 抓取 ──
# `crawl-gateway run` 只在**真的跑不起来**时非 0（账号级失败会走 completed_with_failures
# 但仍返回 0，见 cli.py），所以这里非 0 = 需要人介入，不必再往下对比。
log "[2/3] gateway 抓取（purpose=daily-shadow）"
if $PYTHON_BIN -m crawl_gateway --config config/sites.yaml \
        --db "$GATEWAY_DB" run --site xueqiu --purpose daily-shadow \
        --all-accounts --execute --data-dir "$SHADOW_DIR" >> "$LOG_FILE" 2>&1; then
    log "✅ gateway 抓取结束"
else
    log "❌ gateway 抓取失败（退出码 $?），本轮中止"
    exit 1
fi

# ── 3/3 对比 ──
# 对比脚本退出码：0 无差异 / 1 有差异 / 2 读不出结论（见其 docstring）。
# 只有 2 算脚本故障 —— 有差异是业务结论，不该让调度器报警。
log "[3/3] 对比两侧当天产物"
set +e
$PYTHON_BIN scripts/compare_crawl_outputs.py \
    --legacy-dir data --gateway-dir "$SHADOW_DIR" \
    --gateway-db "$GATEWAY_DB" \
    --date "$DATE" \
    --out "logs/shadow_compare_$DATE.json" >> "$LOG_FILE" 2>&1
COMPARE_RC=$?
set -e

case "$COMPARE_RC" in
    0) log "✅ 两侧产物无差异" ;;
    1) log "⚠️ 两侧产物有差异，见 logs/shadow_compare_$DATE.json（业务结论，不算脚本失败）" ;;
    *)
        log "❌ 对比脚本无法得出结论（退出码 $COMPARE_RC）—— 常见原因：两侧基线没对齐、日期没有产物"
        exit 1
        ;;
esac

log "======== 影子轮完成（产物：logs/shadow_compare_$DATE.json）========"
