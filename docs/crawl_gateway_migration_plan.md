# Crawl Gateway 生产迁移执行方案

> 状态：待执行 | 前置设计：docs/crawl_gateway_design.md | 更新：2026-10-07

## 1. 现状盘点

### 已就绪

| 能力 | 实现位置 | 状态 |
|---|---|---|
| CLI 入口（run/stats/health） | crawl_gateway/cli.py | 可用 |
| SQLite 状态库（jobs/tasks/attempts/site_health/site_locks） | crawl_gateway/storage.py | 可用 |
| 频控、重试、熔断策略层 | rate_limiter.py / retry.py / health.py | 可用 |
| OpenCLI → nodriver 双 backend 降级 | adapters/xueqiu_backend.py | 可用 |
| 旧版产物兼容（data/{uid}/{aid}.md、index.json、history） | adapters/xueqiu.py:ArticleStore | 可用 |
| 测试 | 3 个测试文件，101 passed | 绿 |

### 缺口（按生产切换阻塞程度排序）

| # | 缺口 | 阻塞 | 说明 |
|---|---|---|---|
| G1 | .last_crawl_stats.json 未导出 | ✅ 已解决 | compatibility.export_last_crawl_stats 在 run --execute 后导出，字段与旧版对齐 |
| G2 | 无 shadow 对比机制 | ✅ 已解决 | Step 3 已实现 `CRAWL_ENGINE=shadow` 开关 + scripts/compare_crawl_outputs.py（当天文章集合 / 统计数值 / 基线与失败分类，退出码 0/1/2） |
| G3 | run_daily.sh 无引擎开关 | 阻塞 | 爬取步骤硬编码 crawler_nodriver.py --all --max 20 |
| G4 | 无 verify / retry-failed 命令 | ✅ 已解决 | Step 2 已实现 verify（预检，exit 1 表示不过）与 retry-failed（按 task 状态重放，不越熔断） |
| G5 | 登录态预检不在 gateway 流程 | 建议 | run_daily.sh 第 1 步 cookies.py --check 暂时保留在脚本层即可 |
| G6 | nodriver backend 是旧爬虫薄封装 | 非阻塞 | 每任务起停浏览器，效率低但行为正确；Phase 6 再原生会话化 |

## 2. 迁移原则

1. 数据面唯一写入方：切换后 gateway 是 data/ 唯一爬取写入方；旧入口只读不写，禁止双写业务结论（避免统计口径分裂）。
2. 影子先行：gateway 先写 data-gateway/ 影子目录全量跑，对比无误后才切生产 data/。
3. 一键回滚：CRAWL_ENGINE=legacy 立即回旧链路；legacy 入口保留 ≥ 2 周。
4. 每步一个 PR：独立验收、独立回滚，不合并大步骤。
5. 报告/发布链路零改动：generate_report.py、IMA、飞书步骤不动，只换爬取入口。

## 3. 分步执行

### Step 1：兼容导出 .last_crawl_stats.json（G1）— ✅ 已完成

改动：Orchestrator.run 结束后导出统计文件，字段与旧版逐一对齐：

```
{
  "date": "YYYY-MM-DD",
  "total_users": <任务数>,
  "successful": <success/no_update/duplicate 任务数>,
  "failed": <硬失败任务数>,
  "new_articles": <saved_articles 合计>
}
```

- 写入失败只记 warning，不阻断 job（与旧爬虫行为一致）。
- 单测：固定 attempts 序列 → 校验导出内容；写失败不影响 job 状态。

验收：fake backend 下，gateway 导出与旧爬虫同输入同构。

### Step 2：verify 与 retry-failed 命令（G4）— ✅ 已完成

- crawl-gateway verify --site xueqiu：不发真实请求。检查配置加载、SQLite 可写、数据目录存在、opencli doctor、熔断状态，输出 JSON；任一检查不过 exit 1。
- crawl-gateway retry-failed --job <id>：按 task 状态重放失败任务，尊重熔断与频控。
- 单测覆盖：verify 各失败分支（全通过 / 熔断打开 / opencli 不可用）、retry-failed 只重放失败任务、retry-failed 不越熔断。

两处口径决定（实现时核实代码后确定，勿回退）：

1. **失败任务的判定口径**：orchestrator 把 task 终态写成 `succeeded` / `failed` / `skipped` 三值，具体失败原因（`blocked_waf` 等 12 种 AttemptStatus）记在 `attempts.status` 上。因此 `GatewayStore.failed_tasks` 必须按 `tasks.status = 'failed'` 过滤，**不能**按 attempt 状态名过滤——后者查不到任何行，重放会静默变成空跑。
   - 已知边界：因熔断打开 / 频控拒绝而 `skipped` 的任务不在重放范围内。这类任务是被延后而非失败，若需要补跑要另立机制，别把它并进 `retry-failed`（否则会连带重放 dry-run 留下的 skipped 任务）。
2. **retry-failed 不写 `.last_crawl_stats.json`**：该文件是「当日整轮抓取」的聚合口径，`generate_report.py` 用它渲染「X/Y 账号成功」。重放只补跑少数失败账号，覆写会把日报从「9/10」篡改成看似完美的「1/1」。重放结果查 SQLite 审计（`stats` 命令）即可。
   - 代价：若某账号当日失败后被重放成功，日报仍显示原始成功率（偏保守偏低，不会虚高）。
   - 如需让日报反映重放后的最终成功率，需改为「按日期从 DB 汇总全部 job」派生统计文件，属独立改动，不放在 Step 2。

验收：verify 全绿是 shadow/生产运行的前置条件。

### Step 3：shadow 模式（G2）— ✅ 已完成

改动：run_daily.sh 增加引擎开关（默认 legacy）：

```
CRAWL_ENGINE=legacy    # 现状，旧链路写 data/
CRAWL_ENGINE=gateway   # Step 4 后启用，gateway 写 data/
CRAWL_ENGINE=shadow    # 旧链路照常写 data/；gateway 写 data-gateway/
```

shadow 分支执行：

```
python3 -m crawl_gateway --config config/sites.yaml \
  --db data-gateway/gateway.sqlite3 run --site xueqiu --purpose daily-shadow \
  --all-accounts --execute --data-dir data-gateway
```

新增 scripts/compare_crawl_outputs.py：对比两侧当天文章集合、.last_crawl_stats.json 数值、失败分类，输出 diff 报告到 logs/shadow_compare_YYYY-MM-DD.json（退出码 0 无差异 / 1 有差异 / 2 读取错误）。

验收：连续 3 个交易日 shadow 对比无差异告警（文章集合一致或差异可解释），gateway 健康分不下降。

三处实现时才暴露、计划文档原先没覆盖的口径（勿回退）：

1. **影子目录必须与 data/ 对齐已知状态**，两个要求缺一不可：
   - **必须在 legacy 开跑之前对齐**。顺序反了会让整轮对比退化成空转（2026-10-08 实测）：legacy 先跑并写下今天的新文章，之后才做镜像，那些新文章被一并拷给 gateway；gateway 于是认为今天无事可做（`duplicate`/`no_update`、`new_articles=0`），而对比读到的 "gateway 新增" 其实是**被拷进去的 legacy 数据**——文章层会显示完美一致，却什么都没验证。识别特征：两侧 history 文件的 **mtime 完全相同**（`cp -a` 保留时间戳），或 gateway 的 attempt 状态全是 `duplicate`/`no_update`。
   - **必须整体镜像** `data/`（排除 `daily_reports/`；`.last_crawl_stats.json` 由 gateway 自己写）。gateway 的已知集合来自三处：`index.json` + `history/<user>/*.json` + **`<user>/*.md`**；而 `index.json` 是**有损**的（曾因 OOM/SIGKILL 丢失，`scripts/rebuild_index.py` 为此而写），存在文章只在 `.md` 里、不在 index 里。只拷 index+history 会让 gateway 把这些旧文当新文重抓（2026-10-07 实测：4 篇去年/年初的老文被当成当天新文），对比结果全是假差异。
   - 两者都是**验证装置**的问题，不影响真切换：切到 `CRAWL_ENGINE=gateway` 后 gateway 直接用 `data/` 本身，`.md` 都在，已知集合与 legacy 一致。
   - 另外 `compare_crawl_outputs.py` 现在会在**两侧当天都无新增**时给出 `warnings`（走 stderr，也写进报告的 `warnings` 字段）：那种情况下的「无差异」不能算一次有效验证。
2. **对比「两侧各自新保存了什么」，不能直接比累计 index**。两侧 index.json 都是累计的，直接比全量集合会把历史差异算进来。因此当天新增一律取自 `history/<user>/<date>.json`（两侧都只在真存下新文章时才写）。另设基线校验：index 扣掉**两侧当天新增 id 的并集**后应一致；用并集而非各侧自己的集合，否则一侧只是漏记当天新文章时会被误报成「影子目录未对齐」。
3. **逐字段对比排除 `crawl_time` / `filepath`**。这两个字段只反映「何时爬、文件落在哪」，两侧不可能相同，纳入对比全是噪音。实际比对 `title` / `author` / `publish_time`。

已知代价：shadow 模式当天**站点访问量翻倍**（legacy + gateway 各跑一遍全量账号）。对 WAF 保护的站点这是额外风控暴露，也是本步骤要连续跑 3 天的成本。gateway 侧自带频控与熔断兜底。

run_daily.sh 的引擎分支行为：
- 切换/回滚的唯一开关：服务器上 `$PROJECT_DIR/.env` 里的 `CRAWL_ENGINE`。run_daily.sh 读该变量的位置**必须在 source .env 之后**，否则 .env 里写的值会被默认值盖掉、回滚失效（实现时踩到过，已修）。
- `legacy`（默认）：与现状完全一致。
- `gateway`：先 `verify` 再 `run`，失败即中止整个流水线——与旧链路爬取失败时的现状行为保持一致（断供窗口最多一个 cron 周期，见风险表）。
- `shadow`：legacy 照常写 `data/`（生产链路，失败即中止）；gateway 影子运行与对比**只告警不阻断**，保证日报不受影响。未知 `CRAWL_ENGINE` 值在取 cron 锁之前就报错退出，不留锁、不写生产日志。

### Step 4：切换生产（G3）

改动：run_daily.sh 爬取步骤按 CRAWL_ENGINE 分派：

```
if [ "$CRAWL_ENGINE" = "gateway" ]; then
    python3 -m crawl_gateway verify --site xueqiu >> "$LOG_FILE" 2>&1
    python3 -m crawl_gateway run --site xueqiu --purpose daily \
        --all-accounts --execute >> "$LOG_FILE" 2>&1
else
    python3 scripts/crawler_nodriver.py --all --max 20 >> "$LOG_FILE" 2>&1
fi
```

- cookies 预检保留不动。
- cron、.cron_running.lock、浏览器清理逻辑不动。
- 首切当天人工盯一次日报产物。

验收：连续 2 个交易日日报产物、IMA 发布、飞书推送正常；stats/health 结论与旧链路巡检一致。

回滚：crontab 或 .env 里设 CRAWL_ENGINE=legacy，下一个周期自动回旧链路。

### Step 5：收尾

- 生产稳定 1-2 周后，run_daily.sh 删 legacy 与 shadow 分支，crawler_nodriver.py 移入 archive/（或保留为 gateway nodriver backend 的实现库）。
- data-gateway/ 影子目录删除。
- README 与 docs/crawl_gateway_design.md §1.1 的「尚未落地」清单同步更新。

## 4. 风险与对策

| 风险 | 对策 |
|---|---|
| gateway 真实访问触发风控 | 站点熔断 3 次硬失败即停；shadow 阶段已验证频控参数 |
| 影子与生产文章集合有差异 | 对比脚本逐篇 diff，先解释再切换；不解释不切 |
| 切换日日报断供 | legacy 回滚一条环境变量；报告/发布步骤从未改动 |
| SQLite 锁竞争 | 已启用 WAL + busy_timeout + site_locks；cron 单实例窗口沿用 |
| 统计口径分裂 | 兼容文件由 gateway 单点导出，旧入口切走后不再写 |

## 5. 总验收

切换完成的标准：

1. run_daily.sh 生产路径走 crawl-gateway run --site xueqiu --purpose daily。
2. .last_crawl_stats.json 由 gateway 导出，generate_report.py 无感知。
3. 连续 2 个交易日日报正常发布，且 stats/health 可回答「为什么没数据」。
4. CRAWL_ENGINE=legacy 回滚演练成功一次。
5. legacy 入口归档，data/ 唯一爬取写入方是 gateway。
