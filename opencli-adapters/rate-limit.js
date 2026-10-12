/**
 * opencli 插件入口：跨进程站点限速。
 *
 * **本文件是插件目录里唯一注册钩子的模块**。拆出来的实现都在 `lib/` 下 ——
 * `lib/*` 不会被 opencli 的 discovery 扫到（它只平铺读顶层 `.js`），必须由这里
 * import 进来；同时这也保证钩子只注册一次。
 *
 * ⚠️ 本文件必须保留 `onBeforeExecute(` 字样：discovery 的模块识别正则
 * （`dist/src/discovery.js:26`）靠它判断这个文件是不是插件模块。删掉/改名会让
 * **整套限速静默消失**（import 失败只打一条 warn）。改完请务必：
 *     node --check rate-limit.js && opencli list 2>&1 | grep -i plugin
 *
 * ## 覆盖面（写清楚，别以为是「所有浏览器操作」）
 *
 * 钩子的唯一触发点是 `emitHook('onBeforeExecute', …)`，它在 `executeCommand` 里
 * （`dist/src/execution.js:206`），而 `executeCommand` 只被 `commanderAdapter.js`
 * 调用 —— 那是 `cli()` 注册的站点命令走的路径。因此：
 *
 * | 通道 | 是否被本模块覆盖 |
 * | --- | --- |
 * | 所有 `cli()` 注册命令（`xueqiu/*`、`xiaohongshu/search`、`web/article`） | ✅ |
 * | 手敲 `opencli …`、其它脚本的裸 `subprocess` | ✅（同样触发钩子） |
 * | `opencli doctor` / `list` / `--help` | ❌ 本地探测，不经 executeCommand |
 * | **`opencli browser …`** | ❌ 直接挂 commander、经 daemon 发指令，**不触发任何钩子**（上游 jackwener/opencli#2606） |
 * | urllib / curl / playwright / nodriver 直连 | ❌ 根本不经过 opencli |
 *
 * `browser open` 会真导航到目标站点，是唯一有实际风险的缺口 —— 详见 README。
 *
 * ## 键与额度
 *
 * 频率额度按**流量键**存：现在是 opencli 站点键；通用站 `web` 额外用 `HOST_RULES`
 * 把打 xueqiu.com 的调用归到 `xueqiu` 额度（否则列表调用与详情页抓取会各算各的
 * 窗口，实际请求密度翻倍，而风控墙看的是总量）。
 *
 * 配置：`~/.opencli/rate-limits.json`（每键不同策略；未声明的键**不限速**）。
 * 状态：`~/.opencli/<key>-throttle.json`，形状 {next_at, calls, last_source}。
 * 调试：`OPENCLI_RATELIMIT_DEBUG=1 OPENCLI_VERBOSE=1`（**两个都要设**：前者开本模块
 * 自己的日志，后者是 opencli logger 的开关，只设前者不会输出任何东西）。
 */
import { onBeforeExecute } from '@jackwener/opencli/registry';

import { log } from './lib/env.js';
import { applyEnv, policyFor, shouldThrottle } from './lib/policy.js';
import { acquireSlot } from './lib/ratelimit.js';

/**
 * 目标域名 → 归到哪个站点的策略与额度。
 *
 * `web article` 的站点键是 `web`，而它抓的可能是 xueqiu.com 的详情页（爬虫/监控的
 * 主路径，且**被 WAF 盯的就是这一类**），也可能是 cninfo/hkexnews 的公告页。
 * 风控看的是「打同一个站点的请求总量」，所以判定与额度都必须落到**域名**上。
 *
 * 没命中规则的 `web` 调用退回 `web` 自己的节奏（不是不限速）—— 宁可慢一点，也不要
 * 因为「URL 解析不出来」而静默变成完全不限速。
 */
const HOST_RULES = [{ host: /(^|\.)xueqiu\.com$/i, site: 'xueqiu' }];

/** 从参数里找目标 URL：通用命令的位置参数名不固定，先看常见的 `url`，再扫字符串值。 */
function urlFromArgs(args) {
  if (!args || typeof args !== 'object') return '';
  if (typeof args.url === 'string' && /^https?:\/\//i.test(args.url)) return args.url;
  for (const value of Object.values(args)) {
    if (typeof value === 'string' && /^https?:\/\//i.test(value)) return value;
  }
  return '';
}

/**
 * 通用命令（`web`）按**目标域名**归到具体站点；非通用命令返回 ''（用站点键本身）。
 *
 * 解析不出来时返回 ''、由调用方退回站点键的策略 —— 即「温和限速」而不是「不限速」。
 */
export function hostSiteFor(site, args) {
  if (site !== 'web') return '';
  const url = urlFromArgs(args);
  if (!url) return '';
  let hostname;
  try {
    hostname = new URL(url).hostname;
  } catch {
    return '';
  }
  const rule = HOST_RULES.find((r) => r.host.test(hostname));
  return rule ? rule.site : '';
}

export function installRateLimit() {
  onBeforeExecute(async (ctx) => {
    const full = String(ctx?.command ?? '');
    const slash = full.indexOf('/');
    if (slash <= 0) return; // "__startup__" 或无法解析的形态：放过
    const site = full.slice(0, slash);
    const name = full.slice(slash + 1);
    // 策略、环境变量覆盖、状态额度都按**归组后的键**算 —— 这样打 xueqiu.com 的
    // `web article` 与 `xueqiu user-articles` 共用一份额度和长停计数，
    // 且 `XUEQIU_OPENCLI_*` 那套开关对两者一致生效。
    const target = hostSiteFor(site, ctx?.args) || site;
    const base = policyFor(target);
    if (!base) return;
    const policy = applyEnv(target, base);
    if (!shouldThrottle(name, ctx?.args, policy)) return;
    await acquireSlot(target, full, policy);
  });
}

installRateLimit();

export { acquireSlot, applyEnv, policyFor, urlFromArgs };
export { log };
