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
  // IP 字面量与单标签主机不能切：`1.2.3.4` 切出来是 `3.4` —— 那会开出一个
  // `3.4-throttle.json` 的垃圾状态文件，而且额度被凭空拆走。
  if (parts.length <= 2) return hostname;
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(hostname)) return hostname;
  return parts.slice(-2).join('.');
}

/**
 * 把 hostname 或「注册域」映射成规范键；**对两者都要查**。
 *
 * 只查完整 hostname 是不够的：`api.xueqiu.com` 不在别名表里，会先落到
 * 末两段 `xueqiu.com` —— 若不再查一次别名表，这个键就成了新字符串
 * （另开 `xueqiu.com-throttle.json`，且策略查找落回 `web` 的弱档、**丢掉长停**）。
 * 那正是「被 WAF 盯的详情页静默掉档」，必须阻止。
 */
function canonicalFor(hostname) {
  const candidates = [hostname];
  const registrable = registrableDomain(hostname);
  if (registrable !== hostname) candidates.push(registrable);

  for (const candidate of candidates) {
    const aliased = CANONICAL_DOMAIN_KEY[candidate];
    if (aliased) return { key: aliased, matched: candidate };
    const ruled = DOMAIN_RULES.find((r) => r.re.test(candidate));
    if (ruled) return { key: ruled.key, matched: candidate };
  }
  return { key: registrable, matched: null };
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
  // `web` 没有默认域名（它是通用站，目标在参数里）。任何解析不出目标域名的 web 调用
  // 都会退回站点键那一档 = 6–8s、**没有长停** —— 而 `web article` 正是抓雪球详情页的
  // 主通道，掉档就意味着被 WAF 盯的那类请求失去保护。所以 web 退档一律标 degraded，
  // 让调用方告警；非 web 站退回站点键是正常路径（它们的域名是固定的），不告警。
  const fallback = (via) => ({ key: site, host: '', via, degraded: site === 'web' });

  let hostname = SITE_DEFAULT_DOMAIN[site] ?? '';
  let via = hostname ? 'site' : 'url';

  if (!hostname) {
    const url = urlFromArgs(args);
    if (!url) return fallback('site-key');
    try {
      hostname = new URL(url).hostname.toLowerCase();
    } catch {
      // 有 URL 但解析不出 hostname（`https://`、`https://[bad`…）。
      return fallback('url-unparseable');
    }
    if (!hostname) return fallback('url-unparseable');
  }

  const { key } = canonicalFor(hostname);
  return { key, host: hostname, via, degraded: false };
}
