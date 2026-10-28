#!/usr/bin/env node
// 雪球每日流程巡检 —— 核对本轮改动在**真实定时任务**里是否生效。
//
// 协议（宿主 preRunHook）:
//   exit 0 → 放行：发现问题，叫醒 agent 诊断
//   exit 2 → 跳过本轮：一切正常，不烧 token、不出声
//   其它非零 → fail-closed：脚本自身出错，宿主阻止本轮并记录
//
// 模式由触发时段自判（XQ_PRECHECK_MODE 可覆盖，便于手工自测）:
//   < 12 点  → morning  09:00：只看 08:00 crawler 那轮的产出
//   12–18 点 → health   14:00：只看 13:30 健康检查
//   >= 18 点 → eod      20:30：全天汇总，逐个核对产物
//
// hermes 各任务本来就会推飞书卡片，所以这里不重复报「任务跑没跑」，
// 只查卡片不会告诉你的具体信号 —— 也就是这轮改动的落点。

import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { homedir } from 'node:os';

import { readPipelineRun, isAllEmpty } from './pipeline-stats.mjs';

const now = new Date();
const mode = process.env.XQ_PRECHECK_MODE
  || (now.getHours() < 12 ? 'morning' : now.getHours() < 18 ? 'health' : 'eod');

const CRAWLER = process.env.XQ_CRAWLER_DIR || join(homedir(), 'code/claude_code/xueqiu-crawler');
const MONITOR = process.env.XQ_MONITOR_DIR || join(homedir(), 'code/claude_code/xueqiu-monitor');

const problems = [];
let complete = true;
const today = now.toLocaleDateString('sv-SE');
const dayOf = (d) => d.toLocaleDateString('sv-SE');

// 正文里剥掉头部元数据，只留正文本身，用来判「抓到了没」。
// 头部（作者/发布时间/原文链接/爬取时间/标题）占了 ~150 字符，不剥掉的话
// 「正文是空的」和「正文正常」在长度上几乎分不出来。
//
// ⚠️ 必须**锚定在行首**：不带 `^` 的 `爬取时间：` 会把正文里出现该字样的整行删掉 ——
// 「正则打在全文上」这类坑刚在影子的归一层踩过一次（那边被 review 抓出来）。
const METADATA_LINE = /^(?:>|\*爬取时间：)/;
// 每篇都有的「来源：雪球App，作者：…」样板行（后缀带作者和链接，故前缀匹配），
// 以及正文两侧的 `---` 分隔线 —— 都是存储格式的一部分，不是文章内容。
// ⚠️ `$` 锚的是整条正则的行尾：把签名行写成 `^来源：…$` 会因后缀永不匹配，
// 剥样板整个空转（PR #122 review 实测抓出来的）。
const BOILERPLATE_LINE = /^(?:来源：雪球App，作者：.*|---+)$/;

/**
 * 一篇 `.md` 里**文章自己的内容**：剥掉头部元数据、标题行、样板行之后剩下的。
 *
 * 判「抽取故障」只认这个，不认总长度（2026-10-16/20 两次误报的教训）：
 * - 短帖/图帖的正文就是标题本身，或只有一两句话 —— 长度很短但**有**内容，
 *   抽取没坏。而且现在 opencli 链路上「无内容根本不落盘」（crawler_nodriver
 *   的「跳过无内容」分支），存下来的短文就是作者真的只写了这么多。
 * - 真正的故障签名是剥完样板后**什么都不剩**（只剩存储格式）。
 */
function articleContent(raw) {
  return bodyText(raw)
    .split('\n')
    .filter((line) => {
      const t = line.trim();
      return t !== '' && !BOILERPLATE_LINE.test(t);
    })
    .join('\n')
    .trim();
}

/** 一篇 `.md` 里属于「正文」的部分（剥掉头部元数据与标题行）。 */
function bodyText(raw) {
  const lines = raw.split('\n');
  // 只丢**首行**的 `# 标题`，不在正文里全局丢 `#` 开头的行（正文可能自带小标题）。
  if (lines.length > 0 && lines[0].trimStart().startsWith('#')) lines.shift();
  return lines
    .filter((line) => {
      const t = line.trim();
      return t !== '' && !METADATA_LINE.test(line.trimStart());
    })
    .join('\n')
    .trim();
}

function emptyBodies() {
  const found = [];
  const historyDir = join(CRAWLER, 'data/history');
  if (!existsSync(historyDir)) return found;
  for (const user of readdirSync(historyDir)) {
    const p = join(historyDir, user, `${today}.json`);
    if (!existsSync(p)) continue;
    let rows;
    try {
      rows = JSON.parse(readFileSync(p, 'utf8'));
    } catch {
      continue; // 坏 JSON 不是内容问题，别在这里报
    }
    rows = Array.isArray(rows) ? rows : rows.articles || Object.values(rows);
    for (const row of rows) {
      if (!row || !row.article_id) continue;
      const md = join(CRAWLER, 'data', user, `${row.article_id}.md`);
      if (!existsSync(md)) continue; // 缺文件由「今天没抓够」那条覆盖，这里只管内容空
      const content = articleContent(readFileSync(md, 'utf8'));
      if (content.length === 0) {
        found.push(`${user}/${row.article_id}（正文只剩样板，0 字符内容）`);
      }
    }
  }
  return found;
}

// crawler：今天的日报有没有产出。
// #54 修的「无新增也产出最小日报」落在这里：以前 0 新增 return ""，
// 下游 publish/push 全报「日报文件不存在」，当天零交付。
function checkCrawler() {
  const statsPath = join(CRAWLER, 'data/.last_crawl_stats.json');
  try {
    const stats = JSON.parse(readFileSync(statsPath, 'utf8'));
    if (stats.date !== today) {
      problems.push(`crawler 今天没跑：stats.date=${stats.date}，期望 ${today}`);
    } else if (Number(stats.failed) > 0) {
      problems.push(`crawler ${stats.failed}/${stats.total_users} 个账号失败`);
    }
  } catch (err) {
    complete = false;
    problems.push(`读不到 .last_crawl_stats.json: ${err.message}`);
  }
  const reportPath = join(CRAWLER, `data/daily_reports/${today}.md`);
  if (!existsSync(reportPath)) {
    problems.push(`今天没有日报文件：${reportPath} —— 查 generate_report 的「无新增」分支`);
  }

  // 内容断言：stats 说成功 ≠ 真抓到了东西。
  // 这是本项目反复发作的一类静默失败 —— 风控页/抽取失效时正文为空，而 stats 仍报
  // successful=18、failed=0，日报也照样产出（只剩标题），下游全绿。
  // 「错误结果与正常结果长得一样」才是最贵的，所以这里不看 stats，直接看产物本身。
  const empty = emptyBodies();
  if (empty.length > 0) {
    problems.push(
      `今天有 ${empty.length} 篇正文只剩样板、没有内容（stats 不会报这个）：${empty.slice(0, 5).join('，')}`,
    );
  }
}

// 热榜贴：01:00 的 prepare job 负责抓列表 + 全文，08:00 的日报只读文件。
// 这条断言的意义和「正文是空的」一样 —— 01:00 失败时日报会**静默**少一块
// （降级为纯自选账号文章），没有任何下游报错。
function checkHotPosts() {
  // hot_posts.py 按 Asia/Shanghai 取日期，这里必须用同一时区。
  const hotDay = now.toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' });
  const p = join(CRAWLER, `data/hot_posts/${hotDay}/hot_top10.json`);
  if (!existsSync(p)) {
    problems.push(`今天没有热榜数据：${p} —— 01:00 的 prepare job 没跑成？`);
    return;
  }
  let doc;
  try {
    doc = JSON.parse(readFileSync(p, 'utf8'));
  } catch (err) {
    problems.push(`热榜文件损坏：${err.message}`);
    return;
  }
  const items = Array.isArray(doc.items) ? doc.items : [];
  if (items.length === 0) {
    problems.push('热榜 top10.json 里没有条目');
    return;
  }
  const withFull = items.filter((it) => it && it.content_source === 'full').length;
  if (withFull === 0) {
    problems.push(
      `热榜 ${items.length} 条全是首页摘要（正文没抓到），日报分析质量会打折`,
    );
  } else if (withFull * 2 < items.length) {
    problems.push(`热榜只有 ${withFull}/${items.length} 条拿到全文`);
  }
}

// monitor 健康检查。
// #46 的落点：source_failures 按「最近一轮 pipeline」窗口算；ok 分支的 detail
// 里必须带「窗口 ….」时间。注意 warn 分支的文案（N 次失败 → …）本来就没有
// 「窗口」二字 —— 那是真实失败，不是口径问题，别误报。
function checkHealth() {
  try {
    const out = execFileSync('python3', [join(MONITOR, 'scripts/health_check.py')], {
      encoding: 'utf8', timeout: 180000, cwd: MONITOR,
    });
    const rep = JSON.parse(out);
    if (rep.status !== 'ok') problems.push(`health_check status=${rep.status}（期望 ok）`);
    const sf = (rep.checks || []).find((c) => c.check === 'source_failures');
    if (sf && String(sf.detail).startsWith('读取失败')) {
      problems.push(`source_failures 读取失败：${sf.detail}（#46 修掉的 NameError 复发）`);
    } else if (sf && sf.status === 'ok' && !String(sf.detail).includes('窗口')) {
      problems.push(`source_failures 报 ok 却没有窗口时间，可能没走到新口径：${sf.detail}`);
    }
  } catch (err) {
    complete = false;
    problems.push(`health_check 执行失败: ${err.message}`);
  }
}

// 全天汇总：逐个核对所有定时任务的产物。
function checkEod() {
  checkCrawler();
  checkHotPosts();
  // 六个 pipeline 日志：g1–g5 每次 tee **覆盖**，主 job 的 pipeline.log 是跨天**追加**。
  // 「今天更新过 + 有 [SUMMARY]」= 今天跑完了；收尾记录一律取最后一次运行。
  const pipeStats = [];
  for (const name of ['pipeline.log', 'pipeline-g1.log', 'pipeline-g2.log',
                      'pipeline-g3.log', 'pipeline-g4.log', 'pipeline-g5.log']) {
    const p = join(MONITOR, 'logs', name);
    if (!existsSync(p)) { problems.push(`缺日志 ${name}`); continue; }
    if (dayOf(statSync(p).mtime) !== today) {
      problems.push(`${name} 今天没更新（最后 ${dayOf(statSync(p).mtime)}）`);
      continue;
    }
    const run = readPipelineRun(p);
    if (!run.summary) { problems.push(`${name} 今天跑了但没有 [SUMMARY]（可能中途失败）`); continue; }
    if (run.failed > 0) problems.push(`${name} 有 ${run.failed} 只失败：${run.summary}`);
    // 和上面「正文只剩样板」同一类：failed=0 只说明没抛异常。opencli 两个操作全挂时
    // 会静默回退 Playwright，回退也抓不到就成了整组零帖 —— 2026-10-28 的 g2/g4
    // 17 只股票整天没数据，[SUMMARY] 仍写 success=9/9 failed=0，巡检全绿。
    pipeStats.push({ name, run });
  }
  // 整组零帖：别的组有数据、这组全空是异常；六组全空则是一次汇总（不是静默，
  // 也不刷六条）—— 全站性故障正是最该叫醒人的那种。
  const empty = pipeStats.filter(({ run }) => isAllEmpty(run));
  const alive = pipeStats.length - empty.length;
  if (empty.length > 0 && alive > 0) {
    for (const { name, run } of empty) {
      problems.push(
        `${name} 整组零帖（${run.total ?? run.stocks} 只跑完却一只都没抓到帖子，`
        + `SUMMARY 仍报 failed=${run.failed}）`,
      );
    }
  } else if (empty.length > 0 && alive === 0) {
    problems.push(
      `六组 pipeline 全部零帖（${empty.map((g) => g.name).join('、')}）`
      + ' —— 要么站点侧全挂，要么今天休市；请人工确认是哪一种',
    );
  }
  const syncLog = join(MONITOR, 'logs/sync_watchlist.log');
  if (!existsSync(syncLog)) {
    problems.push('缺 logs/sync_watchlist.log（12:00 的 sync-watchlist 没跑过？）');
  } else if (dayOf(statSync(syncLog).mtime) !== today) {
    problems.push(`sync_watchlist.log 今天没更新（最后 ${dayOf(statSync(syncLog).mtime)}）`);
  }
}

if (mode === 'morning') {
  checkCrawler();
  checkHotPosts();
} else if (mode === 'health') checkHealth();
else checkEod();

if (complete) process.stdout.write('CINDY_PRECHECK_OK\n');

if (problems.length === 0) {
  console.log(`[每日流程巡检:${mode}] 全部正常`);
  process.exit(2);
}
console.log(`[每日流程巡检:${mode}] 发现问题：`);
for (const p of problems) console.log('  - ' + p);
process.exit(0);
