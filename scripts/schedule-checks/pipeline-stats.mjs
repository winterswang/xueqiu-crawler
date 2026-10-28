// pipeline 日志的收尾记录解析 —— check.mjs 与 peak.mjs 共用。
//
// 两种写法混在一起
// ----------------
// `pipeline.log`（13:00 主 job）是跨天**追加**的，同一天里能翻到前几天的
// `[SUMMARY]`；`pipeline-g1..g5.log` 是各条 cron 每次 tee **覆盖**的。
// 所以取收尾记录一律取**最后一次**运行 —— 用 `match`（第一次匹配）会让主 job
// 永远读到几天前的数字。2026-10-28 复现：pipeline.log 里 6 条 SUMMARY（10-23…10-28），
// 首条是 10-23 的，`failed=0` 和零帖数都成了陈年数据。
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

function lastMatch(text, re) {
  // matchAll 只接受带 g 的正则；调用方写 `/x/` 时在这里补上，免得每次都要记。
  const global = re.flags.includes('g') ? re : new RegExp(re.source, `${re.flags}g`);
  const all = [...text.matchAll(global)];
  return all.length ? all[all.length - 1][1] : null;
}

/**
 * 读一份 pipeline 日志里**最后一次运行**的收尾状态。
 *
 * @param {string} file 日志绝对路径。
 * @returns {{summary: string|null, stocks: number, failed: number,
 *   total: number|null, withPosts: number|null, coverage: number|null,
 *   status: string|null, trailZero: number|null}}
 *   `total`/`withPosts` 来自 `crawl_health`，缺失时为 null（老日志）。
 */
export function readPipelineRun(file) {
  const text = readFileSync(file, 'utf8');
  const lines = text.split('\n');

  // 从尾部往前找一个能解析的收尾 JSON —— 进程被打断时最后一行可能是半截。
  let done = null;
  for (let i = lines.length - 1; i >= 0 && done === null; i -= 1) {
    const at = lines[i].indexOf(DONE_MARK);
    if (at < 0) continue;
    try {
      done = JSON.parse(lines[i].slice(at + DONE_MARK.length).trim());
    } catch {
      done = null;
    }
  }

  const summary = lastMatch(text, /\[SUMMARY\] (.+)/);
  const health = done?.crawl_health ?? null;
  const zeroText = lastMatch(text, /成功但零帖: (\d+)/);

  return {
    summary,
    stocks: Number(/stocks=(\d+)/.exec(summary ?? '')?.[1] ?? 0),
    failed: Number(/failed=(\d+)/.exec(summary ?? '')?.[1] ?? 0),
    total: health?.total ?? null,
    withPosts: health?.success_with_posts ?? null,
    coverage: health?.posts_coverage ?? null,
    status: health?.status ?? null,
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
  if (run.trailZero !== null) return run.trailZero >= total;
  return false;
}
