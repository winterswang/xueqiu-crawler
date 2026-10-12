/**
 * 跨进程并发闸：限制「同时打同一个域名的请求数」。
 *
 * 为什么需要：限频只管**起始时刻的间隔**，管不住同时在飞的数量 —— 一个调用睡完
 * 6–12 秒才开始跑，而跑完要 10–20 秒，于是间隔再稳也可能同时有 2–3 路压在同一站点上；
 * 5 组 monitor pipeline 各自没有锁，单组超时就会与下一组叠加。风控看的是瞬时压力。
 *
 * 机制：**每个在飞调用一个槽文件**（不是 JSON 里存数组）——
 *     ~/.opencli/inflight/<key>/<token>.slot
 * `ls` 就能看谁在飞；独占创建即原子；崩溃只泄漏一个文件。临界区（清点 + 占位）用
 * 同一把 `.gate.lock` 互斥，复用 lib/lockfile.js 的原语。
 *
 * ## 观测模式（当前默认）
 *
 * `concurrency` 缺省或 0 = **只记录不拦**：照常占位（否则测不出真实并发）、照常释放，
 * 但不等待、不拒绝。目的是先拿到「真实并发分布」再拍上限 —— 拍脑袋定 1 会把吞吐压到
 * 最差，定 3 又可能等于没拦。
 *
 * ## 泄漏与回收
 *
 * `onAfterExecute` 在**成功与抛错两条路径**上都会触发（`execution.js` 的成功分支与
 * catch 分支都 emit），所以正常失败不会漏槽。**唯一漏的是进程被 KILL**（JS 收不到任何
 * 回调）—— 靠槽里的 `expires_at` 由后续调用顺手 sweep 回收。这不是假想：
 * `~/.opencli/dianping-throttle.json.lock` 就是一次真实残留。
 *
 * ## 与调用方超时的关系（启用闸门时必须一起改）
 *
 * 排队等待发生在 **opencli 子进程内部**，会被调用方 `subprocess.run(timeout=…)` 一并
 * 计时。最坏墙钟 = 限速 87s + 取锁 2s + 排队预算 + 执行 ~10s；调用方现在是 120s，
 * 所以**启用前**必须把 `scripts/opencli_extractor.py` 与
 * `xueqiu-monitor/src/detail_fetcher.py` 的超时抬到 ~150s。
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { DEBUG, envNumber, log } from './env.js';
import { acquireLock, newLockToken, releaseLock } from './lockfile.js';
import { OPENCLI_DIR } from './paths.js';
import { sleep } from './util.js';

// 槽的存活上限。必须 > 调用方最坏超时（120s）+ 余量，否则会误删**正在跑**的槽。
const DEFAULT_TTL_MS = 240_000;
// 排队预算：超了就 fail-open 放行并留痕。见文件头「与调用方超时的关系」。
const DEFAULT_WAIT_MS = 20_000;
const POLL_MS = 200;

export function inflightRoot() {
  const override = process.env.XUEQIU_OPENCLI_INFLIGHT_DIR;
  if (override) return path.resolve(override.replace(/^~(?=\/|$)/, os.homedir()));
  return path.join(OPENCLI_DIR, 'inflight');
}

const ttlMs = () => envNumber(['XUEQIU_OPENCLI_INFLIGHT_TTL_MS'], DEFAULT_TTL_MS);
const waitBudgetMs = () => envNumber(['XUEQIU_OPENCLI_CONCURRENCY_WAIT_MS'], DEFAULT_WAIT_MS);

function readSlots(dir) {
  let names = [];
  try {
    names = fs.readdirSync(dir);
  } catch {
    return [];
  }
  const now = Date.now();
  const live = [];
  for (const name of names) {
    if (!name.endsWith('.slot')) continue;
    const full = path.join(dir, name);
    let meta = null;
    try {
      meta = JSON.parse(fs.readFileSync(full, 'utf8'));
    } catch {
      meta = null;
    }
    const expiresAt = Number(meta?.expires_at) || 0;
    if (!expiresAt || expiresAt <= now) {
      // 过期 = 持有者大概率被 KILL 了（正常路径由 onAfterExecute 释放）。回收。
      try {
        fs.unlinkSync(full);
      } catch {
        /* 别人抢先删了 */
      }
      continue;
    }
    live.push({ name, meta });
  }
  return live;
}

/**
 * 占一个并发槽。
 *
 * @returns {Promise<{enforced:boolean, max:number|null, inflight:number|null,
 *                    acquired:boolean, token:string|null, waited_ms:number,
 *                    overflowed:boolean}>}
 */
export async function acquireInflight(key, policy, meta = {}) {
  const max = Math.trunc(Number(policy.concurrency) || 0);
  const enabled = policy.concurrency_enabled !== false && max > 0;
  const token = newLockToken();
  const dir = path.join(inflightRoot(), key);
  const gate = path.join(dir, '.gate.lock');
  const startedAt = Date.now();
  let inflight = null;
  let slotToken = null;
  let overflowed = false;

  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch (err) {
    log.warn(`concurrency[${key}]: 建不了槽目录（${err.message}），本轮放行`);
    return { enforced: enabled, max, inflight: null, acquired: false, token: null, waited_ms: 0, overflowed: false };
  }

  for (;;) {
    const lockToken = newLockToken();
    const { fd, failOpen } = await acquireLock(key, gate, lockToken);
    if (failOpen) {
      // 拿不到闸门锁：不阻塞调用，直接放行（与限速器同一套 fail-open 哲学）。
      return { enforced: enabled, max, inflight, acquired: false, token: null, waited_ms: Date.now() - startedAt, overflowed: true };
    }
    let live;
    try {
      live = readSlots(dir);
      if (!enabled || live.length < max) {
        slotToken = `${token}.slot`;
        // 观测模式下也要占位：不占就量不出「同时在飞几路」，这正是要先拿到的数据。
        const body = JSON.stringify({
          token: slotToken,
          pid: process.pid,
          ppid: process.ppid,
          started_at: startedAt,
          expires_at: Date.now() + ttlMs(),
          domain_key: key,
          enforced: enabled,
          source: meta.source ?? null,
          command: meta.command ?? null,
        });
        fs.writeFileSync(path.join(dir, slotToken), body);
        inflight = live.length + 1;
      } else {
        inflight = live.length;
      }
    } finally {
      releaseLock(fd, gate, lockToken);
    }

    if (slotToken) break;

    if (Date.now() - startedAt > waitBudgetMs()) {
      overflowed = true; // 超预算 → 放行，但留痕（日志里 overflowed: true）
      if (DEBUG) log.debug(`concurrency[${key}]: 排队超预算，放行（在飞 ${inflight}/${max}）`);
      break;
    }
    await sleep(POLL_MS + Math.random() * POLL_MS);
  }

  return {
    enforced: enabled,
    max: max || null,
    inflight,
    acquired: Boolean(slotToken),
    token: slotToken,
    waited_ms: Date.now() - startedAt,
    overflowed,
  };
}

/** 释放并发槽：只删自己那个（核对 token，避免误删被回收后别人建的同名槽）。 */
export function releaseInflight(key, token) {
  if (!token) return;
  const full = path.join(inflightRoot(), key, token);
  try {
    const meta = JSON.parse(fs.readFileSync(full, 'utf8'));
    if (meta?.token !== token) return;
    fs.unlinkSync(full);
  } catch {
    /* 已被 TTL 回收，或本就没建成 —— 都不是问题 */
  }
}

export { readSlots, ttlMs, waitBudgetMs };
