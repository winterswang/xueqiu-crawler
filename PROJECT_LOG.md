# 📋 项目开发跟踪文档

> 📅 创建于：2026-05-24 | 最后更新：2026-05-24
> 🌿 当前分支：main

---

## 📌 项目概述

| 属性 | 内容 |
|------|------|
| 项目名称 | xueqiu-crawler |
| 项目简介 | 自动爬取指定雪球用户的专栏文章，AI 分析 + 日报生成 + 推送 IMA 知识库 |
| 技术栈 | Python 3 (requests, pyyaml, filelock, anthropic, openai, playwright, python-dotenv) |
| 仓库地址 | https://github.com/winterswang/xueqiu-crawler |
| 主要负责人 | winterswang |

---

## 🚀 功能特性 (Features)

| ID | 特性描述 | 优先级 | 状态 | 负责人 | 开始日期 | 目标版本 | 关联Issue/PR | 备注 |
|----|---------|--------|------|--------|----------|----------|-------------|------|
| F-001 | 专栏文章过滤 + 历史快照 | P0 | 已发布 | winterswang | - | v1.0 | - | 项目起点 |
| F-002 | 飞书消息推送（标记文件模式） | P1 | 已发布 | winterswang | - | v1.0 | PR #4 前身 | 接口改版 |
| F-003 | 切换智谱 GLM-5 模型分析 | P1 | 已发布 | winterswang | - | v1.0 | - | Gist + 链接模式 |
| F-004 | 分析模块重构 + 报告格式优化 | P1 | 已发布 | winterswang | - | v1.0 | - | 80fb614 |
| F-005 | 多账号支持（11 个雪球账号） | P0 | 已发布 | winterswang | - | v1.0 | - | 71d0172 + ... |
| F-006 | 自动登录 + 二维码检测 | P1 | 已发布 | winterswang | - | v1.0 | - | 3cbb562 |
| F-007 | Smart crawler v2 迭代抓取 | P0 | 已发布 | winterswang | - | v1.0 | - | 780cdc8 |
| F-008 | JSON 解析韧性 + MiniMax M2.7 切换 | P1 | 已发布 | winterswang | - | v2.3 | - | 6b86817 |
| F-009 | 代码审查修复 — 4 阶段优化 (P0-P2) | P0 | 已发布 | winterswang | - | v2.3 | PR #3 | 6d0bcc5 |
| F-010 | 日志系统完善 + JSON 解析失败诊断 | P1 | 已发布 | winterswang | - | v2.5 | - | ee4dbeb |
| F-011 | Playwright 主用爬虫 + XCrawl 废弃 | P0 | 已发布 | winterswang | - | v2.6 | PR #10, #11 | 84062ac |
| F-012 | crawler 自动加载 cookies + login --headless | P0 | 已发布 | winterswang | - | v2.10 | - | b429000 |
| F-013 | 全面反检测增强 + Chromium new headless 模式 | P0 | 已发布 | winterswang | - | v2.12 | - | c780800 |
| F-014 | 超时统一读 config.yaml (30s→60s) | P1 | 已发布 | winterswang | - | v2.7 | #12 | 64297e1 |
| F-015 | 数据统计与分析报告生成 | P2 | 已发布 | winterswang | - | v2.0 | - | scripts/generate_report.py |
| F-016 | Info Card 生成 + IMA 知识库推送 + 飞书推送 | P0 | 已发布 | winterswang | - | v2.6+ | - | 现由 `scripts/publish_daily_report.py` + `scripts/push_feishu.py` 承担（`_v3.py` 已于 f816595 删除） |
| F-017 | MiniMax API 详细执行日志 + 指数退避重试 | P1 | 已发布 | winterswang | 2026-05-24 | v2.13 | #13 | 096f90a |
| F-018 | LLM 响应字段完整性校验 + 修复机制 | P1 | 已发布 | winterswang | 2026-05-23 | v2.6 | #7 | 77ee580 |
| F-019 | Mock 分析警告标记（日报头部插 ⚠️ 提示） | P2 | 已发布 | winterswang | 2026-05-23 | v2.6 | #7 | 77ee580 |
| F-020 | 截断阈值 4000→8000（长文分析完整性） | P1 | 已发布 | winterswang | 2026-05-23 | v2.6 | #7 | 77ee580 |
| F-021 | 爬取覆盖率反馈（日报末尾显示爬取状态） | P2 | 已发布 | winterswang | 2026-05-23 | v2.6 | #7 | 77ee580 |

**优先级说明**：P0-核心/阻塞、P1-重要、P2-一般、P3-锦上添花
**状态说明**：规划中 / 开发中 / 已完成 / 已发布 / 已搁置

---

## 🐛 Bug 跟踪

| ID | Bug描述 | 严重程度 | 状态 | 发现日期 | 修复日期 | 关联Commit | 关联Issue | 负责人 |
|----|---------|---------|------|----------|----------|------------|-----------|--------|
| B-001 | 飞书推送接口调用失败 | Major | 已修复 | - | - | cb08294 | - | winterswang |
| B-002 | 报告文件路径漏掉 .md 后缀 | Minor | 已修复 | - | - | ab8cc94 | - | winterswang |
| B-003 | MiniMax ThinkingBlock-only 响应导致 JSON 解析失败 | Major | 已修复 | - | - | 808d667 | - | winterswang |
| B-004 | 日报中作者字段为空（index.json 缺 author） | Major | 已修复 | - | 2026-03-? | 1979199 | PR #6 | winterswang |
| B-005 | 3 项并行 bug 修复（批量修复） | Major | 已修复 | - | - | 027685d | - | winterswang |
| B-006 | analyzer.py 未使用的 log_execution_stage 导入 | Trivial | 已修复 | - | - | acbe2d1 | - | winterswang |
| B-007 | browser.close() 偶发 EPIPE 崩溃 | Major | 已修复 | - | - | ef58b65 | - | winterswang |
| B-008 | _load_index 文件存在但缺 articles/history 键 | Minor | 已修复 | - | - | 1adab08 | - | winterswang |
| B-009 | 死评分维度（value_alignment / safety_margin）未清理 | Minor | 已修复 | - | - | b69a171 / b327a99 | PR #4 | winterswang |

**严重程度**：Critical-阻断 / Major-严重 / Minor-一般 / Trivial-轻微
**状态说明**：待确认 / 已确认 / 修复中 / 已修复 / 已验证 / 无法复现 / 不予修复

---

## 📝 Code Review 记录

| 日期 | 审查人 | 审查范围 | 类型 | 发现的问题 | 处理状态 | 关联PR/MR |
|------|--------|---------|------|-----------|---------|-----------|
| 2026-03 待补充 | - | 全仓库架构审查 | 架构审查 | 三个发布版本并存、check_article_quality 重复定义、index.json 无写入保护、硬编码路径等 | 已并入技术债务表（D-001~D-004） | CLAUDE.md 记录 |
| 2026-03 待补充 | - | analyzer.py / quality_check.py | 代码规范 | check_article_quality 重复定义（2项 vs 4项） | ✅ 已解决（f816595） | README.md 记录 |
| 2026-10-07 | Claude | 全仓库债务复核 | 架构审查 | 逐条核实 T-001~T-008 / D-001~D-008；8 条里 4 条已解决、4 条部分解决；新记 D-009~D-012 | 见技术债务表 | 本次复核 |
| 2026-05-22 | winterswang | README.md 质量评估 | 架构审查 | 整体评分 7/10，关键问题标记为 P0/P1/P2 | 部分修复 | README.md |

**类型说明**：功能审查 / 安全审查 / 性能审查 / 代码规范 / 架构审查
**处理状态**：待处理 / 已修复 / 已讨论通过 / 已知悉

---

## ✅ TODO 事项

| ID | 事项 | 优先级 | 状态 | 创建日期 | 截止日期 | 负责人 | 关联 | 备注 |
|----|------|--------|------|----------|----------|--------|------|------|
| T-001 | 删除 publish_daily_report.py 和 publish_daily_report_v2.py（三个版本并存） | 高 | ✅ 已完成 | 2026-05-22 | - | - | P0 | 2026-10-07 核实：`_v2`/`_v3` 均已删除（f816595「版本清理」），只剩 `scripts/publish_daily_report.py` |
| T-002 | 合并 analyzer.py 和 quality_check.py 的 check_article_quality | 高 | ✅ 已完成 | 2026-05-22 | - | - | P0 | 2026-10-07 核实：`scripts/quality_check.py` 已删除，实现统一到 `scripts/analyzer.py:89`，README:148 标注已统一 |
| T-003 | index.json 加写入锁或迁移到 SQLite | 高 | 🟡 部分完成 | 2026-05-22 | - | - | P0 | 已改成 tmp + `os.replace` 原子写（`crawler_nodriver.py:164`、`crawl_gateway/adapters/xueqiu.py:123`），不再有半写损坏；但**无文件锁**（全仓无 fcntl/flock），并发仍是后写覆盖。实际由 `.cron_running.lock`（60 分钟窗口）与 gateway 的 SQLite `site_locks` 在进程层挡住。`scripts/rebuild_index.py` 是 OOM/SIGKILL 丢索引的恢复工具 |
| T-004 | 消除 publish_daily_report_v3.py 中的硬编码路径 | 中 | ✅ 已完成 | 2026-05-22 | - | - | P1 | 随文件删除一并消失；现行 `scripts/publish_daily_report.py` 全文无 `/root` 路径 |
| T-005 | 统一凭证管理（集中到 config.yaml） | 中 | 🟢 以其它方式达成 | 2026-05-22 | - | - | P1 | 目标（凭证不落码）已达成，但没有走 config.yaml：凭证全在 `.env` + `~/.config/ima/`。`config.yaml` 内无任何 key/token/secret 字段。原方案未做，保持不变即可 |
| T-006 | 写单元测试（classify_stock_market / check_article_quality） | 中 | ✅ 已完成 | 2026-05-22 | - | - | P1 | `tests/test_core.py:28-64`（5 例）与 `:94-156`（5 例）分别覆盖两个函数 |
| T-007 | generate_report.py 改为可导入模块 | 低 | ✅ 已完成 | 2026-05-22 | - | - | P2 | 实测 `import scripts.generate_report` 成功，且 `tests/test_generate_report_no_articles.py:21` 就在 import 它。**但**模块级有副作用（`load_dotenv(override=True)` + `sys.path.insert`），故仍是「约定上只作 CLI 用」，见 D-007 |
| T-008 | 所有路径和凭证移到 config/config.yaml | 中 | 🟡 部分完成 | 2026-05-22 | - | - | P1 | 路径改由 `Path(__file__)` 相对推导解决，未配置化；生产代码仍剩 3 处硬编码绝对路径，见 D-008 |

**优先级**：高 / 中 / 低
**状态说明**：待开始 / 进行中 / ✅ 已完成 / 🟡 部分完成 / 🟢 以其它方式达成 / 已取消
**核实说明**：本表 2026-10-07 逐条对着代码复核过，每行「备注」里的 `文件:行号` 是当时的证据。落地提交一栏留给后续补充。

---

## 🔄 版本发布记录

> 每个版本的 Changelog，按时间倒序排列。新版本追加在最前面。

### v2.13 (2026-05-24)

**变更摘要**：MiniMax API 日志增强 + 指数退避重试机制

#### ✨ 新增功能
- MiniMax API 详细执行日志（logs/minimax_api.log），每次调用记录结构化 JSON
- 指数退避重试：2s → 4s → 8s → ... → max 30s（最多 3 次重试）
- 重试条件：529/429/502/503/504、NetworkError、Timeout
- 不可重试 4xx 错误立即失败，Anthropic SDK max_retries=0

#### 🔧 优化改进
- 统计扩展：新增 success_calls / retry_count / total_latency_ms
- 报告末尾显示平均延迟和重试次数
- 仅对未知异常输出完整 traceback，减少日志噪声

#### 📚 配置变更
- config.yaml 新增 analysis.retry 块（max_retries / base_delay_ms / backoff_multiplier / retry_on_http_codes / request_timeout_ms）

### v2.12 (2026-05-24)

**变更摘要**：反检测增强 + headless 新模式

#### ✨ 新增功能
- 全面反检测增强 + Chromium new headless 模式
- crawler 自动加载 cookies + login 支持 --headless

#### 🐛 Bug 修复
- _load_index 确保已有文件也有 articles/history 键

#### 🔧 优化改进
- headless 改为 true（--headless=new arg 启用新无头模式）

#### 📚 文档更新
- 更新至 2.12 文档

### v2.10 (2026-05-? 待补充)

**变更摘要**：Playwright 主用爬虫 + 废弃 XCrawl

#### ✨ 新增功能
- Playwright 主用爬虫，XCrawl 退为 fallback (#10)
- 移除 Link-Collector 依赖，简化流水线 (#9)
- 超时统一读 config.yaml (30s→60s) + Playwright 自动登录脚本 (#12)

#### 🐛 Bug 修复
- 3 项 bug 修复（027685d）
- 移除 analyzer.py 未使用的 log_execution_stage 导入
- browser.close() 防 EPIPE 崩溃
- 作者字段写入 index.json，解决日报中作者为空的问题

#### 📚 文档更新
- 同步架构变更到文档
- 更新至 2.10

### v2.6 (2026-05-23)

**变更摘要**：4 项高优先优化 + 死评分维度清理 + 日志诊断完善

#### ✨ 新增功能
- 完善日志系统与 JSON 解析失败诊断
- **C3**: 截断阈值 4000→8000（config.yaml + analyzer.py 同步）
- **C5**: Mock analysis 警告 — mock 结果添加 'mock': True 标记，日报头部插 ⚠️ 提示
- **C6**: LLM 响应字段完整性校验 — _validate_analysis / _missing_fields / _repair_analysis 三件套
- **A1**: 爬取覆盖率反馈 — crawler 保存 .last_crawl_stats.json，日报末尾显示爬取状态

#### 🐛 Bug 修复
- MiniMax ThinkingBlock-only 响应导致 JSON 解析失败
- 移除已失效的评分维度 (value_alignment/safety_margin)

#### 📚 文档更新
- 更新至 2.5/2.6

### v2.3 (2026-03-? 待补充)

**变更摘要**：代码审查修复 + MiniMax 切换

#### ✨ 新增功能
- 代码审查修复 — 4 阶段优化 (P0-P2)
- JSON 解析韧性 + MiniMax M2.7 切换

#### 📚 文档更新
- PR #3 合入 main，版本号更新至 2.3

### v2.2 (2026-03-? 待补充)

**变更摘要**：新增分析维度 + 账号扩展

#### ✨ 新增功能
- 新增雪球账号 价值投资新经济 (7680894870)
- 完善 README，补充架构图和问题清单

### v2.0 (2026-03-? 待补充)

**变更摘要**：架构重构 + 多账号支持

#### ✨ 新增功能
- Smart crawler v2 with iterative fetching
- 自动更新账号名称和二维码登录检测
- 添加多个雪球账号（8790885129, 4641860462, 1156957441）
- 重构分析模块和报告格式

#### 🐛 Bug 修复
- 修复报告文件路径漏掉 .md 后缀
- 更新飞书推送为标记文件模式
- 修复飞书推送接口调用

### v1.0 (早期)

**变更摘要**：项目初始化

#### ✨ 新增功能
- 雪球专栏文章爬取（基础功能）
- 专栏文章过滤，排除评论和短状态
- 切换到智谱 GLM-5 模型分析
- Gist 提交 + 发送链接流程

---

## 🏗️ 技术债务

| ID | 债务描述 | 影响范围 | 优先级 | 计划处理版本 | 创建日期 | 状态 |
|----|---------|---------|--------|-------------|----------|------|
| D-001 | 三个发布版本并存（.py / v2 / v3），功能重叠 | publish_* | P0 | v2.13 | 2026-05-22 | ✅ 已解决（f816595） |
| D-002 | check_article_quality 在 analyzer.py 和 quality_check.py 中重复定义 | 质量分析 | P0 | v2.13 | 2026-05-22 | ✅ 已解决（f816595） |
| D-003 | index.json 无写入保护，并发写入会损坏数据 | 数据层 | P0 | v2.13 | 2026-05-22 | 🟡 部分解决（已原子写、仍无锁） |
| D-004 | publish_daily_report_v3.py 硬编码路径 /root/.openclaw/... | 发布层 | P1 | v2.13 | 2026-05-22 | ✅ 已解决（随文件删除） |
| D-005 | IMA 凭证曾在脚本中硬编码（已通过 ~/.config/ima/ 解决） | 安全 | P1 | 已解决 | 2026-05-22 | ✅ 已解决 |
| D-006 | 零测试覆盖 — 关键函数无单元测试 | 全局 | P1 | v2.13 | 2026-05-22 | 🟡 部分解决（原判断已不成立，见下） |
| D-007 | generate_report.py 不可导入（只能 CLI） | 报告层 | P2 | v2.14 | 2026-05-22 | ✅ 事实已不成立（可 import，但有导入副作用） |
| D-008 | 路径和凭证分散在各脚本中 | 全局 | P1 | v2.13 | 2026-05-22 | 🟡 部分解决（凭证已收口，路径剩 3 处） |

**状态说明**：待处理 / 处理中 / ✅ 已解决 / 🟡 部分解决（核实于 2026-10-07）

**逐条核实说明（2026-10-07，均带代码证据）**

- **D-001 / D-002 / D-004 / D-005**：均已实打实解决。`publish_daily_report_v2.py`、`_v3.py`、`scripts/quality_check.py` 在工作区与 HEAD 中都不存在；现行 `publish_daily_report.py` 无 `/root` 路径，凭证走 `publish_daily_report.py:34-45`（env → `~/.config/ima/`）。
- **D-003**：`crawler_nodriver.py:164` 与 `crawl_gateway/adapters/xueqiu.py:123` 都已是 tmp + `os.replace` 原子替换，**半写损坏**这一项已解决。剩余风险是**并发丢更新**（无文件锁，全仓 grep 无 fcntl/flock）。缓解措施是进程层：`.cron_running.lock`（60 分钟窗口，见 `run_daily.sh`）保证 cron 单实例；gateway 侧另有 SQLite `site_locks` 站点租约。若要彻底闭环，需给 index 写入加锁，或让 gateway 完全接管索引写入。
- **D-006**：**「零测试覆盖」这个整体判断已经为假**，宜按「部分解决」重估。现状：`tests/` 下 17 个 pytest 文件、116 个 `def test_`（约 119 个用例），覆盖 `analyzer`、`crawler_nodriver`、`opencli_extractor`、`generate_report`、`publish_daily_report`、`parse_daily_report`、`sync_waf_patterns` 及整个 `crawl_gateway`。**残余缺口**（按「在生产 cron 路径上 + 完全无测试」排序）：
  1. `scripts/sync_raw_articles_to_ima.py`（325 行，`run_daily.sh` 步骤 5/6）— 完全无测试
  2. `scripts/push_feishu.py`（297 行，步骤 6/6）— 完全无测试
  3. `scripts/cookies.py`（263 行，步骤 1/4 的 `--check`）— 完全无测试（`is_expired`/`is_valid` 等纯逻辑易测却未测）
  4. `scripts/slider_solver.py`（319 行，风控时被爬虫路径间接调用）— 完全无测试，逻辑复杂、失败代价高
  5. `scripts/analyzer.py` 主体（1686 行）— 仅核心纯函数被测，AI 调用链与 `generate_daily_report` 主流程未测
- **D-007**：「不可导入」在事实上不成立（见 T-007）。**但**该文件模块级就执行 `load_dotenv(..., override=True)` 与 `sys.path.insert`，导入会污染调用方的环境变量，所以「不要随手 import」这条**约定仍然成立**，只是原因从「做不到」变成「有副作用」。README:159 与 PROJECT_RECORD.md:161 的表述已按此更正。
- **D-008**：凭证侧已收口（无任何 key/token/password 字面量）。路径侧仍有 **3 处**生产硬编码绝对路径：
  - `scripts/slider_solver.py:42` — `/root/.xueqiu_crawler/cookies.json`（有 `if mf.exists()` 守卫，本地静默跳过）
  - `scripts/run_daily.sh:68` — `find /root/.cache/openclaw`（清理 nodriver 临时 profile，已用 `2>/dev/null || true` 吞错，**有意面向 Linux 生产**，属可移植性瑕疵而非缺陷；修的话应改成 `"$HOME/.cache/openclaw"` 或加环境变量开关）
  - `scripts/test_waf_bypass.py:25` — 同上 cookies 路径（人工排障脚本）

  另有 **2 处硬编码标识符**（非凭证，但同类「写死」，值得记一笔）：`sync_raw_articles_to_ima.py:48` 的 `KB_ID`、`publish_daily_report.py:47` 的 `IMA_FOLDER_ID`。以及模型名曾在 `analyzer.py:1239`、`analyzer.py:1555`、`push_feishu.py:37` 与 `config/config.yaml` **4 处源码 + 1 处 `.env` 手工同步**（注释自己承认「与 config.yaml 同步」）——这个是真正的漂移风险点。**2026-10-07 已收口**：新增 `scripts/llm_config.py` 作为唯一解析入口，`analyzer` 与 `push_feishu` 都从 `config/config.yaml` 取，并移除了 `ANALYZE_LLM_MODEL` 环境覆盖（不入库的第二来源会让服务器上残留的旧值静默压过仓库配置）。同批把 `.env.example` 里漏掉的 `ARK_API_KEY` / `ARK_CODING_BASE_URL` / `FEISHU_WEBHOOK` / `XUEQIU_USERNAME` 等补上——照着原文件配环境根本跑不起来。见 D-009。

**新发现（2026-10-07 复核时新增，原表未记）**

| ID | 债务描述 | 影响范围 | 优先级 | 创建日期 | 状态 |
|----|---------|---------|--------|----------|------|
| D-009 | 模型 id 曾散在 **5 处**手工同步（`config.yaml`、`analyzer.py` 两处默认值、`push_feishu.py` 默认值、不入库的 `.env` 的 `ANALYZE_LLM_MODEL`） | 配置一致性 | P2 | 2026-10-07 | ✅ 已解决 |
| D-010 | `scripts/test_waf_bypass.py`、`scripts/test_batch_crawl.py` 命名像测试但不在 pytest 收集路径（CI 只跑 `python -m pytest tests/ -q`），容易被误当成「有覆盖」 | 可维护性 | P2 | 2026-10-07 | 待处理 |
| D-011 | `PROJECT_RECORD.md` 整篇按**已删除**的 `quality_check.py` / `publish_daily_report_v3.py` / `crawler.py`（标着「✅ 唯一」，实际已归档）描述流水线与模块清单，与现状严重脱节 | 文档可信度 | P1 | 2026-10-07 | 待处理 |
| D-012 | `scripts/run_daily.sh` 步骤编号不自洽：`[1/4]`、`[2/4]`、`[3/4]` 之后接 `[4/6]`、`[5/6]`、`[6/6]` | 可维护性 | P3 | 2026-10-07 | 待处理 |

---

## 📊 项目指标

> 定期更新，可选择性维护

| 指标 | 数值 | 更新时间 |
|------|------|----------|
| Git 提交数 | 160 | 2026-10-07 |
| 已修复 Bug 数 | 9 | 2026-05-24 |
| 已完成 Feature 数 | 21（未记入 crawl_gateway 系列，实际更多） | 2026-10-07 |
| 活跃分支数 | 1 (main) | 2026-10-07 |
| 自动化测试 | 17 个测试文件 / 116 个 test 函数 / 119 用例全绿（`python -m pytest tests/ -q`） | 2026-10-07 |
| 代码规模 | scripts/ 7004 行 + crawl_gateway/ 2033 行 | 2026-10-07 |

> 原「测试覆盖率 0%」是 2026-05-24 的快照，**现已不成立**（见 D-006）。当前没有接覆盖率工具，所以不写百分比，只记测试规模。

---

## 📅 重要里程碑

| 日期 | 里程碑 | 描述 | 状态 |
|------|--------|------|------|
| 2026-03 早期 | v1.0 基础爬虫 | 雪球文章爬取 + GLM-5 分析 + 飞书推送 | 已达成 |
| 2026-03 中旬 | v2.0 架构重构 | 多账号支持 + Smart crawler v2 + 模块重构 | 已达成 |
| 2026-03 下旬 | v2.3 代码审查修复 | 4 阶段优化 + MiniMax M2.7 切换 | 已达成 |
| 2026-05-? | v2.6 Playwright 切换 | 废弃 XCrawl，全面改用 Playwright | 已达成 |
| 2026-05-24 | v2.12 反检测增强 | Chromium new headless + 反检测 + headless 登录 | 已达成 |
| 2026-10-07 | v2.13 工程化收尾 | 清理技术债务：旧版删除、写入保护、测试覆盖 | 🟡 大部分达成（旧版删除 ✅、测试覆盖 ✅、写入保护部分：已原子写仍无锁，见 D-003） |

**状态说明**：计划中 / 进行中 / 已达成

---

## 📋 会议 / 决策记录

> 记录重要的技术决策和会议结论

| 日期 | 决策/议题 | 结论 | 参与者 | 关联 |
|------|----------|------|--------|------|
| 2026-03 待补充 | 爬虫引擎选型 | 从 XCrawl 切换到 Playwright，Playwright 主用，XCrawl fallback | winterswang | #10 |
| 2026-03 待补充 | AI 模型选型 | 从 GLM-5 切换到 MiniMax M2.7，提高 JSON 解析成功率 | winterswang | 6b86817 |
| 2026-05 待补充 | 全面废弃 XCrawl | XCrawl 全部代码与引用删除 | winterswang | #11 |
| 2026-05-22 | 代码质量评估 | 确立 P0/P1/P2 问题分级，规划工程化收尾方向 | winterswang | README.md |
| 2026-05-24 | 反检测方案 | 全面反检测增强 + Chromium new headless 模式 | winterswang | c780800 |

---

> 💡 **使用提示**
> - 新增条目时，读取当前最大 ID 序号 +1
> - 日期格式统一使用 `YYYY-MM-DD`
> - 每完成一次版本发布，在"版本发布记录"中新增一个版本块
> - 已完成的 TODO 和已修复的 Bug 保留在表格中，用于历史追溯
> - 如需归档历史条目，可创建 `PROJECT_LOG_ARCHIVE.md`
>
> <!-- @@LAST_ANALYZED: 096f90aaa337bc02a1c4185de075276de586525f @@-->
