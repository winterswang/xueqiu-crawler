# 统一爬取访问层（Crawl Gateway）设计

## 1. 背景与目标

当前雪球日报链路已经具备 OpenCLI 优先、nodriver 兜底、WAF 识别和基础延迟，但访问策略分散在 `scripts/crawler_nodriver.py` 与 `scripts/run_daily.sh` 中。其他工具如果直接调用 OpenCLI 或自行启动浏览器，会绕过现有频控和状态记录，带来三类问题：

- 多个任务同时访问同一网站，触发风控或互相抢占浏览器会话。
- 失败、冷却、重试和健康状态没有统一事实来源，巡检难以判断“无数据”是正常还是故障。
- 新增小红书等站点时，需要重新实现频控、熔断、统计和降级逻辑。

Crawl Gateway 的目标是提供一个所有工具共同经过的本地抓取访问层：

1. **统一入口**：日报任务、人工重跑、其他 agent 和后续站点抓取都通过同一 CLI/API 访问目标网站。
2. **统一治理**：集中管理频控、并发、浏览器会话、失败重试、熔断和冷却。
3. **站点可插拔**：雪球、小红书等站点共用编排与治理能力，只替换站点适配器与配置。
4. **可观测**：每次尝试都有结构化结果、耗时、失败原因、降级路径和健康影响。
5. **渐进迁移**：保持现有数据文件和日报输出兼容，避免一次性重写生产链路。

### 1.1 当前实现边界（2026-10-06）

已落地：

- 状态模型、配置校验、频控、重试、健康分与熔断的纯策略层。
- SQLite 状态库：jobs/tasks/attempts/site_health/site_locks。
- 站点级互斥租约、结构化 stats、跨进程频控种子与熔断窗口恢复。
- python -m crawl_gateway 的 run --dry-run、stats、health 命令。
- 雪球 OpenCLI adapter、独立浏览器 session、三类去重来源与旧版
  Markdown/index/history 产物兼容。
- 雪球 nodriver 薄封装 adapter 与 backend 降级链：同一任务先 OpenCLI，
  失败后最多尝试一次 nodriver。
- 显式 run --execute 实验入口；仅雪球，未接入定时任务。

尚未落地：

- nodriver 仍是旧爬虫薄封装，尚未拆成会话复用的原生 backend。
- daemon、MCP、Cindy Skill 外壳尚未实现。
- run_daily.sh 仍使用旧爬虫入口，生产行为未改变。

## 2. 总体架构

```
Cindy Skill / MCP / 人工命令 / 定时任务
        ↓
crawl-gateway CLI（run / retry / health / stats / verify）
        ↓
Orchestrator：任务拆分、队列、锁、重试、熔断、降级策略
        ↓
Site Adapter：xueqiu、xiaohongshu、future sites
        ↓
Access Backend：OpenCLI、nodriver/headless、只读缓存
        ↓
Session Manager：浏览器租约、cookie/profile、人工验证状态

Storage                         Observability
SQLite 状态库 + 现有 data/ 兼容文件   结构化日志 / 指标 / 健康分
```

### 2.1 分层职责

| 层 | 职责 | 不负责 |
|---|---|---|
| Skill/MCP | 向 agent 描述调用方式、使用边界、人工验证提示 | 直接操作浏览器或网站 |
| CLI/API | 提供稳定命令与 JSON 输出 | 业务日报生成 |
| Orchestrator | 队列、锁、频控、重试、熔断、降级、统计 | 站点选择器和解析规则 |
| Site Adapter | 站点账号列表、请求语义、解析、去重、存储映射 | 全局并发策略 |
| Access Backend | OpenCLI/nodriver 调用、浏览器细节、WAF 分类 | 决定是否重试或降级 |
| Session Manager | 浏览器/profile/cookie 租约与互斥 | 判断业务成败 |
| Storage | 原子记录任务、尝试、指标和资源索引 | 展示逻辑 |

## 3. 运行形态

为避免一开始过度工程化，采用两种运行模式，共用同一套核心代码：

1. **Embedded 模式**（第一阶段）
   - `crawl-gateway run --site xueqiu` 在当前进程中执行。
   - 通过 SQLite 事务和文件锁保证跨进程互斥。
   - 适合当前定时任务与人工重跑，部署简单。

2. **Daemon 模式**（第二阶段）
   - 常驻本地服务监听 `localhost` 或 Unix socket。
   - 独占管理 OpenCLI/Chrome 会话，向所有调用方发放浏览器租约。
   - 适合多个 agent 同时使用、多个站点并发抓取和长时间会话复用。

两种模式的调用方接口保持一致。Embedded 模式先落地核心治理，Daemon 模式只替换传输层，不改变业务逻辑。

## 4. 核心对象与状态模型

### 4.1 结果状态

每次访问尝试必须归入以下状态之一：

| 状态 | 含义 | 是否消耗失败预算 | 是否影响健康分 |
|---|---|---:|---:|
| `success` | 成功读取并处理 | 否 | 正向 |
| `no_update` | 访问成功但无新增 | 否 | 正向 |
| `duplicate` | 资源已存在，按成功处理 | 否 | 正向 |
| `blocked_waf` | WAF/风控页 | 是 | 强负向 |
| `captcha_required` | 需要人工滑块/验证 | 是 | 强负向 |
| `auth_expired` | 登录态失效 | 是 | 强负向 |
| `rate_limited` | 站点限流 | 是 | 负向 |
| `http_error` | HTTP 错误 | 是 | 负向 |
| `parse_error` | 页面返回但解析失败 | 是 | 负向 |
| `network_error` | 本地网络/浏览器错误 | 是 | 弱负向 |
| `circuit_open` | 熔断开启，未发起访问 | 否 | 中性 |
| `skipped` | 配置禁用或队列取消 | 否 | 中性 |

禁止把“读不到数据”笼统记为成功；`no_update` 必须以页面结构解析成功为前提。

### 4.2 SQLite 表

建议新增 `data/gateway.sqlite3`，作为任务、指标和熔断的唯一事实来源；现有 JSON 文件只作为兼容导出。

| 表 | 关键字段 | 用途 |
|---|---|---|
| `sites` | `code`, `enabled`, `config_json`, `updated_at` | 站点注册与配置快照 |
| `jobs` | `id`, `site`, `purpose`, `status`, `started_at`, `finished_at` | 一次日报抓取或人工重跑 |
| `tasks` | `id`, `job_id`, `resource_type`, `resource_id`, `status` | 用户主页、搜索、详情页等最小任务 |
| `attempts` | `id`, `task_id`, `backend`, `status`, `error`, `started_at`, `duration_ms` | 每次实际访问 |
| `resources` | `site`, `resource_id`, `uri`, `content_hash`, `stored_path`, `first_seen_at` | 去重与产物索引 |
| `site_health` | `site`, `score`, `state`, `opened_until`, `consecutive_failures` | 健康分与熔断 |
| `quotas` | `site`, `window`, `used`, `limit`, `reset_at` | 小时/日配额 |
| `interventions` | `id`, `site`, `kind`, `status`, `created_at`, `resolved_at` | 需要人工滑块/登录的请求 |

SQLite 启用 `WAL` 与 `busy_timeout`。跨进程并发以数据库事务为第一道保护，浏览器会话另加租约锁。

## 5. 配置设计

新增 `config/sites.yaml`，站点级配置相互独立，但字段结构一致：

```yaml
sites:
  xueqiu:
    enabled: true
    purpose: article_daily
    schedule_window: "07:55-08:30 Asia/Shanghai"
    resource:
      type: user_timeline
      source: config/accounts.yaml
    backend_priority:
      - opencli
      - nodriver
      - read_only_cache
    rate_limit:
      concurrency: 1
      min_interval_seconds: 4
      max_interval_seconds: 9
      hourly_requests: 80
      daily_requests: 300
    retry:
      max_attempts: 2
      backoff_base_seconds: 60
      backoff_multiplier: 2
      retry_statuses: [network_error, http_error, parse_error]
    circuit_breaker:
      failure_threshold: 3
      window_minutes: 10
      cooldown_minutes: 60
      hard_failure_statuses: [blocked_waf, captcha_required, auth_expired]
    degradation:
      allow_read_only_cache: true
      allow_empty_result: false
      emit_minimal_report: true

  xiaohongshu:
    enabled: false
    backend_priority:
      - opencli
    rate_limit:
      concurrency: 1
      min_interval_seconds: 12
      max_interval_seconds: 30
      hourly_requests: 25
      daily_requests: 80
    circuit_breaker:
      failure_threshold: 2
      window_minutes: 10
      cooldown_minutes: 120
```

配置加载后生成不可变的运行快照，写入 `jobs.config_snapshot_json`，保证事后能追溯“当时到底用了什么策略”。

## 6. 编排流程

### 6.1 正常任务

1. CLI 校验参数与站点配置。
2. 创建 `jobs` 记录，获取站点级互斥锁。
3. 检查配额、健康分和熔断状态。
4. 将账号/搜索词/详情页拆成 `tasks`。
5. 逐任务按 `backend_priority` 访问。
6. 每次访问写入 `attempts`，更新配额与健康分。
7. 失败按状态决定重试、切换 backend、熔断或请求人工介入。
8. 汇总输出 JSON，同时导出兼容文件。

### 6.2 降级规则

- `captcha_required` / `blocked_waf`：立即停止当前站点后续请求，创建 `interventions`，进入冷却。
- `auth_expired`：不再尝试其他账号，避免连锁失效。
- `network_error` / `http_error`：可短退避重试，不立即熔断。
- `parse_error`：重试一次；连续出现则标记适配器可能过期。
- 所有 backend 均失败：若允许 `read_only_cache`，只返回缓存并明确标注降级；否则返回失败。
- 禁止把失败任务静默转换成“今日无新增”。

### 6.3 健康分

每个站点维护 0-100 健康分：

- 初始值 80。
- `success` / `no_update` / `duplicate`：+2，上限 100。
- `parse_error` / `network_error` / `http_error`：-10。
- `rate_limited`：-20。
- `blocked_waf` / `captcha_required` / `auth_expired`：-40。
- 连续硬失败达到阈值后 `circuit_open`，到期后先发一个探活请求，成功才半开恢复。

健康分只描述访问通道，不代表网站内容质量。

## 7. 雪球迁移方案

### 7.1 新目录

```
crawl_gateway/
├── __init__.py
├── cli.py                 # crawl-gateway 命令入口
├── config.py              # sites.yaml 加载与校验
├── models.py              # Job/Task/Attempt/枚举
├── storage.py             # SQLite migration 与 DAO
├── orchestrator.py        # 队列、重试、降级
├── rate_limiter.py
├── health.py              # 健康分与熔断
├── backends/
│   ├── opencli.py
│   └── nodriver.py
├── adapters/
│   ├── xueqiu.py
│   └── xiaohongshu.py
└── compatibility.py       # 导出 .last_crawl_stats.json 等现有文件
```

### 7.2 兼容要求

迁移期间保持以下接口不变：

- `python scripts/crawler_nodriver.py --all --max 20` 仍可执行，内部逐步委托 gateway。
- `data/.last_crawl_stats.json` 字段保持兼容。
- `data/{user_id}/{article_id}.md` 与 `data/index.json` 继续作为日报输入。
- `scripts/run_daily.sh` 只替换爬取命令，不改变报告、IMA 和飞书步骤。

### 7.3 迁移顺序

具体分步执行方案（缺口盘点、shadow 对比、切换与回滚）见 `docs/crawl_gateway_migration_plan.md`。

先抽离纯逻辑，再接浏览器，最后切定时任务。每一步都能独立回滚。

## 8. 开发计划

### Phase 0：基线冻结（0.5 天）

**目标**：明确现状，防止重构改变行为。

- 梳理现有 OpenCLI/nodriver 输出与失败分类。
- 固定 `xueqiu` 的成功、无更新、WAF、解析失败样例。
- 补齐结果状态枚举与兼容统计字段测试。

**验收**：现有测试全绿；新增状态分类单测。

### Phase 1：Gateway 骨架（1-2 天）

**目标**：先落地无浏览器访问的核心治理。

- 新增 `crawl_gateway` 包、`sites.yaml` 加载和配置校验。
- 建立 SQLite migration 与 `jobs/tasks/attempts` 写入。
- 实现站点级互斥、令牌桶频控、基础重试策略。
- 提供 `crawl-gateway run --dry-run` 与 `crawl-gateway stats`。

**验收**：并发调用被串行化；配额不会被并发扣减；所有尝试可查询。

### Phase 2：雪球 Adapter（2-3 天）

**目标**：雪球访问策略从日报脚本迁出。

- 将 OpenCLI 提取逻辑封装为 backend。
- 将 nodriver 浏览器启动、WAF 判定和详情读取封装为 backend。
- 保持现有 Markdown、索引和失败清单行为。
- 为 WAF、滑块、解析失败、无新增建立 fake backend 集成测试。

**验收**：同一输入下 gateway 与旧爬虫产物一致；WAF 不再被记为普通成功或无更新。

### Phase 3：编排与熔断（1-2 天）

**目标**：自动止损，减少风控扩大。

- 实现健康分、熔断、半开探活和人工介入记录。
- 实现 `retry-failed`、`health`、`verify` CLI。
- 将 `.last_crawl_stats.json` 改为 gateway 结果的兼容导出。

**验收**：连续硬失败后后续请求不发；人工验证未完成前不自动重放全量账号。

### Phase 4：切换日报链路（1 天）

**目标**：生产任务开始使用统一入口。

- `run_daily.sh` 调用 `crawl-gateway run --site xueqiu --purpose daily`。
- 保留旧命令一个版本作为回滚入口。
- 增加灰度开关：环境变量或配置可在旧爬虫与 gateway 间切换。

**验收**：连续两天日报产物、发布状态和巡检结论正常；异常时可用旧入口回滚。

### Phase 5：Skill/MCP 外壳（0.5-1 天）

**目标**：让其他 agent 安全复用，而不是复制知识。

Skill 内容包含：

- 允许调用的 gateway 命令。
- 各站点频控与熔断边界。
- 人工滑块/登录的处理流程。
- 禁止直接启动浏览器或绕过 gateway 访问目标网站。
- 如何读取 `stats/health/interventions` 判断是否可运行。

**验收**：一个新会话仅凭 Skill 能完成健康检查、任务触发和结果解读，且无法绕过频控。

### Phase 6：Daemon 与多站点（2-4 天）

**目标**：支撑多工具常驻并发。

- 增加本地 daemon 与 Unix socket/localhost API。
- 浏览器会话由 daemon 统一持有和租借。
- 新增小红书 adapter，先只开放只读列表/详情能力。
- 按独立配置灰度启用，不影响雪球主链路。

**验收**：两个调用方并发请求时浏览器会话不冲突；雪球配额不被小红书消耗；单站点熔断不影响其他站点。

## 9. 测试策略

- **单元测试**：状态分类、配置校验、令牌桶、退避、健康分、熔断状态机。
- **集成测试**：fake OpenCLI/nodriver backend 覆盖成功、无更新、重复、WAF、滑块、解析失败。
- **并发测试**：多进程同时触发同一站点任务，验证互斥与配额原子性。
- **兼容测试**：对比 gateway 与旧爬虫的统计文件、索引和 Markdown 输出。
- **手工演练**：断网、OpenCLI 不可用、真实滑块、SQLite 锁竞争。

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| 过早引入 daemon 增加复杂度 | 先 Embedded + SQLite，浏览器租约稳定后再 daemon 化 |
| 站点结构频繁变化 | 适配器独立测试与 `parse_error` 快速熔断 |
| 迁移期间统计口径不一致 | 兼容文件由 gateway 单点导出，禁止双写业务结论 |
| 其他工具仍绕过 gateway | Skill 明确禁令；daemon 独占浏览器 profile 与 OpenCLI 租约 |
| 小红书与雪球风控差异 | 站点独立配置和熔断阈值，不共享失败预算 |

## 11. 暂不做

- 不做分布式队列和云端调度。
- 不做验证码自动破解；只识别人工介入需求并暂停访问。
- 不把其他工具的通用浏览器操作全部收编，第一阶段只治理目标站抓取。
- 不在同一个 PR 中重写报告、IMA、飞书链路。

## 12. 总体验收

Crawl Gateway 达到可用状态时，应满足：

1. 所有目标站抓取均有统一 CLI 入口。
2. 每次访问可追溯到 job/task/attempt。
3. 频控、重试、熔断和降级由配置驱动。
4. 雪球日报链路完成灰度切换且可回滚。
5. 其他 agent 通过 Skill/MCP 使用 gateway，无法绕过统一治理。
6. 新增一个站点只需实现 adapter、配置和站点测试，不需要复制治理代码。
