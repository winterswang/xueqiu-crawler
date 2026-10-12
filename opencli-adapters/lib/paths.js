/**
 * opencli 状态目录与状态文件路径。
 *
 * 这里的所有路径都支持环境变量覆盖 —— 不是为了配置灵活，而是为了**能在不碰生产
 * 状态文件的前提下做验证**：插件本体装在真实 HOME 的符号链接里（改了 HOME 连插件
 * 都找不到），所以隔离只能靠覆盖路径，不能靠换 HOME。
 */
import * as os from 'node:os';
import * as path from 'node:path';

export const OPENCLI_DIR = path.join(os.homedir(), '.opencli');
export const CONFIG_PATH = path.join(OPENCLI_DIR, 'rate-limits.json');

/**
 * 状态文件路径：默认 `~/.opencli/<key>-throttle.json`。
 *
 * `key` 是「流量键」—— 现在是 opencli 站点键，按域名重构后是域名键（见 lib/domains.js）。
 * 名字刻意保持 `-throttle.json` 后缀不变：已有的 `xueqiu-throttle.json` /
 * `xiaohongshu-throttle.json` 要继续原地生效，换名等于把限速排期清零。
 *
 * 覆盖优先：`policy.state_path` > 站点专用环境变量。
 */
export function statePathFor(key, policy) {
  const override =
    key === 'xueqiu' ? process.env.XUEQIU_OPENCLI_THROTTLE_STATE : '';
  const chosen = policy.state_path || override;
  if (chosen) return path.resolve(String(chosen).replace(/^~(?=\/|$)/, os.homedir()));
  return path.join(OPENCLI_DIR, `${key}-throttle.json`);
}
