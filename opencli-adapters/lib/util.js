/** 无依赖小工具。 */

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * 在 [min, max] 之间取随机值，**下限永远不被突破**。
 *
 * 调用方可能只调了下限（例如 `XUEQIU_OPENCLI_MIN_DELAY_SECONDS=20`）而 max 还是 12。
 * 此时不能交换上下界 —— 那会给出 12–20 秒，**下限被静默忽略**，比不设还危险
 * （你以为设了 20 秒的地板，实际发出去的是 12 秒）。对齐 Python 版的语义：
 * `max_delay = max(min_delay, max_delay)`，即 min>max 时收敛成 min。
 *
 * 2026-10-12 review：上一版写成 `lo = min(min,max)`，min=20/max=12 时得到 12–20 ——
 * 注释声称的语义与实际不符。
 */
export function randomBetween(min, max) {
  const lo = Math.max(0, min);
  const hi = Math.max(lo, max); // min > max 时收敛为 min
  return lo + Math.random() * (hi - lo);
}

export const asNumber = (value, fallback = 0) => {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};
