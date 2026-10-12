/**
 * 插件侧请求台账：一行一次调用，覆盖**所有** hook 能看到的调用。
 *
 * ## 为什么要有它（和现有台账的分工）
 *
 * 现有 `~/.opencli/xueqiu-calls.jsonl` 由 **Python 调用方**写（`record_opencli_call`），
 * 它只知道「我发起了什么、拿到什么结局」。它**看不到**：
 *   · 手敲的 `opencli …`、其它脚本自己写的裸 subprocess；
 *   · **限速器自己的决策**（等了多久、长停命中没有、为什么被跳过）；
 *   · 即将加入的并发闸（同时在飞几路）。
 * 本次排查里最关键的两个数字（`throttle_wait_ms` 恒为 0）就是栽在这个盲区上。
 *
 * 所以分工定死，别再混：
 *   · **本文件（插件台账）= 传输层真相**：域名、限频等待、被跳过原因、并发、是否放行。
 *   · `xueqiu-calls.jsonl`（Python 台账）= 调用方意图与结局：`source`/`caller`、
 *     `expect_miss`、stderr 摘要、以及插件看不见的 `browser:*`。
 * 两者都能拿到 `ppid`——opencli 是无 shell 包装的 node 直启脚本，插件进程的 ppid
 * 就是调用方 Python 进程的 pid，可以据此 join。
 *
 * ## 硬约束（违反会出事）
 *
 * 1. **绝不能写 stdout/stderr**：调用方把 opencli 的 stdout 当 JSON 解析
 *    （`opencli_extractor` 的 `_strip_opencli_notice` 就是为此存在）。日志一律只落盘。
 * 2. **写失败一律吞掉、绝不上抛**：钩子里抛异常会被 opencli 的 `emitHook` 静默
 *    fail-open，**连限速一起失效**。日志坏掉不能影响请求，更不能影响限速决策。
 * 3. 单行 < 4KB：超长字段截断（O_APPEND 下单行追加是原子的）。
 */
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { OPENCLI_DIR } from './paths.js';

const MAX_LINE_BYTES = 4096;
const ROTATE_BYTES = 32 * 1024 * 1024; // 比 Python 台账那版大一点，减少轮转次数

function logPath() {
  const raw = process.env.OPENCLI_ADAPTER_LOG;
  if (raw === undefined) return path.join(OPENCLI_DIR, 'adapter-calls.jsonl');
  const value = String(raw).trim();
  if (value === '' || value.toLowerCase() === 'off' || value.toLowerCase() === 'disabled') {
    return null; // 显式关闭
  }
  return path.resolve(value.replace(/^~(?=\/|$)/, os.homedir()));
}

const truncate = (value, max) => {
  const s = String(value ?? '');
  return s.length > max ? `${s.slice(0, max)}…` : s;
};

/** 从继承来的环境变量里认调用方（钩子跑在 opencli 子进程，环境变量是继承的）。 */
function callerInfo() {
  const source = process.env.XUEQIU_CALL_SOURCE || '';
  if (!source) return { source: null, sourceKind: 'manual' };
  if (source.startsWith('xueqiu-crawler')) return { source, sourceKind: 'crawler' };
  if (source.startsWith('xueqiu-monitor')) return { source, sourceKind: 'monitor' };
  if (source.startsWith('xueqiu-analyzer')) return { source, sourceKind: 'analyzer' };
  return { source, sourceKind: 'other' };
}

/** 轮转：抢到独立的 rotlock 才动，抢不到就本次不轮转（继续 append）。 */
function maybeRotate(target) {
  let size = 0;
  try {
    size = fs.statSync(target).size;
  } catch {
    return; // 还不存在
  }
  if (size < ROTATE_BYTES) return;
  const rotLock = `${target}.rotlock`;
  let fd;
  try {
    fd = fs.openSync(rotLock, 'wx');
  } catch {
    return; // 别人在轮转，本次跳过
  }
  try {
    fs.renameSync(target, `${target}.1`); // rename 是原子的；重复轮转只留一个 .1
  } catch {
    /* 轮转失败不影响本次追加 */
  } finally {
    try {
      fs.closeSync(fd);
    } catch {
      /* 已关闭 */
    }
    try {
      fs.unlinkSync(rotLock);
    } catch {
      /* 已被回收 */
    }
  }
}

/**
 * 追加一行。**任何异常都吞掉** —— 见文件头硬约束 2。
 *
 * @param {object} record 由调用方组好（见 rate-limit.js 的 buildRecord）
 */
export function appendRecord(record) {
  const target = logPath();
  if (!target) return;
  let line;
  try {
    line = `${JSON.stringify(record)}\n`;
    if (Buffer.byteLength(line) > MAX_LINE_BYTES) {
      line = `${JSON.stringify({ ...record, truncated: true, error: truncate(record.error, 200) })}\n`;
      if (Buffer.byteLength(line) > MAX_LINE_BYTES) {
        line = `${JSON.stringify({
          schema: record.schema,
          ts: record.ts,
          layer: record.layer,
          truncated: true,
        })}\n`;
      }
    }
  } catch {
    return; // 循环引用之类：放弃这一行，不抛
  }
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    maybeRotate(target);
    const fd = fs.openSync(target, 'a'); // O_APPEND：单行写入跨进程原子
    try {
      fs.writeSync(fd, line);
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    /* 盘满 / 权限 / 只读 HOME：一律不影响请求与限速 */
  }
}

/** 去掉 opencli 自己塞进来的内部键（`__opencliOptionSources` 之类）。 */
export function stripInternalArgs(args) {
  if (!args || typeof args !== 'object') return null;
  const out = {};
  for (const [k, v] of Object.entries(args)) {
    if (k.startsWith('__')) continue;
    out[k] = v;
  }
  return out;
}

/**
 * 挑一个「最有辨识度」的目标写进 `target`。
 *
 * 优先 URL（`web article` 这类），其次常见定位参数 —— 否则 `xueqiu user-articles
 * --user_id 4111857140` 这种调用在台账里看不出打的是谁，排查时只能去翻 args。
 */
export function pickTarget(args) {
  const visible = stripInternalArgs(args) || {};
  if (typeof visible.url === 'string') return visible.url;
  for (const key of ['symbol', 'user_id', 'keyword', 'id', 'code', 'query']) {
    if (visible[key] != null && visible[key] !== '') return String(visible[key]);
  }
  for (const value of Object.values(visible)) {
    if (typeof value === 'string' && value) return value;
  }
  return '';
}

export { callerInfo, logPath, truncate };
