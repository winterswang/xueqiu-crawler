/**
 * 雪球代码归一化：6 位数字 → 带市场前缀（`SH` / `SZ` / `BJ`）。
 *
 * 为什么单独成模块：这段逻辑原先在 `news.js` 与 `stock-notices.js` 里各写了一份
 * 嵌套三元，**两边都错**且错得一致，而错误是静默的（抓到错的页面 → 列表空、
 * 没有任何报错），所以必须能被测试钉住。
 *
 * 2026-10-12 实测原实现：
 *   `830799`（北交所 8 开头）→ `SZ830799` ✗   `920001` → `SH920001` ✗
 *   （原表达式是 `'69'→SH / '03802'→SZ / 其余→BJ`，而 `'03802'` 里**含 8**，
 *    于是 8 开头落进了 SZ；`9` 开头又无条件判 SH，把 920 段吃掉了。）
 *
 * 规则（按交易所实际代码段）：
 *   · `920xxx` → BJ —— **北交所 2024 起的新号段，必须在「9 开头」之前判**
 *   · `6xxxxx` / `9xxxxx` → SH —— 沪主板·科创(688)·沪B(900)
 *   · `0xxxxx` / `2xxxxx` / `3xxxxx` → SZ —— 深主板·深B(200)·创业板(300)
 *   · `5xxxxx` → SH、`1xxxxx` → SZ —— 沪/深基金与 ETF（原实现把它们判成 BJ）
 *   · 其余（`4xxxxx` / `8xxxxx` 等）→ BJ —— 北交所
 */

/** 沪市：6 开头（主板/科创）、9 开头（沪B）、5 开头（基金/ETF）。 */
const SH_HEADS = '695';
/** 深市：0 开头（主板）、2 开头（深B）、3 开头（创业板）、1 开头（基金/ETF）。 */
const SZ_HEADS = '0231';

/**
 * @param {string} symbol 已 trim / 大写 / 去掉前导 `$` 的代码
 * @returns {string} 带市场前缀的代码；不是 6 位纯数字时原样返回（港股/美股代码等）
 */
export function normalizeXueqiuSymbol(symbol) {
  if (!/^\d{6}$/.test(symbol)) return symbol;
  if (symbol.startsWith('920')) return `BJ${symbol}`;
  const head = symbol[0];
  if (SH_HEADS.includes(head)) return `SH${symbol}`;
  if (SZ_HEADS.includes(head)) return `SZ${symbol}`;
  return `BJ${symbol}`;
}
