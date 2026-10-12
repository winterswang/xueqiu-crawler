/**
 * 频率控制：「预留下一个槽位，然后睡到它开始」。
 *
 * 跨进程：状态放文件（`<key>-throttle.json`），互斥用独占创建的锁文件。
 * 算法：持锁读改写 next_at → 释放锁 → 才 sleep —— 所以睡眠**不占锁**，长命令不会
 * 阻塞别的调用方。
 *
 * ⚠️ 睡眠发生在 **opencli 子进程内部**，会被调用方 `subprocess.run(timeout=…)` 的
 * 墙钟一并计时。调用方的超时必须大于「最大等待 + 单次真实执行」，否则长停一命中就
 * 必然被误杀（2026-07-12 实测：30s 上限下 2/18 账号失败）。
 *
 * **fail-open 的依据**（别在这里补 try/catch —— 那是重复的）：opencli 只在
 * `execution.js:206` 一处触发 onBeforeExecute，且经 `emitHook` 派发，而 `emitHook`
 * 对每个 handler 都套了 try/catch（「a failing hook never blocks command execution」）。
 * 所以本函数抛出的异常只会让 opencli 打一条 warn、本次不限速，命令照常执行。
 * 本模块内的 try 只有 `finally`、没有 `catch` 是**有意的**：状态写失败时也必须释放锁。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

import { DEBUG, log } from './env.js';
import { acquireLock, newLockToken, readState, releaseLock, writeState } from './lockfile.js';
import { statePathFor } from './paths.js';
import { asNumber, randomBetween, sleep } from './util.js';

/**
 * 预留下一个槽位并睡到它开始。
 *
 * @param {string} key    流量键（决定状态文件与策略来源）
 * @param {string} source 记进状态文件的 `last_source`（命令全名，便于排查）
 * @returns {Promise<{enabled:boolean, waited_s:number, reserved_s?:number, fail_open?:boolean, long_pause_s?:number}>}
 */
export async function acquireSlot(key, source, policy) {
  const statePath = statePathFor(key, policy);
  const lockPath = `${statePath}.lock`;
  let waited = 0;
  let reserved = 0;
  let longPause = 0;

  // 锁文件先于状态文件落地，所以目录得在这里建 —— writeState 里那次 mkdirSync
  // 在**拿到锁之后**才执行，太晚。漏了这一步的后果（2026-10-12 实测）：在
  // `~/.opencli` 还不存在的机器上（全新安装 / 新用户），openSync 报 ENOENT，
  // 被 acquireLock 当作「无法创建锁」直接放行 —— **限速静默失效、没有任何告警**。
  fs.mkdirSync(path.dirname(statePath), { recursive: true });

  const lockToken = newLockToken();
  const { fd, failOpen } = await acquireLock(key, lockPath, lockToken);
  if (failOpen) return { enabled: true, waited_s: 0, fail_open: true };

  try {
    const state = readState(statePath);
    const nowS = Date.now() / 1000;
    const nextAt = Math.max(nowS, asNumber(state.next_at));
    const calls = Math.trunc(asNumber(state.calls)) + 1;
    const every = Math.trunc(asNumber(policy.long_pause_every));
    longPause =
      every > 0 && calls % every === 0
        ? randomBetween(asNumber(policy.long_pause_min_s), asNumber(policy.long_pause_max_s))
        : 0;
    reserved =
      randomBetween(asNumber(policy.min_delay_s), asNumber(policy.max_delay_s)) + longPause;
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
    if (DEBUG) log.debug(`rate-limit[${key}]: 等待 ${waited.toFixed(1)}s（${source}）`);
    await sleep(waited * 1000);
  }
  return {
    enabled: true,
    waited_s: waited,
    reserved_s: reserved,
    long_pause_s: longPause,
  };
}
