/**
 * 通用跨进程站点限速器 —— 以 opencli 插件钩子实现。
 *
 * 为什么放在这里：做在 adapter 层，**所有走 registry 的调用方**自动被覆盖 ——
 * FrontierAgent 的工具、xueqiu-crawler / xueqiu-monitor 的脚本、以及手敲
 * `opencli xueqiu ...` 都走同一条 `onBeforeExecute` 钩子。调用方不需要记得
 * 先调限速器（以前靠调用方自觉，结果有 4 条路径完全绕过限速）。
 *
 * ⚠️ **例外，务必知道**：`opencli browser ...` 子命令**不经过这条钩子**，因此
 * **完全没有被限速**。钩子的唯一触发点是 `emitHook('onBeforeExecute', …)`，它在
 * `executeCommand` 里（dist/src/execution.js:206），而 `executeCommand` 只被
 * `commanderAdapter.js:107` 调用 —— 那是 `cli()` 注册的站点命令走的路径。
 * `browser` 命令是直接挂在 commander 上、经 daemon-client 发指令的，根本不经过
 * `executeCommand`，所以 hook 看不到它（2026-10-12 核实）。
 * 影响面：`opencli browser open` 会**导航到真实站点**，Python 版的
 * `_should_throttle` 曾经专门覆盖它（注释「browser open … 必须限速」）；现在
 * Python 侧默认关闭，于是这条路径落空。当前生产链路不受影响（`web article`
 * 已注册，回退路径 `_attempt_via_browser` 只在该命令未注册时才走），但别以为
 * 「钩子覆盖了一切」。真要补，只能在调用方做，或在 opencli 里给 browser 也发钩子。
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
 * 状态：`~/.opencli/<site>-throttle.json`，形状
 * {next_at, calls, last_source} —— 与既有 Python 实现同形，便于互读。
 * 额度按**站点**存，而 `hostSiteFor` 会把打 xueqiu.com 的 `web` 调用重归到
 * site=`xueqiu` —— 于是 `web article` 抓详情页与 `xueqiu/` 的列表调用自然共用
 * 同一份额度（xueqiu-throttle.json），不会各算各的窗口。
 * 调试：`OPENCLI_RATELIMIT_DEBUG=1 OPENCLI_VERBOSE=1` 打印每次等待。
 * **两个都要设**：前者打开本模块自己的日志，后者是 opencli logger 的开关
 * （`log.debug` 直接转调 `verbose`，而 `verbose` 只在 `OPENCLI_VERBOSE` 下才写），
 * 只设前者不会输出任何东西 —— 2026-10-12 核实。
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
  // `web` 是**通用**站（article.js 读任意网页正文，还有 opencli 自带的 `web read`），
  // 站点键不携带目标信息。雪球生产链路的**详情页**正是走 `opencli web article`，
  // 而它的站点键是 `web` —— 所以光给 `web` 一个策略还不够，得**按目标域名**分流：
  // 打 xueqiu.com 的走 xueqiu 的节奏与额度（见 HOST_RULES），其余按这里的温和节奏。
  //
  // 为什么不能整站按雪球节奏：`xueqiu-monitor` 也用这条命令抓公告页，域名是**混的**
  // （实测 announcements 表 xueqiu.com 4429 / cninfo.com.cn 1035 / hkexnews.hk 61 /
  // sseinfo.com 21，见 README）。整站套雪球节奏 = 把这些下载也拖成每 30 次停 45–75 秒。
  //
  // 3–8s 比 Python 版更宽松（它按**动词**匹配，对所有 `web article` 一律套雪球节奏），
  // 所以这里是放宽而不是收紧；被 WAF 盯的那部分由 HOST_RULES 单独覆盖。
  web: { min_delay_s: 3, max_delay_s: 8 },
};

/**
 * 目标域名 → 归到哪个站点的策略与额度。
 *
 * 为什么需要：`web article` 的站点键是 `web`，而它抓的可能是 xueqiu.com 的详情页
 * （爬虫/监控的主路径，且**被 WAF 盯的就是这一类**），也可能是 cninfo/hkexnews 的
 * 公告页。风控看的是「打同一个站点的请求总量」，所以判定与额度都必须落到**域名**上：
 * 打 xueqiu.com 的与 `xueqiu user-articles` 共用同一个 6–12s 额度与长停计数
 * （`hostSiteFor` 重归到 site=`xueqiu`，状态文件名由 site 决定），别的域名不跟着算。
 *
 * 没命中规则的 `web` 调用退回 `web` 自己的温和节奏（不是不限速）—— 宁可慢一点，
 * 也不要因为「URL 解析不出来」而静默变成完全不限速。
 */
const HOST_RULES = [{ host: /(^|\.)xueqiu\.com$/i, site: 'xueqiu' }];

// 注意：额度共用**不是**靠一张「站点→组」的映射表实现的 —— `hostSiteFor` 已经把
// 打 xueqiu.com 的 `web` 调用重新归到 site='xueqiu'，而状态文件名由 site 直接决定
// （见 statePathFor）。所以这里不要再加一层 group 映射：多一层就没有哪一层是
// load-bearing 的了，后来人会照着注释把 cninfo 之类的域名也并进雪球额度
// （2026-10-12 review 指出先前那版 STATE_GROUP 已经是恒等映射、纯死代码）。

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
// 等锁的上限，**算在调用方的 subprocess 超时预算里**（它与预留等待是相加的，
// 见 scripts/opencli_extractor.py 的 OPENCLI_CALL_TIMEOUT_SECONDS）。临界区只是一次
// 几十 KB 的 JSON 读改写、亚毫秒级，30 秒这种量级纯属照搬「真实锁」的直觉，会把
// 最坏等待抬到 30+87 ≈ 117 秒、反过来逼调用方把超时抬得更高。2 秒足够跨过
// 轮询间隔（25–50ms）和一次进程调度；超时就 fail-open 放行。
const LOCK_WAIT_MS = 2_000;
const LOCK_POLL_MS = 25;

// 用 envFlagOn 而不是 Boolean(process.env.X)：后者把 `=0` / `=false` 也当成「开」
// —— 用户设 `OPENCLI_RATELIMIT_DEBUG=0` 想关掉，反而打出全部等待日志
// （2026-10-12 review）。
//
// 注意：光设这个开关是**看不到输出**的，`log.debug` 实际走 opencli logger 的
// `verbose`，后者只在 `OPENCLI_VERBOSE` 下才写。要调试就两个都设。
const DEBUG = envFlagOn(['OPENCLI_RATELIMIT_DEBUG']);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function envFlagOff(names) {
  for (const name of names) {
    const value = String(process.env[name] ?? '').trim().toLowerCase();
    // 否定值集合必须与 envFlagOn 的那套**互补**，否则 `=no` 在一个助手里算「关」、
    // 在另一个里算「开」（2026-10-12 review）。
    if (
      value === '0' ||
      value === 'false' ||
      value === 'off' ||
      value === 'no' ||
      value === 'disabled'
    ) {
      return true;
    }
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
    // 否定值集合必须与 envFlagOff 的那套**逐字一致**，否则同一个值在两个助手里
    // 含义相反（例：`=disabled` 在 envFlagOff 里是「关」，漏在这里就变成「开」）。
    if (
      value === '0' ||
      value === 'false' ||
      value === 'off' ||
      value === 'no' ||
      value === 'disabled'
    ) {
      continue;
    }
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
  const override =
    site === 'xueqiu' ? process.env.XUEQIU_OPENCLI_THROTTLE_STATE : '';
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
  //
  // 临时名带 pid：Python 版用的是 `${statePath}.tmp`（同一个名字），若同时开两个
  // 实现（`XUEQIU_OPENCLI_THROTTLE=1` 的回退档），两边会互相 rename 对方写到一半的
  // 临时文件，读方解析失败后 readState 会静默回落成 {next_at:0,calls:0}、把限速
  // 排期整个丢掉（2026-10-12 review）。
  const tmp = `${statePath}.${process.pid}.tmp`;
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
 * 取状态锁：独占创建 = 跨进程互斥（Node 没有 flock）。
 *
 * 成功返回 `{ fd }`；需要 fail-open 放过本次调用时返回 `{ failOpen: true }`
 * （锁被别人占着超过 LOCK_WAIT_MS、或锁文件根本建不出来）。
 *
 * 陈旧锁回收放在这里：持锁超过 LOCK_STALE_MS 说明是崩溃残留 —— 但注意它与
 * `lockToken` 的配合，见 releaseLock。
 */
async function acquireLock(site, lockPath, lockToken) {
  const deadline = Date.now() + LOCK_WAIT_MS;
  for (;;) {
    try {
      const fd = fs.openSync(lockPath, 'wx');
      try {
        fs.writeSync(fd, lockToken);
      } catch {
        /* 写不进凭证不影响互斥；解锁时按「空文件 = 自己的」兜底 */
      }
      return { fd };
    } catch (err) {
      if (err.code !== 'EEXIST') {
        log.warn(`rate-limit[${site}]: 无法创建锁 ${lockPath}（${err.message}），本轮放行`);
        return { failOpen: true };
      }
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
      // 文案别再说「仅记录状态」—— 这里既没等到、也没写状态，本次就是**不限速**放行。
      log.warn(`rate-limit[${site}]: 等待状态锁超时，本次不限速放行`);
      return { failOpen: true };
    }
    await sleep(LOCK_POLL_MS + Math.random() * LOCK_POLL_MS);
  }
}

/**
 * 释放状态锁：**只删自己的**，靠 lockToken 核对所有权。
 *
 * 不核对会误删别人的活锁 —— 若本进程在临界区里被挂起超过 LOCK_STALE_MS
 * （笔记本睡眠 / SIGSTOP），别人会回收我们的锁并建一把新的；我们恢复后若无脑
 * unlink，删掉的就是**别人的**锁，第三个进程随即能挤进临界区做并发读改写
 * （2026-10-12 review）。空文件按「自己的」处理：凭证写失败时的兜底，
 * 否则锁会一直留到陈旧回收。
 */
function releaseLock(fd, lockPath, lockToken) {
  try {
    fs.closeSync(fd);
  } catch {
    /* 已关闭 */
  }
  try {
    const owner = fs.readFileSync(lockPath, 'utf8');
    if (owner === '' || owner === lockToken) fs.unlinkSync(lockPath);
  } catch {
    /* 已被陈旧回收线程删掉 */
  }
}

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
  let waited = 0;
  let reserved = 0;

  // 锁文件先于状态文件落地，所以目录得在这里建 —— writeState 里那次 mkdirSync
  // 在**拿到锁之后**才执行，太晚。漏了这一步的后果（2026-10-12 实测）：在
  // `~/.opencli` 还不存在的机器上（全新安装 / 新用户），openSync 报 ENOENT，
  // 被 acquireLock 当作「无法创建锁」直接放行 —— **限速静默失效、没有任何告警**。
  fs.mkdirSync(path.dirname(statePath), { recursive: true });

  const lockToken = `${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const { fd, failOpen } = await acquireLock(site, lockPath, lockToken);
  if (failOpen) return { enabled: true, waited_s: 0 };

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
    releaseLock(fd, lockPath, lockToken);
  }

  if (waited > 0) {
    if (DEBUG) log.debug(`rate-limit[${site}]: 等待 ${waited.toFixed(1)}s（${source}）`);
    await sleep(waited * 1000);
  }
  return { enabled: true, waited_s: waited, reserved_s: reserved };
}

function shouldThrottle(name, args, policy) {
  if (policy.enabled === false) return false;
  const skip = Array.isArray(policy.skip_commands) ? policy.skip_commands : [];
  if (skip.includes(name)) return false;
  if (args && (args.help === true || args.h === true)) return false; // 本地探测，不发请求
  return true;
}

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
 * 这一点是刻意的：宁可慢，也不能因为参数形态变了就静默退化成完全不限速。
 */
function hostSiteFor(site, args) {
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

function installRateLimit() {
  onBeforeExecute(async (ctx) => {
    const full = String(ctx?.command ?? '');
    const slash = full.indexOf('/');
    if (slash <= 0) return; // "__startup__" 或无法解析的形态：放过
    const site = full.slice(0, slash);
    const name = full.slice(slash + 1);
    // 策略、环境变量覆盖、状态额度都按**归组后的站点**算 —— 这样打 xueqiu.com 的
    // `web article` 与 `xueqiu user-articles` 共用一份额度和长停计数，
    // 且 `XUEQIU_OPENCLI_*` 那套开关对两者一致生效。
    //
    // 更彻底的做法是让 opencli 在 hookCtx 里直接带目标域名（或让调用方改用站点专属
    // 命令），那样就不必从任意 args 里反推域名、也不必维护 HOST_RULES。这属于
    // opencli 侧/调用方侧的改动，见下面那条 warn 的前提。
    if (site === 'web' && !urlFromArgs(ctx?.args)) {
      // 反推不出发域名 → 退回 `web` 自身那档（3–8s，**没有长停**）。让这件事可见：
      // 否则 args 形态一变（opencli 改位置参数名、URL 挪进嵌套选项…），被 WAF 盯的
      // xueqiu 详情页就会**静默**掉到弱档，且没有任何线索（2026-10-12 review）。
      log.warn(`rate-limit: web 调用解析不出目标 URL（${full}），按其自身节奏限速`);
    }
    const target = hostSiteFor(site, ctx?.args) || site;
    const base = policyFor(target);
    if (!base) return;
    const policy = applyEnv(target, base);
    if (!shouldThrottle(name, ctx?.args, policy)) return;
    await acquireSlot(target, full, policy);
  });
}

installRateLimit();

export { acquireSlot, installRateLimit, policyFor, applyEnv, hostSiteFor };
