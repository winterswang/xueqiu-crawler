#!/usr/bin/env bash
# 安装 / 重装本目录为 opencli 插件（本地 file:// 安装 = 符号链接，改源码立即生效）。
#
# 用法： ./install.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["name"])' "$DIR/opencli-plugin.json")"

command -v opencli >/dev/null 2>&1 || {
  echo "错误：找不到 opencli，请先 npm i -g @jackwener/opencli" >&2
  exit 1
}

echo "== 插件: $NAME"
echo "== 源目录: $DIR"
echo

echo "-- 卸载旧版本（若存在）"
opencli plugin uninstall "$NAME" >/dev/null 2>&1 || true

echo "-- 安装"
opencli plugin install "file://$DIR"

echo
echo "-- 校验命令已注册"
# opencli list 按站点分节缩进输出，取站点块再匹配命令名
LIST="$(opencli list 2>/dev/null)"
site_block() {
  printf '%s\n' "$LIST" | awk -v s="$1" '
    $0 == "  " s { inside = 1; next }
    inside && /^  [A-Za-z0-9_-]+$/ { inside = 0 }
    inside { print }
  '
}
missing=0
for pair in "xueqiu:news" "xueqiu:replies" "xueqiu:stock-notices" "xueqiu:user-articles" \
            "xueqiu:comments" "xiaohongshu:search" "web:article"; do
  site="${pair%%:*}"; cmd="${pair##*:}"
  if site_block "$site" | grep -qE "^    ${cmd}( |$)"; then
    echo "  OK   opencli $site $cmd"
  else
    echo "  MISS opencli $site $cmd"
    missing=1
  fi
done

echo
echo "-- opencli adapter status"
opencli adapter status 2>&1 | sed 's/^/  /'

echo
if [ "$missing" -ne 0 ]; then
  echo "有命令未注册。查看插件加载告警： opencli list 2>&1 | grep -i plugin" >&2
  exit 1
fi
echo "完成。"
