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

import { readFileSync, writeFileSync, existsSync, statSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { homedir } from 'node:os';

import {
  readPipelineRun, isAllEmpty, zeroPostProblems, failureProblem, GROUP_LOGS,
} from './pipeline-stats.mjs';

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

// 六组 pipeline 的收尾状态。**每小时都判**，不只在收官轮 ——
// 2026-10-28 的 g2/g4 是 15:00–16:06 空的，若只在 18:xx 判，等于晚了两个多小时。
// g1–g5 每次 tee **覆盖**，主 job 的 pipeline.log 是跨天**追加**；所以「今天更新过
// + 今天有 [SUMMARY]」才等于「这组今天跑完了」。2026-10-05 踩过：只看 [SUMMARY]
// 不看 mtime，13:30 跑收官分支会把**昨天**的 g2–g5 日志读成 ok —— 那几组今天还没到点。
// 2026-10-28 又踩了另一半：主 job 追加多天，只看「最后一条 [SUMMARY]」而不看日期，
// 今天崩掉的运行会被昨天那条 healthy 记录顶替。
let pipeReport = [];

// 每小时都判，于是同一个没修好的故障会在 15:30/16:30/17:30 反复叫醒人。
// 按「今天 + 同一组同一状态」去重，跟 cron_healthgate.sh 的 .healthgate_last_alert
// 是同一套思路：告警疲劳最终会把真正的故障一起淹掉。
const PIPELINE_ALERT_STATE = join(MONITOR, 'logs', '.peak_pipeline_alert');
// 一轮 pipeline 实测 6–20 分钟；超过这个时长还停在「没有 [SUMMARY]」才算卡死，
// 否则每小时的 :30 轮都会把「正在跑」误报成故障。
const STALL_MINUTES = 25;

// 状态文件按**组**记今天已经报过的问题签名：
//   <日期>\n<组名:签名>\n...
// 组恢复正常就从集合里消失，之后再坏会重新报；只压「同一组同一个状态」的重复。
function readAlertState(today) {
  try {
    const lines = readFileSync(PIPELINE_ALERT_STATE, 'utf8').trim().split('\n');
    return lines[0] === today ? new Set(lines.slice(1).filter(Boolean)) : new Set();
  } catch {
    return new Set(); // 没有状态文件 = 今天还没报过
  }
}

function writeAlertState(today, keys) {
  try {
    writeFileSync(PIPELINE_ALERT_STATE, `${today}\n${[...keys].sort().join('\n')}\n`);
  } catch {
    // 写不进去就每轮都报 —— 宁可多叫，不要静默。
  }
}

function checkPipelines() {
  const today = now.toLocaleDateString('sv-SE');
  const report = [];
  const pipeStats = [];
  const fresh = []; // {key, message}
  for (const name of GROUP_LOGS) {
    const p = join(MONITOR, 'logs', name);
    if (!existsSync(p)) { report.push(`${name}: 缺日志`); continue; }
    const st = statSync(p);
    if (st.mtime.toLocaleDateString('sv-SE') !== today) {
      report.push(`${name}: 今天还没跑`);
      continue;
    }
    const run = readPipelineRun(p, today);
    if (!run.summary) {
      // 今天起了头却没写收尾：正在跑是常态，停了很久才是故障。
      const idle = Math.round((Date.now() - st.mtimeMs) / 60000);
      const stalled = idle > STALL_MINUTES;
      report.push(`${name}: ${stalled ? `跑了但没跑完（日志已停 ${idle} 分钟）` : '正在跑'}`);
      if (stalled) {
        fresh.push({ key: `${name}:stalled`, message: `${name} 今天跑了但没写出 [SUMMARY]，日志已停 ${idle} 分钟` });
      }
      continue;
    }
    if (run.failed > 0) fresh.push({ key: `${name}:${run.failed}`, message: failureProblem(name, run) });
    // failed=0 只说明没抛异常；整组零帖时 [SUMMARY] 照样写 success=N/N（见 check.mjs）。
    pipeStats.push({ name, run });
    const total = run.total ?? run.stocks;
    report.push(
      `${name}: ${run.failed > 0 ? `失败 ${run.failed} 只`
        : isAllEmpty(run) ? `整组零帖 ${total} 只` : 'ok'}`,
    );
  }
  for (const p of zeroPostProblems(pipeStats)) {
    fresh.push({ key: `${p.name}:empty`, message: p.message });
  }

  const prev = readAlertState(today);
  const current = new Set(fresh.map((f) => f.key));
  const toAlert = fresh.filter((f) => !prev.has(f.key));
  writeAlertState(today, current);
  // 组恢复正常时 current 会少掉它，状态文件随之收窄，之后再坏会重新叫。
  if (toAlert.length > 0) problems.push(...toAlert.map((f) => f.message));
  pipeReport = report;
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

  lines.push(`pipeline：${pipeReport.join('  ')}`);
  problems.push('【下午峰值小结】\n  ' + lines.join('\n  '));
}

checkWindow();
checkPipelines();
if (isFinal) buildAfternoonSummary();

if (complete) process.stdout.write('CINDY_PRECHECK_OK\n');

if (problems.length === 0) {
  console.log(`[下午峰值监控 ${hour}:xx] 正常：站点峰值 ${summary?.site_peak?.count ?? '?'} 次/分，无失败`);
  process.exit(2);
}
console.log(`[下午峰值监控 ${hour}:xx] 需要关注：`);
for (const p of problems) console.log('  - ' + p);
process.exit(0);
