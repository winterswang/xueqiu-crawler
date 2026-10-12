/**
 * 环境变量解析原语。
 *
 * 两个开关助手（envFlagOn / envFlagOff）的**否定值集合必须逐字一致**，否则同一个值
 * 在两个助手里含义相反：`=disabled` 在 envFlagOff 里是「关」，漏在 envFlagOn 里就
 * 变成「开」（2026-10-12 review 踩过）。
 */
import { log } from '@jackwener/opencli/logger';

const NEGATIVE_VALUES = new Set(['0', 'false', 'off', 'no', 'disabled']);

/** 名字里带 DISABLED 的开关：**真值 = 关闭**。 */
export function envFlagOn(names) {
  for (const name of names) {
    const value = String(process.env[name] ?? '').trim().toLowerCase();
    if (value === '') continue;
    if (NEGATIVE_VALUES.has(value)) continue;
    return true;
  }
  return false;
}

/** 名字里带 ENABLED / THROTTLE 的开关：**假值 = 关闭**。 */
export function envFlagOff(names) {
  for (const name of names) {
    const value = String(process.env[name] ?? '').trim().toLowerCase();
    if (NEGATIVE_VALUES.has(value)) return true;
  }
  return false;
}

export function envNumber(names, fallback) {
  for (const name of names) {
    const raw = process.env[name];
    if (raw === undefined || raw === '') continue;
    const value = Number(raw);
    if (Number.isFinite(value) && value >= 0) return value;
  }
  return fallback;
}

// 用 envFlagOn 而不是 Boolean(process.env.X)：后者把 `=0` / `=false` 也当成「开」
// —— 用户设 `OPENCLI_RATELIMIT_DEBUG=0` 想关掉，反而打出全部等待日志。
//
// 注意：光设这个开关是**看不到输出**的，`log.debug` 实际走 opencli logger 的
// `verbose`，后者只在 `OPENCLI_VERBOSE` 下才写。要调试就两个都设。
export const DEBUG = envFlagOn(['OPENCLI_RATELIMIT_DEBUG']);

export { log };
