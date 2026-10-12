/**
 * 限速策略：内置表、用户配置、环境变量覆盖。
 *
 * 策略的键是**流量键**（现在是 opencli 站点键，按域名重构后是域名键）。
 * 未声明的键**不限速** —— 宁可漏，也不误伤本地命令（见 README）。
 */
import * as fs from 'node:fs';

import { CONFIG_PATH } from './paths.js';
import { envFlagOff, envFlagOn, envNumber, log } from './env.js';

// 6–12s 随机 + 每 30 次 45–75s 长停，照搬原 Python 实现，行为不回退。
export const XUEQIU_PACE = {
  min_delay_s: 6,
  max_delay_s: 12,
  long_pause_every: 30,
  long_pause_min_s: 45,
  long_pause_max_s: 75,
};

export const BUILTIN_SITES = {
  // 15s 固定间隔：2026-10 实测单次真实执行仅 ~3.6s，其余全是排队。
  xiaohongshu: { min_delay_s: 15, max_delay_s: 15 },
  xueqiu: XUEQIU_PACE,
  // `web` 是**通用**站（article.js 读任意网页正文，还有 opencli 自带的 `web read`），
  // 站点键不携带目标信息。雪球生产链路的**详情页**正是走 `opencli web article`，
  // 而它的站点键是 `web` —— 所以光给 `web` 一个策略还不够，得**按目标域名**分流：
  // 打 xueqiu.com 的走 xueqiu 的节奏与额度，其余按这里的温和节奏。
  //
  // 为什么不能整站按雪球节奏：`xueqiu-monitor` 也用这条命令抓公告页，域名是**混的**
  // （实测 announcements 表 xueqiu.com 4429 / cninfo.com.cn 1035 / hkexnews.hk 61 /
  // sseinfo.com 21，见 README）。整站套雪球节奏 = 把这些下载也拖成每 30 次停 45–75 秒。
  //
  // ⚠️ 下限必须 ≥6 秒：`scripts/schedule-checks/peak.mjs` 用「6 秒下限 ⇒ 一分钟不可能
  // 超过 10 次」做「限速器被绕过」的判据（PEAK_IMPOSSIBLE=10）。调到 6 秒以下会让
  // 合法的公告批量下载被误报成绕过。改这里之前先看那个脚本。
  web: { min_delay_s: 6, max_delay_s: 8 },
};

/** 键未覆盖字段的兜底。注意 enabled 的默认值只在键被声明时才有意义。 */
export const DEFAULT_POLICY = {
  enabled: true,
  min_delay_s: 3,
  max_delay_s: 8,
  long_pause_every: 0,
  long_pause_min_s: 0,
  long_pause_max_s: 0,
  // 不发站点请求的命令不该被限速。
  skip_commands: ['auth', 'doctor', 'help', 'list', 'version', 'login', 'logout'],
};

let cachedConfig;
export function loadConfig() {
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
  // 新增可选的 `domains`（按域名覆盖）—— 结构不动 `sites`，老配置原样继续生效。
  const userDomains =
    raw && typeof raw.domains === 'object' && raw.domains ? raw.domains : {};
  cachedConfig = {
    defaults: {
      ...DEFAULT_POLICY,
      ...(raw && typeof raw.defaults === 'object' ? raw.defaults : {}),
    },
    sites: { ...BUILTIN_SITES, ...named },
    domains: { ...userDomains },
    wildcard: wildcard ?? null,
  };
  return cachedConfig;
}

/** 显式声明（域名优先于站点键）。没声明返回 null。 */
function explicitPolicy(key) {
  const cfg = loadConfig();
  return cfg.domains[key] ?? cfg.sites[key] ?? null;
}

/**
 * 一次调用的策略查找链：**域名键 → 站点键 → 通配**。
 *
 * 站点键那一步不能省：`web article` 抓 cninfo 时域名键是 `cninfo.com.cn`（没有专门
 * 配置），必须落回 `sites.web` 的那档 6–8 秒 —— 否则就成了「未声明即不限速」，
 * 正是要避免的静默缺口。
 */
export function policyForCall(domainKey, siteKey) {
  for (const key of [domainKey, siteKey]) {
    if (!key) continue;
    const hit = explicitPolicy(key);
    if (hit) {
      const cfg = loadConfig();
      return { ...cfg.defaults, ...(cfg.wildcard ?? {}), ...hit };
    }
  }
  const cfg = loadConfig();
  return cfg.wildcard ? { ...cfg.defaults, ...cfg.wildcard } : null;
}

/**
 * 环境变量覆盖。
 *
 * `key` 是域名键（决定 `OPENCLI_RATELIMIT_<KEY>_*`），`siteKey` 是 opencli 站点键 ——
 * 两者都试，顺序 domain 优先。这样既支持新增的按域名调参
 * （`OPENCLI_RATELIMIT_CNINFO_COM_CN_MIN_DELAY_SECONDS`），也让既有的
 * `OPENCLI_RATELIMIT_WEB_*` / `XUEQIU_OPENCLI_*` 继续生效。
 */
export function applyEnv(key, base, siteKey = '') {
  let policy = base;
  if (envFlagOn(['OPENCLI_RATELIMIT_DISABLED'])) return { ...policy, enabled: false };
  if (key === 'xueqiu' || siteKey === 'xueqiu') {
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
  const upperOf = (k) => k.toUpperCase().replace(/[^A-Z0-9]/g, '_');
  const keys = [...new Set([key, siteKey].filter(Boolean))].map(upperOf);
  const names = (suffix) => [
    ...keys.map((k) => `OPENCLI_RATELIMIT_${k}_${suffix}`),
    `OPENCLI_RATELIMIT_${suffix}`,
  ];
  return {
    ...policy,
    min_delay_s: envNumber(names('MIN_DELAY_SECONDS'), policy.min_delay_s),
    max_delay_s: envNumber(names('MAX_DELAY_SECONDS'), policy.max_delay_s),
  };
}

/** 本次调用该不该限速。`--help` 是本地探测（不发请求），放过。 */
export function shouldThrottle(name, args, policy) {
  if (policy.enabled === false) return false;
  const skip = Array.isArray(policy.skip_commands) ? policy.skip_commands : [];
  if (skip.includes(name)) return false;
  if (args && (args.help === true || args.h === true)) return false;
  return true;
}
