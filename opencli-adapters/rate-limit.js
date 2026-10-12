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
 * 频率额度按**域名**存（见 lib/domains.js）：`xueqiu/*` 与打 xueqiu.com 的
 * `web/article` 共用一份额度（否则列表调用与详情页抓取各算各的窗口，实际请求密度
 * 翻倍，而风控墙看的是总量）；cninfo/hkexnews 这类公告域名各拿各的额度，
 * 不会跟雪球互相拖累。已有的键（xueqiu / xiaohongshu）由别名表映射回原值，
 * 所以状态文件与配置原地继续生效。
 *
 * 配置：`~/.opencli/rate-limits.json`（每键不同策略；未声明的键**不限速**）。
 * 状态：`~/.opencli/<key>-throttle.json`，形状 {next_at, calls, last_source}。
 * 调试：`OPENCLI_RATELIMIT_DEBUG=1 OPENCLI_VERBOSE=1`（**两个都要设**：前者开本模块
 * 自己的日志，后者是 opencli logger 的开关，只设前者不会输出任何东西）。
 */
import { onAfterExecute, onBeforeExecute } from '@jackwener/opencli/registry';

import { log } from './lib/env.js';
import { resolveDomainKey } from './lib/domains.js';
import { applyEnv, policyForCall, shouldThrottle } from './lib/policy.js';
import { acquireInflight, releaseInflight } from './lib/concurrency.js';
import { acquireSlot } from './lib/ratelimit.js';
import { appendRecord, callerInfo, pickTarget, stripInternalArgs, truncate } from './lib/requestlog.js';

/** 谁、何时、打哪条命令 —— 与调用方身份（靠继承来的环境变量认，不需要调用方配合）。 */
function callIdentity(ctx, guard) {
  const finishedAt = Number(ctx.finishedAt) || Date.now();
  const startedAt = Number(guard.startedAt) || Number(ctx.startedAt) || finishedAt;
  const { source, sourceKind } = callerInfo();
  return {
    finishedAt,
    fields: {
      schema: 2,
      layer: 'plugin',
      ts: new Date(finishedAt).toISOString(),
      started_at: new Date(startedAt).toISOString(),
      finished_at: new Date(finishedAt).toISOString(),
      duration_ms: Math.max(0, finishedAt - startedAt),
      source,
      source_kind: sourceKind,
      call_id: process.env.XUEQIU_OPENCLI_CALL_ID || null,
      // ppid = 调用方进程 pid（opencli 是无 shell 包装的 node 直启脚本），
      // 可与 `xueqiu-calls.jsonl` 的 pid 对齐。
      ppid: process.ppid,
      pid: process.pid,
      site: guard.site ?? null,
      name: guard.name ?? null,
      operation: guard.name ?? null,
      // opencli 只给 "site/name"、不给 argv；完整参数在 args 里，target 是其中最有辨识度的那个。
      command: ctx.command ?? null,
      args: safeArgsJson(guard.args),
      domain_key: guard.key ?? null,
      host: guard.host ?? null,
      domain_via: guard.via ?? null,
      target: truncate(guard.target || pickTarget(guard.args), 300) || null,
    },
  };
}

function safeArgsJson(args) {
  try {
    const visible = stripInternalArgs(args);
    return visible && Object.keys(visible).length
      ? truncate(JSON.stringify(visible), 600)
      : null;
  } catch {
    return null; // 循环引用之类
  }
}

/** 限速与并发闸的决策 —— 这两块**只有插件层知道**，正是 Python 台账的盲区。 */
function guardFields(guard) {
  const rate = guard.rate || {};
  const conc = guard.concurrency || {};
  const ms = (seconds) => (seconds == null ? null : Math.round(seconds * 1000));
  return {
    throttled: guard.throttled ?? null,
    skip_reason: guard.skipReason ?? null,
    rate_waited_ms: ms(rate.waited_s),
    rate_reserved_ms: ms(rate.reserved_s),
    long_pause_ms: ms(rate.long_pause_s),
    rate_error: guard.rateError ?? null,
    rate_fail_open: rate.fail_open ?? (guard.rateError ? true : null),
    // concurrency_max 为 null = 观测模式（只看不拦）。
    concurrency_max: conc.max ?? null,
    // 配了 `concurrency` 但 `concurrency_enabled: false` 时闸门仍是观测态 ——
    // 只凭 max 会被读成「已生效」（review 指出）。这个字段才是权威。
    concurrency_enforced: conc.enforced ?? null,
    inflight_at_acquire: conc.inflight ?? null,
    acquired_slot: conc.acquired ?? null,
    concurrency_waited_ms: conc.waited_ms ?? null,
    // overflowed 专指「排队超预算」；拿不到闸门锁是另一回事，单列字段别混。
    overflowed: conc.overflowed ?? null,
    gate_fail_open: conc.gate_fail_open ?? null,
  };
}

/** 结局。err.code 是 opencli 的 CliError 码（如 BLOCKED_WAF），比字符串匹配可靠。 */
function outcomeFields(ctx) {
  const err = ctx.error;
  return {
    ok: !err,
    error_code: err?.code ?? null,
    error: err ? truncate(err.message ?? String(err), 500) : null,
  };
}

/** 把钩子 ctx 上挂着的信息 + 结局组装成台账的一行（见 lib/requestlog.js）。 */
function buildRecord(ctx) {
  const guard = ctx.__xqGuard || {};
  const { fields } = callIdentity(ctx, guard);
  return { ...fields, ...guardFields(guard), ...outcomeFields(ctx) };
}

export function installRateLimit() {
  onBeforeExecute(async (ctx) => {
    const full = String(ctx?.command ?? '');
    const slash = full.indexOf('/');
    if (slash <= 0) return; // "__startup__" 或无法解析的形态：放过
    const site = full.slice(0, slash);
    const name = full.slice(slash + 1);

    // 先解析「这次打的是哪个域名」—— 策略、环境变量覆盖、状态额度全部按它算。
    const { key, host, via, degraded } = resolveDomainKey(site, ctx?.args);
    if (degraded) {
      // `web` 调用没能解析出目标域名（没有 URL，或 URL 解析失败）。此时会退回站点键
      // 那一档（6–8s，**没有长停**）—— 而 `web article` 正是抓雪球详情页的主通道，
      // 掉档就意味着被 WAF 盯的那类请求失去保护。让这件事可见，否则零线索。
      log.warn(`rate-limit: web 调用解析不出目标域名（${full}），按其站点档限速`);
    }

    // 挂在 ctx 上带给 onAfterExecute —— opencli 明确支持（同一个 hookCtx 对象喂两个钩子，
    // 且 HookContext 带索引签名，注释写着「Plugins can attach arbitrary data here」）。
    ctx.__xqGuard = { startedAt: Date.now(), site, name, args: ctx?.args, key, host, via, target: '' };

    const base = policyForCall(key, site);
    if (!base) {
      ctx.__xqGuard.skipReason = 'no_policy';
      ctx.__xqGuard.throttled = false;
      return;
    }
    const policy = applyEnv(key, base, site);
    if (!shouldThrottle(name, ctx?.args, policy)) {
      ctx.__xqGuard.skipReason =
        policy.enabled === false ? 'disabled' : ctx?.args?.help || ctx?.args?.h ? 'help' : 'skip_command';
      ctx.__xqGuard.throttled = false;
      return;
    }
    ctx.__xqGuard.throttled = true;
    try {
      // 顺序刻意如此：限速的 sleep 不该占着并发名额（否则 max=1 时睡的人占着槽，
      // 并发闸就退化成纯串行器了）。槽代表「真的在飞」。
      ctx.__xqGuard.rate = await acquireSlot(key, full, policy);
    } catch (err) {
      // 限速器自己抛了（状态文件建不出来之类）：异常会被 opencli 的 emitHook 兜住、
      // 命令照常执行，但**本次实际是不限速的**。必须记下来 —— 否则台账里会出现
      // 「throttled: true 而等待全为 null」的行，正是这套台账要消除的那种误导读法。
      ctx.__xqGuard.rateError = err?.message ?? String(err);
      throw err;
    }
    ctx.__xqGuard.concurrency = await acquireInflight(key, policy, {
      source: callerInfo().source,
      command: full,
    });
  });

  // 一行一次调用：成功与抛错两条路径都会走到（execution.js 的成功分支与 catch 分支
  // 都 emit 了这个钩子）。**只在 before 钩子登记过的调用上写**，别把 `__startup__`
  // 之类也记进来。
  onAfterExecute(async (ctx) => {
    const guard = ctx?.__xqGuard;
    if (!guard) return;
    // 先释放并发槽再写台账：日志写失败也必须把名额还回去。
    releaseInflight(guard.key, guard.concurrency?.token ?? null);
    appendRecord(buildRecord(ctx));
  });
}

installRateLimit();

export { acquireSlot, applyEnv, policyForCall, resolveDomainKey, buildRecord };
export { log };
