/**
 * 域名解析：把一次 opencli 调用归到**哪个域名**的额度上。
 *
 * 为什么以域名为主键：风控看的是「打同一个域名的请求总量」，而 opencli 的站点键
 * 只是实现细节 —— `xueqiu/user-articles`（列表）与 `web/article`（详情页，通用站）
 * 打的是同一个 xueqiu.com，必须共用一份额度；反过来，同样走 `web/article` 的
 * cninfo/hkexnews 公告页不该被算进雪球的账。
 *
 * 迁移兼容（**关键**）：别名表把已知域名映射回**现有的键**，于是
 * `xueqiu-throttle.json` / `xiaohongshu-throttle.json` 与 `rate-limits.json` 里的
 * `sites.xueqiu` / `sites.xiaohongshu` 全部原地继续生效。若把 xueqiu 映射成
 * `'xueqiu.com'`，状态文件会换名 → **限速排期清零 → 立刻一串请求打出去**。
 * 新增域名才用注册域当键（各拿各的状态文件）。
 */
import { SITE_DEFAULT_DOMAIN, CANONICAL_DOMAIN_KEY, DOMAIN_RULES } from './domain-tables.js';

export { SITE_DEFAULT_DOMAIN, CANONICAL_DOMAIN_KEY, DOMAIN_RULES };

/** 从参数里找目标 URL：通用命令的位置参数名不固定，先看常见的 `url`，再扫字符串值。 */
export function urlFromArgs(args) {
  if (!args || typeof args !== 'object') return '';
  if (typeof args.url === 'string' && /^https?:\/\//i.test(args.url)) return args.url;
  for (const value of Object.values(args)) {
    if (typeof value === 'string' && /^https?:\/\//i.test(value)) return value;
  }
  return '';
}

/** 末两段启发式（`a.b.example.com` → `example.com`）。只作为兜底。 */
function registrableDomain(hostname) {
  const parts = hostname.split('.').filter(Boolean);
  return parts.length <= 2 ? hostname : parts.slice(-2).join('.');
}

/**
 * 解析一次调用该记在哪把「域名键」下。
 *
 * 返回 `{ key, host, via, degraded }`：
 *   · `key`       流量键（决定策略、环境变量覆盖、状态文件名）
 *   · `host`      实际解析出的 hostname（进日志，便于发现启发式猜错）
 *   · `via`       怎么来的：'site'（站点默认域名）/ 'url'（从参数解析）/ 'site-key'（没域名，退回站点键）
 *   · `degraded`  true = 参数里有 http(s) URL 但解析不出 hostname —— 调用方应当告警，
 *                 否则「被 WAF 盯的详情页」会静默掉到弱档
 */
export function resolveDomainKey(site, args) {
  const fallback = { key: site, host: '', via: 'site-key', degraded: false };

  let hostname = SITE_DEFAULT_DOMAIN[site] ?? '';
  let via = hostname ? 'site' : 'url';

  if (!hostname) {
    const url = urlFromArgs(args);
    if (!url) return fallback;
    try {
      hostname = new URL(url).hostname.toLowerCase();
    } catch {
      // 有 URL 但解析不出 hostname（`https://`、`https://[bad`…）——
      // 旧实现这里只在「URL 字符串不存在」时告警，于是这一支是静默降档的。
      return { ...fallback, degraded: true };
    }
    if (!hostname) return { ...fallback, degraded: true };
  }

  const aliased = CANONICAL_DOMAIN_KEY[hostname];
  if (aliased) return { key: aliased, host: hostname, via, degraded: false };

  const ruled = DOMAIN_RULES.find((r) => r.re.test(hostname));
  if (ruled) return { key: ruled.key, host: hostname, via, degraded: false };

  return { key: registrableDomain(hostname), host: hostname, via, degraded: false };
}
