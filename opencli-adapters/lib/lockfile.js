/**
 * 跨进程互斥锁与状态文件读写（Node 没有 flock）。
 *
 * 锁 = 独占创建（`open(..., 'wx')`）+ 陈旧回收 + 所有权凭证。凭证那一条不能省：
 * 若本进程在临界区里被挂起超过 LOCK_STALE_MS（笔记本睡眠 / SIGSTOP），别人会回收
 * 我们的锁并建一把新的；我们恢复后若无脑 unlink，删掉的就是**别人的**锁，第三个
 * 进程随即能挤进临界区做并发读改写。
 */
import * as fs from 'node:fs';
import * as path from 'node:path';

import { log } from './env.js';
import { sleep } from './util.js';

export const LOCK_STALE_MS = 60_000; // 持锁只为读改写状态，超过这个时间必然是崩溃残留

// 等锁的上限，**算在调用方的 subprocess 超时预算里**（它与预留等待是相加的，
// 见 scripts/opencli_extractor.py 的 OPENCLI_CALL_TIMEOUT_SECONDS）。临界区只是一次
// 几十 KB 的 JSON 读改写、亚毫秒级，30 秒这种量级纯属照搬「真实锁」的直觉，会把
// 最坏等待抬到 30+87 ≈ 117 秒、反过来逼调用方把超时抬得更高。2 秒足够跨过
// 轮询间隔（25–50ms）和一次进程调度；超时就 fail-open 放行。
export const LOCK_WAIT_MS = 2_000;
export const LOCK_POLL_MS = 25;

export function readState(statePath) {
  try {
    const state = JSON.parse(fs.readFileSync(statePath, 'utf-8'));
    if (state && typeof state === 'object') return state;
  } catch {
    /* 首次运行或文件损坏：从头开始，不影响调用 */
  }
  return { next_at: 0, calls: 0 };
}

export function writeState(statePath, state) {
  // 先写临时文件再 rename —— 读方永远看不到半截 JSON。
  //
  // 临时名带 pid：Python 版用的是 `${statePath}.tmp`（同一个名字），若同时开两个
  // 实现（`XUEQIU_OPENCLI_THROTTLE=1` 的回退档），两边会互相 rename 对方写到一半的
  // 临时文件，读方解析失败后 readState 会静默回落成 {next_at:0,calls:0}、把限速
  // 排期整个丢掉。
  const tmp = `${statePath}.${process.pid}.tmp`;
  fs.mkdirSync(path.dirname(statePath), { recursive: true });
  fs.writeFileSync(tmp, JSON.stringify(state));
  fs.renameSync(tmp, statePath);
}

export function newLockToken() {
  return `${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * 取锁。成功返回 `{ fd }`；需要 fail-open 放过本次调用时返回 `{ failOpen: true }`
 * （锁被别人占着超过 LOCK_WAIT_MS、或锁文件根本建不出来）。
 */
export async function acquireLock(label, lockPath, lockToken) {
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
        log.warn(`rate-limit[${label}]: 无法创建锁 ${lockPath}（${err.message}），本轮放行`);
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
      log.warn(`rate-limit[${label}]: 等待状态锁超时，本次不限速放行`);
      return { failOpen: true };
    }
    await sleep(LOCK_POLL_MS + Math.random() * LOCK_POLL_MS);
  }
}

/** 释放锁：**只删自己的**，靠 lockToken 核对所有权（见文件头注释）。 */
export function releaseLock(fd, lockPath, lockToken) {
  try {
    fs.closeSync(fd);
  } catch {
    /* 已关闭 */
  }
  try {
    const owner = fs.readFileSync(lockPath, 'utf8');
    // 空文件按「自己的」处理：凭证写失败时的兜底，否则锁会一直留到陈旧回收。
    if (owner === '' || owner === lockToken) fs.unlinkSync(lockPath);
  } catch {
    /* 已被陈旧回收线程删掉 */
  }
}
