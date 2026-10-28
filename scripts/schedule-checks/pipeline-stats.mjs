// pipeline 日志的收尾记录解析 —— check.mjs 与 peak.mjs 共用。
//
// 两种写法混在一起
// ----------------
// `pipeline.log`（13:00 主 job）是跨天**追加**的，同一天里能翻到前几天的
// `[SUMMARY]`；`pipeline-g1..g5.log` 是各条 cron 每次 tee **覆盖**的。
// 所以取收尾记录要同时满足两件事：**最后一次**运行，且**是今天**的。
//
// 只看「最后一次」不够：今天的 job 起了头就卡死/崩掉、还没来得及写收尾时，
// 最后一条仍是昨天的，mtime 却是今天 —— 会被当成「今天跑完了且一切正常」。
// 日志每行都带 `YYYY-MM-DD HH:MM:SS` 前缀，拿它绑定日期。
//
// 为什么读 `Pipeline完成:` 而不是人读日志行
// -----------------------------------------
// `成功但零帖: N` 是给人看的文案，改个措辞断言就静默失效（fail-open）。
// 同一份日志里本来就有结构化收尾：
//   Pipeline完成: {"crawled": 9, "failed": 0, "crawl_health":
//     {"status": "degraded", "total": 9, "success_with_posts": 0, "posts_coverage": 0.0, ...}}
// `crawl_health` 直接给出「成功但没抓到帖子」的只数，用它判断整组空转。
// 老日志没有这个字段时，回退到人读行，别让断言凭空消失。

import { readFileSync } from 'node:fs';

const DONE_MARK = 'Pipeline完成: ';

/** 六组 pipeline 的日志名。判定「六组全空」和巡检遍历都用它，别再各写一份。 */
export const GROUP_LOGS = [
  'pipeline.log',
  'pipeline-g1.log',
  'pipeline-g2.log',
  'pipeline-g3.log',
  'pipeline-g4.log',
  'pipeline-g5.log',
];

function lastMatch(text, re) {
  // matchAll 只接受带 g 的正则；调用方写 `/x/` 时在这里补上，免得每次都要记。
  const global = re.flags.includes('g') ? re : new RegExp(re.source, `${re.flags}g`);
  const all = [...text.matchAll(global)];
  return all.length ? all[all.length - 1][1] : null;
}

/**
 * 读一份 pipeline 日志里**今天最后一次运行**的收尾状态。
 *
 * @param {string} file 日志绝对路径。
 * @param {string} today `YYYY-MM-DD`；只认带这个日期前缀的行。不传则不按日期过滤
 *   （仅便于手工排查，巡检一律传）。
 * @returns {{summary: string|null, stocks: number, failed: number,
 *   total: number|null, withPosts: number|null, coverage: number|null,
 *   status: string|null, trailZero: number|null}}
 *   `summary` 为 null 表示今天没有收尾记录（没跑、或跑到一半崩了）；
 *   `total`/`withPosts` 来自 `crawl_health`，缺失时为 null（老日志）。
 */
export function readPipelineRun(file, today = null) {
  const text = readFileSync(file, 'utf8');
  const days = text.split('\n').filter((line) => !today || line.startsWith(today));

  // 从尾部往前找一个能解析的收尾 JSON —— 进程被打断时最后一行可能是半截。
  let done = null;
  for (let i = days.length - 1; i >= 0 && done === null; i -= 1) {
    const at = days[i].indexOf(DONE_MARK);
    if (at < 0) continue;
    try {
      done = JSON.parse(days[i].slice(at + DONE_MARK.length).trim());
    } catch {
      done = null;
    }
  }

  const scope = days.join('\n');
  const summary = lastMatch(scope, /\[SUMMARY\] (.+)/);
  const health = done?.crawl_health ?? null;
  const zeroText = lastMatch(scope, /成功但零帖: (\d+)/);

  return {
    summary,
    stocks: Number(/stocks=(\d+)/.exec(summary ?? '')?.[1] ?? 0),
    failed: Number(/failed=(\d+)/.exec(summary ?? '')?.[1] ?? 0),
    success: Number(/success=(\d+)/.exec(summary ?? '')?.[1] ?? 0),
    total: health?.total ?? null,
    withPosts: health?.success_with_posts ?? null,
    trailZero: zeroText === null ? null : Number(zeroText),
  };
}

/**
 * 这组是不是「整组零帖」——跑完了、也报成功，但没有一只股票抓到帖子。
 *
 * 用 `success_with_posts === 0` 而不是 `零帖数 >= 股票数`：后者会把超时/失败
 * 的股票也算成「有帖子」，节假日一次超时就能让判定失真。
 * 判不了（老日志既无 crawl_health 也无零帖行）时返回 false，宁可漏报不误报。
 *
 * @param {ReturnType<typeof readPipelineRun>} run
 * @returns {boolean}
 */
export function isAllEmpty(run) {
  const total = run.total ?? run.stocks;
  if (!(total > 0)) return false;
  if (run.withPosts !== null) return run.withPosts === 0;
  // 老日志：没有 withPosts，只有「成功但零帖」的只数。要拿它和**成功数**比，
  // 不能和总数比 —— 超时/失败的股票本来就不会产出帖子，不该稀释这条判断。
  if (run.trailZero !== null) return run.success > 0 && run.trailZero >= run.success;
  return false;
}

function describe(run) {
  return `${run.total ?? run.stocks} 只里没有一只有帖子`;
}

/**
 * 把分组状态翻译成要报的问题（check.mjs 与 peak.mjs 共用同一套措辞）。
 *
 * @param {{name: string, run: ReturnType<typeof readPipelineRun>}[]} pipeStats
 *   今天**成功解析出收尾记录**的分组；没跑/没跑完的组不进这个数组（各自另有告警）。
 * @returns {string[]}
 */
export function zeroPostProblems(pipeStats) {
  // failed>0 的组已经由调用方的「有 N 只失败」报过了，别为同一个根因再报一条。
  const empty = pipeStats.filter(({ run }) => run.failed === 0 && isAllEmpty(run));
  if (empty.length === 0) return [];
  // 只有六组全部解析成功、且全部为空，才是「全站性故障/休市」。
  // 少几组多半是那几组没跑（它们另有「今天没更新/没有 [SUMMARY]」的告警），
  // 这时说成「六组全部零帖」会把排查方向带偏。
  if (pipeStats.length === GROUP_LOGS.length && empty.length === GROUP_LOGS.length) {
    return [
      `六组 pipeline 全部零帖（${pipeStats.map((g) => g.name).join('、')}）`
      + ' —— 要么站点侧全挂，要么今天休市；请人工确认是哪一种',
    ];
  }
  return empty.map(({ name, run }) => `${name} 整组零帖（${describe(run)}，SUMMARY 仍报 failed=${run.failed}）`);
}
