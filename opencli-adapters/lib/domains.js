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

/**
 * 把 hostname 映射成规范键：**按后缀逐级匹配**，匹配不到就原样用整个 hostname。
 *
 * 两个关键取舍：
 *
 * 1. **按后缀匹配而不是只查完整 hostname**：`api.xueqiu.com` 不在别名表里，靠
 *    逐级去最左标签才能命中 `xueqiu.com` 的别名。漏了这一步它就会得到键
 *    `xueqiu.com` —— 另开一个状态文件、且策略落回 `web` 弱档（**丢掉长停**），
 *    正是本文件开头强调「绝不能发生」的静默掉档。
 *
 * 2. **绝不用「末两段」猜注册域**。`www.sse.com.cn` / `stock.10jqka.com.cn` 这类
 *    多级公共后缀切出来都是 `com.cn` —— 一堆互不相关的站点被塞进同一个额度
 *    （互相排队），台账里的 `domain_key` 也变成 `com.cn` 这种误导性归因。宁可让
 *    子域各拿各的键（偏保守：拆开只会少限速，不会把无关站点绑在一起），
 *    已知「一个站点多个子域」的用 DOMAIN_RULES 显式归组（cninfo/hkexnews/sseinfo…）。
 */
function canonicalFor(hostname) {
  const labels = hostname.split('.');
  for (let i = 0; i < labels.length; i += 1) {
    const candidate = labels.slice(i).join('.');
    // 后缀匹配天然按标签对齐，所以 `notxueqiu.com` 不会命中 `xueqiu.com`。
    const aliased = CANONICAL_DOMAIN_KEY[candidate];
    if (aliased) return { key: aliased, matched: candidate };
    const ruled = DOMAIN_RULES.find((r) => r.re.test(candidate));
    if (ruled) return { key: ruled.key, matched: candidate };
  }
  return { key: hostname, matched: null };
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

  let hostname = String(SITE_DEFAULT_DOMAIN[site] ?? '').replace(/\.$/, '');
  let via = hostname ? 'site' : 'url';

  if (!hostname) {
    const url = urlFromArgs(args);
    if (!url) return fallback('site-key');
    try {
      // 去掉尾点：FQDN 形态（`https://xueqiu.com./1/1`）合法且真的出现过，
      // 不归一化就会绕过别名表 → 新状态文件 + 落回 web 弱档（丢掉长停）。
      hostname = new URL(url).hostname.toLowerCase().replace(/\.$/, '');
    } catch {
      // 有 URL 但解析不出 hostname（`https://`、`https://[bad`…）。
      return fallback('url-unparseable');
    }
    if (!hostname) return fallback('url-unparseable');
  }

  const { key } = canonicalFor(hostname);
  return { key, host: hostname, via, degraded: false };
}
