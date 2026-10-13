#!/bin/bash
# cron_shadow.sh — Cindy scheduler script 模式入口（cindy-script/1 协议）。
#
# 影子轮本体是 scripts/run_shadow.sh（可直接手工跑，产物在 data-gateway/ 与 logs/）。
# 本入口只做两件事：
#   ① 把它的 stdout/stderr 引到调度侧日志（本体的运行日志写在 logs/shadow_daily.log）
#   ② **在 stdout 上输出且只输出协议帧** —— 调度侧按 cindy-script/1 解析，
#      多一行普通输出就会解析失败
#
# 与 scripts/cron_sentinel.sh 同一套写法（那是本机既有的、已跑通的 scheduler 入口）。
#
# 退出码语义（与 run_shadow.sh 一致）：
#   0 = 跑完并产出对比（两侧有差异也是 0）；也包含"主动跳过"（生产还在跑 / 上一轮未结束）
#   1 = 脚本自身失败 → 协议帧带 ok:false，调度侧把本轮记为 failed
#
# 失败**不发通知**（notify 全关，与其它 script 模式任务一致）——需要主动告警时
# 再加飞书通道，别默认打开。

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if bash "$ROOT/scripts/run_shadow.sh" >&2; then
    printf '%s\n' '{"protocol":"cindy-script/1","type":"complete"}'
else
    printf '%s\n' '{"protocol":"cindy-script/1","type":"complete","ok":false,"error":"run_shadow 非零退出：见 logs/shadow_daily.log 与调度侧 stderr"}'
    exit 1
fi
