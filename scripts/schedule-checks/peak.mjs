#!/usr/bin/env node
// 雪球爬取「下午峰值」监控 —— 每小时巡检一次站点请求压力，而不是事后翻账。
//
// 为什么单独一个脚本
// ------------------
// check.mjs 的 morning/health/eod 三档已经在线上跑着，且各自绑定一个时段。
// 峰值监控要的是**每小时**都判一次，语义不同、触发频率不同，混进去只会
// 让那三档更难推理。所以独立成文件，宿主 preRunHook 直接指向它。
//
// 协议（宿主 preRunHook）:
//   exit 0 → 放行：发现问题（或到了收官那一轮要出下午小结），叫醒 agent
//   exit 2 → 跳过本轮：一切正常，不烧 token、不出声
//   其它非零 → fail-closed：脚本自身出错，宿主阻止本轮并记录
//
// 口径
// ----
// 只有**站点请求**才触发风控。台账里 browser get/extract/close 只跟已打开的
// 本地标签页交互，混进来会把峰值抬高一档（2026-10-05 实测：全部口径 16 次/分，
// 站点口径只有 6 次/分）。所以一律用 analyze_opencli_usage.py 的站点口径。
//
// 阈值的来历
// ----------
// 限速器把相邻 opencli 站点命令的**起始时间**拉开 6–12 秒（跨进程共享），
// 因此单分钟理论上限 = 60/6 = 10 次。实测均值 6 次/分。取 9 次/分作为告警线：
// 高于实际带（6–7）但低于理论上限，真出现说明有限速之外的路径在打请求。
// 若某分钟 > 10 次，那是**确定性**的绕过（间隔不可能短于 6 秒），单独标红。

import { readFileSync, existsSync, statSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { homedir } from 'node:os';

const now = new Date();
const hour = now.getHours();
// 最后一轮（18 点后）除了巡检，还要把整个下午的峰值画像汇总出来。
// XQ_PEAK_FINAL 可覆盖，便于任何时段手工自测收官分支。
const isFinal = process.env.XQ_PEAK_FINAL
  ? process.env.XQ_PEAK_FINAL === '1'
  : hour >= 18;

const CRAWLER = process.env.XQ_CRAWLER_DIR || join(homedir(), 'code/claude_code/xueqiu-crawler');
const MONITOR = process.env.XQ_MONITOR_DIR || join(homedir(), 'code/claude_code/xueqiu-monitor');
const USAGE = join(CRAWLER, 'scripts/analyze_opencli_usage.py');

const PEAK_ALARM = Number(process.env.XQ_PEAK_ALARM || 9);
const PEAK_IMPOSSIBLE = 10; // 6 秒下限 ⇒ 一分钟不可能超过 10 次

const problems = [];
let complete = true;
let summary = null;

function runUsage(hours) {
  const out = execFileSync('python3', [USAGE, '--hours', String(hours), '--json'], {
    encoding: 'utf8', timeout: 120000, cwd: CRAWLER,
  });
  return JSON.parse(out);
}

// 本轮窗口 = 最近 60 分钟。
function checkWindow() {
  try {
    summary = runUsage(1);
  } catch (err) {
    complete = false;
    problems.push(`analyze_opencli_usage 执行失败: ${err.message}`);
    return;
  }
  const peak = summary.site_peak || { minute: '', count: 0 };
  const failed = Number(summary.site_failed || 0);

  if (failed > 0) {
    problems.push(
      `本轮窗口有 ${failed} 次站点请求失败（共 ${summary.site_total} 次）——`
      + `查是否风控页；跑 analyze_opencli_usage.py --hours 1 看失败样本`
    );
  }
  if (peak.count > PEAK_IMPOSSIBLE) {
    problems.push(
      `站点请求峰值 ${peak.count} 次/分（${peak.minute}）超过 6 秒下限的理论上限 ——`
      + `限速器被绕过的确定性证据，查是否有调用没走 acquire_opencli_slot`
    );
  } else if (peak.count > PEAK_ALARM) {
    problems.push(
      `站点请求峰值 ${peak.count} 次/分（${peak.minute}）高于 ${PEAK_ALARM} 的告警线`
    );
  }
}

// 收官轮：把 14:00 起整个下午的峰值画像与六组 pipeline 的产出一起汇总。
function buildAfternoonSummary() {
  let afternoon;
  try {
    afternoon = runUsage(5); // 18:30 往前 5 小时 ≈ 13:30 起
  } catch (err) {
    complete = false;
    problems.push(`下午汇总取数失败: ${err.message}`);
    return;
  }
  const peak = afternoon.site_peak || { minute: '', count: 0 };
  const lines = [
    `近 5 小时站点请求 ${afternoon.site_total} 次（本地命令 ${afternoon.local_total} 次），失败 ${afternoon.site_failed} 次`,
    `近 5 小时站点峰值 ${peak.count} 次/分（${peak.minute}）`,
    `近 5 小时按小时：${Object.entries(afternoon.by_hour || {}).map(([h, n]) => `${h.slice(11)}→${n}`).join('  ')}`,
  ];

  // 六组 pipeline 各自的 [SUMMARY]。日志由各条 cron tee **覆盖**写入，
  // 所以「今天更新过 + 有 [SUMMARY]」才等于「这组今天跑完了」。
  // 2026-10-05 踩过：只看 [SUMMARY] 不看 mtime，13:30 跑收官分支会把**昨天**
  // 的 g2–g5 日志读成 ok —— 那几组今天还没到点。必须按日期先判新鲜度。
  const today = now.toLocaleDateString('sv-SE');
  const groups = ['pipeline.log', 'pipeline-g1.log', 'pipeline-g2.log',
                  'pipeline-g3.log', 'pipeline-g4.log', 'pipeline-g5.log'];
  const pipeReport = [];
  for (const name of groups) {
    const p = join(MONITOR, 'logs', name);
    if (!existsSync(p)) { pipeReport.push(`${name}: 缺日志`); continue; }
    if (statSync(p).mtime.toLocaleDateString('sv-SE') !== today) {
      pipeReport.push(`${name}: 今天还没跑`);
      continue;
    }
    const s = readFileSync(p, 'utf8').match(/\[SUMMARY\] (.+)/);
    if (!s) { pipeReport.push(`${name}: 跑了但没跑完（无 [SUMMARY]）`); continue; }
    const failed = /failed=(\d+)/.exec(s[1]);
    const bad = failed ? Number(failed[1]) : 0;
    if (bad > 0) problems.push(`${name} 有 ${bad} 只失败：${s[1]}`);
    pipeReport.push(`${name}: ${bad > 0 ? `失败 ${bad} 只` : 'ok'}`);
  }
  lines.push(`pipeline：${pipeReport.join('  ')}`);
  problems.push('【下午峰值小结】\n  ' + lines.join('\n  '));
}

checkWindow();
if (isFinal) buildAfternoonSummary();

if (complete) process.stdout.write('CINDY_PRECHECK_OK\n');

if (problems.length === 0) {
  console.log(`[下午峰值监控 ${hour}:xx] 正常：站点峰值 ${summary?.site_peak?.count ?? '?'} 次/分，无失败`);
  process.exit(2);
}
console.log(`[下午峰值监控 ${hour}:xx] 需要关注：`);
for (const p of problems) console.log('  - ' + p);
process.exit(0);
