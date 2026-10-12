/**
 * 通用跨进程站点限速器 —— 以 opencli 插件钩子实现。
 *
 * 为什么放在这里：做在 adapter 层，**所有**调用方自动被覆盖，绕不过去 ——
 * FrontierAgent 的工具、xueqiu-crawler / xueqiu-monitor 的脚本、以及手敲
 * `opencli xueqiu ...` 都走同一条 `onBeforeExecute` 钩子。调用方不需要记得
 * 先调限速器（以前靠调用方自觉，结果有 4 条路径完全绕过限速）。
 *
 * 机制：opencli 在 `execution.js` 里执行任何命令前 emit `onBeforeExecute`
 * （dist/src/execution.js:206），钩子 API 由 `@jackwener/opencli/registry` 导出。
 * 本文件在模块顶层注册钩子；插件目录是**平铺急加载**的，且 discovery 的
 * 模块识别正则里显式包含 `onBeforeExecute(` —— 所以本文件会在任何命令执行前
 * 就被 import 并完成注册。
 *
 * 跨进程：opencli 每次调用都是新进程，所以状态放文件里，用独占创建的锁文件
 * 做互斥（Node 没有 flock）。算法是「预留即睡」：持锁读改写 next_at → 释放锁
 * → 才 sleep。长命令因此不会阻塞别的 crawler（与 Python 版同款算法）。
 *
 * 配置：`~/.opencli/rate-limits.json`，每站点不同策略；未在配置（含内置表）
 * 里声明的站点**不限速**（宁可漏也不能误伤本地命令）。
 * 状态：`~/.opencli/<site>-throttle.json`，形状 {next_at, calls, last_source}
 * —— 与既有 Python 实现同形，便于互读。
 * 调试：`OPENCLI_RATELIMIT_DEBUG=1` 打印每次等待。
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { onBeforeExecute } from '@jackwener/opencli/registry';
import { log } from '@jackwener/opencli/logger';

const OPENCLI_DIR = path.join(os.homedir(), '.opencli');
const CONFIG_PATH = path.join(OPENCLI_DIR, 'rate-limits.json');

/**
 * 内置策略：开箱即用，无需先写配置文件。
 * 数值是**策略决定**，改之前先看 README 的说明与实测依据。
 */
// 6–12s 随机 + 每 30 次 45–75s 长停，照搬原 Python 实现，行为不回退。
const XUEQIU_PACE = {
  min_delay_s: 6,
  max_delay_s: 12,
  long_pause_every: 30,
  long_pause_min_s: 45,
  long_pause_max_s: 75,
};

const BUILTIN_SITES = {
  // 15s 固定间隔：2026-10 实测单次真实执行仅 ~3.6s，其余全是排队。
  xiaohongshu: { min_delay_s: 15, max_delay_s: 15 },
  xueqiu: XUEQIU_PACE,
  // ⚠️ `web` 是**通用**站（article.js 读任意网页正文），雪球生产链路的**详情页**
  // 正是走 `opencli web article` —— 站点键是 `web`，xueqiu 的策略盖不到它。
  //
  // 2026-10-12：台账里 625 次 `web article` **全部**指向 xueqiu.com，一个非雪球
  // 调用都没有；而当天早上第二遍爬取被风控墙拦掉的，正是这一类详情页请求。
  // Python 版的 _should_throttle 原本专门覆盖了 `web article`（注释写着「切过去
  // 就等于把正文抓取的限速整个拿掉」），限速上移到插件层时漏了这一段。
  // 这里按雪球同款节奏补回来。
  //
  // 将来若真用 `web article` 抓非雪球站点，必须改成按目标域名判定 —— 否则会
  // 顺手把 cninfo/hkexnews 这类下载也拖成雪球的节奏。
  web: XUEQIU_PACE,
};

/** 站点未覆盖字段的兜底。注意 enabled 的默认值只在站点被声明时才有意义。 */
const DEFAULT_POLICY = {
  enabled: true,
  min_delay_s: 3,
  max_delay_s: 8,
  long_pause_every: 0,
  long_pause_min_s: 0,
  long_pause_max_s: 0,
  // 不发站点请求的命令不该被限速。
  skip_commands: ['auth', 'doctor', 'help', 'list', 'version', 'login', 'logout'],
};

const LOCK_STALE_MS = 60_000; // 持锁只为读改写状态，超过这个时间必然是崩溃残留
const LOCK_WAIT_MS = 30_000;
const LOCK_POLL_MS = 25;

const DEBUG = Boolean(process.env.OPENCLI_RATELIMIT_DEBUG);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function envFlagOff(names) {
  for (const name of names) {
    const value = String(process.env[name] ?? '').trim().toLowerCase();
    if (value === '0' || value === 'false' || value === 'off' || value === 'disabled') return true;
  }
  return false;
}

/**
 * 名字里带 DISABLED 的开关：**真值 = 关闭**。
 *
 * 不能复用 envFlagOff —— 它判的是「假值 = 关闭」，于是
 * `OPENCLI_RATELIMIT_DISABLED=1` 反而什么都不做、真正能关掉的是 `=0`，
 * 与名字和 README 正好相反（2026-10-12 review）。改按名字的语义解析：
 * 1/true/on/yes/enabled 以及任何其它非空非否定的值都算「开」。
 */
function envFlagOn(names) {
  for (const name of names) {
    const value = String(process.env[name] ?? '').trim().toLowerCase();
    if (value === '') continue;
    if (value === '0' || value === 'false' || value === 'off' || value === 'no') continue;
    return true;
  }
  return false;
}

function envNumber(names, fallback) {
  for (const name of names) {
    const raw = process.env[name];
    if (raw === undefined || raw === '') continue;
    const value = Number(raw);
    if (Number.isFinite(value) && value >= 0) return value;
  }
  return fallback;
}

const asNumber = (value, fallback = 0) => {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};

let cachedConfig;
function loadConfig() {
  if (cachedConfig !== undefined) return cachedConfig;
  let raw = {};
  try {
    raw = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
  } catch (err) {
    if (err.code !== 'ENOENT') {
      log.warn(`rate-limit: 忽略无法解析的 ${CONFIG_PATH}（${err.message}）`);
    }
  }
  const userSites = raw && typeof raw.sites === 'object' && raw.sites ? raw.sites : {};
  const { '*': wildcard, ...named } = userSites;
  cachedConfig = {
    defaults: { ...DEFAULT_POLICY, ...(raw && typeof raw.defaults === 'object' ? raw.defaults : {}) },
    sites: { ...BUILTIN_SITES, ...named },
    wildcard: wildcard ?? null,
  };
  return cachedConfig;
}

/** 未声明的站点返回 null（= 不限速）。 */
function policyFor(site) {
  const cfg = loadConfig();
  const sitePolicy = cfg.sites[site];
  if (!sitePolicy && !cfg.wildcard) return null;
  return { ...cfg.defaults, ...(cfg.wildcard ?? {}), ...(sitePolicy ?? {}) };
}

/** 环境变量覆盖：通用 OPENCLI_RATELIMIT_*，站点既有命名（xueqiu）继续兼容。 */
function applyEnv(site, base) {
  let policy = base;
  if (envFlagOn(['OPENCLI_RATELIMIT_DISABLED'])) return { ...policy, enabled: false };
  if (site === 'xueqiu') {
    if (envFlagOff(['XUEQIU_OPENCLI_THROTTLE'])) return { ...policy, enabled: false };
    policy = {
      ...policy,
      min_delay_s: envNumber(['XUEQIU_OPENCLI_MIN_DELAY_SECONDS'], policy.min_delay_s),
      max_delay_s: envNumber(['XUEQIU_OPENCLI_MAX_DELAY_SECONDS'], policy.max_delay_s),
      long_pause_every: envNumber(['XUEQIU_OPENCLI_LONG_PAUSE_EVERY'], policy.long_pause_every),
      long_pause_min_s: envNumber(['XUEQIU_OPENCLI_LONG_PAUSE_MIN_SECONDS'], policy.long_pause_min_s),
      long_pause_max_s: envNumber(['XUEQIU_OPENCLI_LONG_PAUSE_MAX_SECONDS'], policy.long_pause_max_s),
    };
  }
  const upper = site.toUpperCase().replace(/[^A-Z0-9]/g, '_');
  return {
    ...policy,
    min_delay_s: envNumber(
      [`OPENCLI_RATELIMIT_${upper}_MIN_DELAY_SECONDS`, 'OPENCLI_RATELIMIT_MIN_DELAY_SECONDS'],
      policy.min_delay_s,
    ),
    max_delay_s: envNumber(
      [`OPENCLI_RATELIMIT_${upper}_MAX_DELAY_SECONDS`, 'OPENCLI_RATELIMIT_MAX_DELAY_SECONDS'],
      policy.max_delay_s,
    ),
  };
}

function statePathFor(site, policy) {
  const override = site === 'xueqiu' ? process.env.XUEQIU_OPENCLI_THROTTLE_STATE : '';
  const chosen = policy.state_path || override;
  if (chosen) return path.resolve(String(chosen).replace(/^~(?=\/|$)/, os.homedir()));
  return path.join(OPENCLI_DIR, `${site}-throttle.json`);
}

function readState(statePath) {
  try {
    const state = JSON.parse(fs.readFileSync(statePath, 'utf-8'));
    if (state && typeof state === 'object') return state;
  } catch {
    /* 首次运行或文件损坏：从头开始，不影响调用 */
  }
  return { next_at: 0, calls: 0 };
}

function writeState(statePath, state) {
  // 先写临时文件再 rename —— 读方永远看不到半截 JSON。
  const tmp = `${statePath}.tmp`;
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  fs.writeFileSync(tmp, JSON.stringify(state));
  fs.renameSync(tmp, statePath);
}

const randomBetween = (min, max) => {
  const lo = Math.min(min, max);
  const hi = Math.max(min, max);
  return lo + Math.random() * (hi - lo);
};

/**
 * 预留下一个槽位并睡到它开始。
 *
 * **fail-open 的依据**（别在这里补 try/catch —— 那是重复的）：opencli 只在
 * `execution.js:206` 一处触发 onBeforeExecute，且经 `emitHook` 派发，而
 * `emitHook` 对每个 handler 都套了 try/catch（`dist/src/hooks.js`：
 * 「a failing hook never blocks command execution」）。所以本函数抛出的异常
 * （例如状态文件不可写）只会让 opencli 打一条 warn、本次不限速，命令照常执行。
 *
 * 本函数内的 try 只有 `finally`、没有 `catch` 是**有意的**：状态写失败时也必须
 * 释放锁文件，再让异常往上抛去做 fail-open。
 */
async function acquireSlot(site, source, policy) {
  const statePath = statePathFor(site, policy);
  const lockPath = `${statePath}.lock`;
  const deadline = Date.now() + LOCK_WAIT_MS;
  let waited = 0;
  let reserved = 0;

  // 锁文件先于状态文件落地，所以目录得在这里建 —— writeState 里那次 mkdirSync
  // 在**拿到锁之后**才执行，太晚。漏了这一步的后果（2026-10-12 实测）：在
  // `~/.opencli` 还不存在的机器上（全新安装 / 新用户），openSync 报 ENOENT，
  // 被下面的 catch 当作「无法创建锁」直接放行 —— **限速静默失效、没有任何告警**。
  fs.mkdirSync(path.dirname(statePath), { recursive: true });

  for (;;) {
    let fd;
    try {
      fd = fs.openSync(lockPath, 'wx'); // 独占创建 = 跨进程互斥
    } catch (err) {
      if (err.code !== 'EEXIST') {
        log.warn(`rate-limit[${site}]: 无法创建锁 ${lockPath}（${err.message}），本轮放行`);
        return { enabled: true, waited_s: 0 };
      }
      try {
        if (Date.now() - fs.statSync(lockPath).mtimeMs > LOCK_STALE_MS) {
          fs.unlinkSync(lockPath); // 崩溃残留，回收后重试
          continue;
        }
      } catch {
        /* 锁刚被另一方释放，直接重试 */
      }
      if (Date.now() > deadline) {
        log.warn(`rate-limit[${site}]: 等待状态锁超时，本轮放行（仅记录状态）`);
        return { enabled: true, waited_s: 0 };
      }
      await sleep(LOCK_POLL_MS + Math.random() * LOCK_POLL_MS);
      continue;
    }

    try {
      const state = readState(statePath);
      const nowS = Date.now() / 1000;
      const nextAt = Math.max(nowS, asNumber(state.next_at));
      const calls = Math.trunc(asNumber(state.calls)) + 1;
      const every = Math.trunc(asNumber(policy.long_pause_every));
      const longPause =
        every > 0 && calls % every === 0
          ? randomBetween(asNumber(policy.long_pause_min_s), asNumber(policy.long_pause_max_s))
          : 0;
      reserved = randomBetween(asNumber(policy.min_delay_s), asNumber(policy.max_delay_s)) + longPause;
      writeState(statePath, {
        next_at: nextAt + reserved,
        calls,
        last_source: source,
      });
      waited = Math.max(0, nextAt - nowS);
    } finally {
      fs.closeSync(fd);
      try {
        fs.unlinkSync(lockPath);
      } catch {
        /* 已被陈旧回收线程删掉 */
      }
    }
    break;
  }

  if (waited > 0) {
    if (DEBUG) log.debug(`rate-limit[${site}]: 等待 ${waited.toFixed(1)}s（${source}）`);
    await sleep(waited * 1000);
  }
  return { enabled: true, waited_s: waited, reserved_s: reserved };
}

function shouldThrottle(site, name, args, policy) {
  if (policy.enabled === false) return false;
  const skip = Array.isArray(policy.skip_commands) ? policy.skip_commands : [];
  if (skip.includes(name)) return false;
  if (args && (args.help === true || args.h === true)) return false; // 本地探测，不发请求
  return true;
}

function installRateLimit() {
  onBeforeExecute(async (ctx) => {
    const full = String(ctx?.command ?? '');
    const slash = full.indexOf('/');
    if (slash <= 0) return; // "__startup__" 或无法解析的形态：放过
    const site = full.slice(0, slash);
    const name = full.slice(slash + 1);
    const base = policyFor(site);
    if (!base) return;
    const policy = applyEnv(site, base);
    if (!shouldThrottle(site, name, ctx?.args, policy)) return;
    await acquireSlot(site, full, policy);
  });
}

installRateLimit();

export { acquireSlot, installRateLimit, policyFor, applyEnv };
