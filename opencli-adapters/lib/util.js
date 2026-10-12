/** 无依赖小工具。 */

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * 在 [min, max] 之间取随机值。
 *
 * 夹紧是刻意的：调用方可能只改了 min（例如把下限调到 20 秒）而 max 还是 12，
 * 若只做 Math.min/Math.max 交换，实际间隔会落在 12–20 秒 —— **下限被静默忽略**。
 * Python 版的写法是 `max_delay = max(min_delay, max_delay)`，这里对齐它：
 * 先夹紧，再取值。
 */
export function randomBetween(min, max) {
  const lo = Math.max(0, Math.min(min, max));
  const hi = Math.max(lo, Math.max(min, max));
  return lo + Math.random() * (hi - lo);
}

export const asNumber = (value, fallback = 0) => {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};
