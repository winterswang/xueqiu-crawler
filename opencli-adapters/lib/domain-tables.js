/**
 * 域名相关的映射表 —— 与解析逻辑分开放，是为了让「有人改了表」这件事在 diff 里一眼
 * 可见：这张表决定每次调用记在谁的账上，改动即改变限速归属。
 */

/** opencli 站点键 → 该站点的默认目标域名（URL 不在参数里的命令靠它兜底）。 */
export const SITE_DEFAULT_DOMAIN = {
  xueqiu: 'xueqiu.com',
  xiaohongshu: 'xiaohongshu.com',
  // web 是通用站，必须从参数里的 URL 解析（解析不出就退回站点键 'web'）。
};

/**
 * 域名 → 规范键（别名表）。
 *
 * ⚠️ **已有的键必须映射回自己**（`xueqiu.com` → `xueqiu`，不是 `'xueqiu.com'`）：
 * 状态文件名由键直接决定，换名等于把限速排期清零。
 */
export const CANONICAL_DOMAIN_KEY = {
  'xueqiu.com': 'xueqiu',
  'www.xueqiu.com': 'xueqiu',
  'stock.xueqiu.com': 'xueqiu',
  'stockn.xueqiu.com': 'xueqiu',
  'xiaohongshu.com': 'xiaohongshu',
  'www.xiaohongshu.com': 'xiaohongshu',
};

/**
 * 多级公共后缀的显式规则（末两段启发式对它们会切错）。
 * 命中的域名直接当键用 —— 各拿各的状态文件与额度。
 */
export const DOMAIN_RULES = [
  { re: /(^|\.)cninfo\.com\.cn$/i, key: 'cninfo.com.cn' },
  { re: /(^|\.)hkexnews\.hk$/i, key: 'hkexnews.hk' },
  { re: /(^|\.)sseinfo\.com$/i, key: 'sseinfo.com' },
  { re: /(^|\.)sec\.gov$/i, key: 'sec.gov' },
  { re: /(^|\.)bigmodel\.cn$/i, key: 'bigmodel.cn' },
];
