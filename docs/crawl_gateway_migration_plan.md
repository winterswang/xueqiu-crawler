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
| G2 | 无 shadow 对比机制 | 阻塞 | 无法证明「同输入下 gateway 与旧链路产物一致」，不敢直接切生产 |
| G3 | run_daily.sh 无引擎开关 | 阻塞 | 爬取步骤硬编码 crawler_nodriver.py --all --max 20 |
| G4 | 无 verify / retry-failed 命令 | 强烈建议 | Phase 3 遗留；切换前至少要 verify 做预检 |
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

### Step 2：verify 与 retry-failed 命令（G4）

- crawl-gateway verify --site xueqiu：不发真实请求。检查配置加载、SQLite 可写、数据目录存在、opencli doctor、熔断状态，输出 JSON。
- crawl-gateway retry-failed --job <id>：按 task 状态重放失败任务，尊重熔断与频控。
- 单测覆盖：verify 各失败分支、retry-failed 不越熔断。

验收：verify 全绿是 shadow/生产运行的前置条件。

### Step 3：shadow 模式（G2）

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

新增 scripts/compare_crawl_outputs.py：对比两侧 index.json 文章集合、.last_crawl_stats.json 数值、失败分类，输出 diff 报告到 logs/shadow_compare_YYYY-MM-DD.json。

验收：连续 3 个交易日 shadow 对比无差异告警（文章集合一致或差异可解释），gateway 健康分不下降。

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
