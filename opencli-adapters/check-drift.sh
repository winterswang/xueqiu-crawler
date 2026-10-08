#!/usr/bin/env bash
# 对比本插件与已安装 opencli 包内的官方基线，报告「我们的覆盖是否还有必要」。
# 每次升级 opencli 后跑一次。只读，不改任何文件。
#
# 用法： ./check-drift.sh
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PKG_ROOT="$(npm root -g 2>/dev/null)/@jackwener/opencli"
BASE="$PKG_ROOT/clis"
if [ ! -d "$BASE" ]; then
  echo "错误：找不到 opencli 包基线目录 $BASE" >&2
  exit 1
fi

VER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$PKG_ROOT/package.json" 2>/dev/null || echo '?')"
echo "官方基线版本: $VER"
echo "基线目录:     $BASE"
echo

# ── 1. 覆盖型：上游若已等于我们，覆盖即可删除 ────────────────────────────
echo "== 覆盖型适配器（上游若已修好，删掉本地文件改用基线）"
for pair in "comments.js:xueqiu/comments.js" "search.js:xiaohongshu/search.js"; do
  ours="${pair%%:*}"; base="${pair##*:}"
  if [ ! -f "$BASE/$base" ]; then
    echo "  ??   $ours — 基线中已不存在 $base"
    continue
  fi
  if diff -q "$DIR/$ours" "$BASE/$base" >/dev/null 2>&1; then
    echo "  DROP $ours — 与基线完全一致，覆盖已无必要，可删除"
  else
    n=$(diff "$DIR/$ours" "$BASE/$base" 2>/dev/null | grep -c '^[<>]' || true)
    echo "  KEEP $ours — 与基线有 $n 行差异（覆盖仍生效）"
  fi
done
echo

# ── 2. 新增型：上游若也加了同名命令，考虑改用基线 ────────────────────────
echo "== 新增型适配器（上游若已内置同名命令，考虑改用基线）"
for f in news.js replies.js stock-notices.js user-articles.js; do
  if [ -f "$BASE/xueqiu/$f" ]; then
    echo "  WARN xueqiu/$f — 上游已内置，建议改用基线版本"
  else
    echo "  OK   $f — 上游仍无此命令，必须保留"
  fi
done
# article.js 的 site 是自建的 web，不在 $BASE/xueqiu/ 下 —— 单独判，别按雪球路径拼。
if [ -f "$BASE/web/article.js" ]; then
  echo "  WARN web/article.js — 上游已内置通用 web 站，建议改用基线版本"
else
  echo "  OK   article.js — 上游 web 站下无 article 命令，必须保留"
fi
echo

# ── 3. 携带副本：上游改动过就要手动同步 ──────────────────────────────────
echo "== 随插件携带的基线副本"
if [ ! -f "$BASE/xueqiu/utils.js" ]; then
  echo "  ??   utils.js — 基线中已不存在"
elif diff -q "$DIR/utils.js" "$BASE/xueqiu/utils.js" >/dev/null 2>&1; then
  echo "  OK   utils.js — 与基线一致"
else
  echo "  SYNC utils.js — 上游已改动，需手动同步："
  diff "$DIR/utils.js" "$BASE/xueqiu/utils.js" | sed 's/^/       /'
fi
echo

# ── 4. 残留的 clis 裸覆盖 ───────────────────────────────────────────────
echo "== ~/.opencli/clis/ 残留裸覆盖（plugin 会盖过它们，重复维护易混淆）"
USER_CLIS="$HOME/.opencli/clis"
if [ -d "$USER_CLIS" ]; then
  real=0
  while IFS= read -r f; do
    rel="${f#$USER_CLIS/}"
    if [ -f "$BASE/$rel" ] && diff -q "$f" "$BASE/$rel" >/dev/null 2>&1; then
      echo "  redundant  $rel — 与基线一致，可删"
    else
      echo "  OVERRIDE   $rel — 与基线不同（真正的裸覆盖）"
      real=$((real + 1))
    fi
  done < <(find "$USER_CLIS" -type f -name '*.js' 2>/dev/null | sort)
  [ "$real" -eq 0 ] && echo "  → 无真实裸覆盖，可整目录删除"
else
  echo "  （目录不存在）"
fi

# ── 5. BLOCK_GUARD 是否与 Python 侧唯一实现同源 ─────────────────────────
echo "== BLOCK_GUARD 与 xueqiu_analyzer.waf.CONTENT_PATTERNS 是否同源"
SYNC="$DIR/../scripts/sync_waf_patterns.py"
if [ -f "$SYNC" ]; then
  if python3 "$SYNC" --check >/tmp/waf_sync_check.out 2>&1; then
    sed 's/^/  /' /tmp/waf_sync_check.out
  else
    sed 's/^/  /' /tmp/waf_sync_check.out
    echo "  → 运行 python3 scripts/sync_waf_patterns.py 修复"
  fi
else
  echo "  ??   找不到 $SYNC"
fi

exit 0
