# opencli-adapters 独立成独立项目的重构计划书

> **2026-10-12：已执行完毕。** 本仓已独立为 `opencli-adapters`（GitHub:
> `winterswang/opencli-adapters`），插件 ID 同步更名为 `opencli-adapters`（本文件 §3
> 建议的「ID 暂不改」已被实际决策取代）。引用收口清单与门控见独立仓
> `docs/consumer-migration-plan.md`。本文件保留为历史方案，正文中的路径与结论以独立仓为准。

> **给新会话的用法**：这份文件是自包含的作业指导。开新对话后，把本文件路径交给它，
> 让它**先读第 1 节（现状尽调）与第 3 节（决策点）**，确认决策后再按第 4 节逐步执行。
> 文中所有路径、行号、文件清单均于 2026-10-12 在本机核实过。

---

## 0. 一句话

把 `xueqiu-crawler/opencli-adapters/` 抽成**独立 git 仓库**，插件身份（`xueqiu-adapters`）
与 `~/.opencli/` 下的所有状态路径**保持不变**；把「插件自身的测试」搬过去、
把「消费方与插件之间的契约测试」留在各消费方，并处理掉 WAF 正则的跨仓生成。

**先明确它解决什么、不解决什么**（这一点决定期望值）：

| | |
|---|---|
| ✅ 解决 | 独立 CI（插件改坏不再只能靠消费方测试发现）、独立提交历史与归属、能独立发版、插件测试不再寄生在 crawler 的 `tests/` 里 |
| ❌ **不解决** | 「机器级共享、改源码立即对所有消费者生效、多个会话可能同时在改同一个工作区」—— 那是**符号链接 + 平铺插件目录**的机制使然，与它在哪个仓库无关。提取后这条风险原样存在，仍需靠 `CONTRACT.md` 的改动检查表与纪律约束 |

---

## 1. 现状尽调（设计依据，均已核实）

### 1.1 它现在是什么

```
xueqiu-crawler/opencli-adapters/        ← 源码本体（在 crawler 仓里，10 个提交）
  ├─ 7 个命令文件   article.js(146) comments.js(355) news.js(116) replies.js(106)
  │                 search.js(167) stock-notices.js(92) user-articles.js(115)
  ├─ 入口/工具      rate-limit.js(208) utils.js(58)
  ├─ lib/           11 个模块（concurrency domains domain-tables env lockfile paths
  │                 policy ratelimit requestlog util xueqiu-symbol）
  ├─ 安装/校验      install.sh(57) check-drift.sh(101)
  ├─ 身份           opencli-plugin.json（name = `xueqiu-adapters`，版本 1.0.0）
  └─ 文档           CONTRACT.md(169) README.md(229)
```

共约 2900 行。**运行时身份**由 `~/.opencli/plugins/xueqiu-adapters` → 符号链接到本目录决定：

```
~/.opencli/plugins/xueqiu-adapters -> /Users/wangguangchao/code/claude_code/xueqiu-crawler/opencli-adapters
```

符号链接由 `install.sh` 里的 `opencli plugin install "file://$DIR"` 创建 —— 这一点很重要：
**换目录后只要在新位置重跑 `install.sh`，链接会重新指向，不需要手工改软链。**

### 1.2 它的产物（提取后**不应**受任何影响）

| 路径 | 谁写 | 谁读 |
|---|---|---|
| `~/.opencli/<域名键>-throttle.json` | 插件 | 插件；`scripts/schedule-checks/peak.mjs` |
| `~/.opencli/inflight/<域名键>/*.slot` | 插件 | 插件 |
| `~/.opencli/adapter-calls.jsonl` | 插件 | `scripts/analyze_opencli_usage.py` |
| `~/.opencli/xueqiu-calls.jsonl` | Python 调用方 | `scripts/analyze_opencli_usage.py` |
| `~/.opencli/rate-limits.json` | 用户 | 插件 |

这些路径**全部与源码位置无关**，提取不动它们 ⇒ 限速排期、台账、并发槽的历史数据原地延续。

### 1.3 提取会牵动的地方（**这张表就是工作量清单**）

| # | 位置 | 性质 | 处理 |
|---|---|---|---|
| 1 | `opencli-adapters/` 整个目录 | 源码本体 | 移入新仓 |
| 2 | `tests/test_opencli_throttle.py:23` `test_opencli_adapter_domains.py:23` `test_opencli_adapter_symbols.py:25` `test_opencli_adapter_plugin_integrity.py:25` `test_opencli_command_names.py:24` | `PLUGIN_DIR = <crawler>/opencli-adapters`，**5 个文件解析插件源码** | 见 §3-D4 拆分 |
| 3 | `scripts/sync_waf_patterns.py:29` | `ADAPTERS_DIR = <crawler>/opencli-adapters`，**会往插件里写代码**（生成 `BLOCK_GUARD` 正则，改 4 个文件） | 见 §3-D3 |
| 4 | `tests/test_waf_patterns_sync.py` | 一半测插件同步、一半测 `waf_bridge` 能到达 analyzer | 拆开：插件那半搬走，`waf_bridge` 那半留下 |
| 5 | `scripts/waf_bridge.py` | **通用** crawler 基建（还服务 `crawl_gateway/adapters/xueqiu.py:334`、`generate_report.py:27`、`opencli_extractor.py:32`、`crawler_nodriver.py:33`） | **留在 crawler**，不要动 |
| 6 | `xueqiu-monitor/tests/test_opencli_contract.py:25` | `PLUGIN_DIR = CRAWLER/"opencli-adapters"`，跨仓超时契约；CI 里已自动跳过 | 改指向新仓（见 §3-D5） |
| 7 | `~/.claude/skills/xueqiu-adapters/SKILL.md`（**4 处**：触发场景、它是什么、CONTRACT 指路、验证命令 `cd <repo>/xueqiu-crawler/opencli-adapters`） | 用户级 skill，正文硬编码 `xueqiu-crawler/opencli-adapters/...` | 改路径（这是 agent 侧的主要入口，最容易漏 —— 它在仓库外） |
| 8 | 一批**注释/docstring**里的路径 | `scripts/opencli_extractor.py`(12,59,65,125,238)、`analyze_opencli_usage.py`(54)、`tests/test_opencli_extractor_article.py`(3，仅 docstring 提及)、`monitor/src/detail_fetcher.py`(371,449,510)、`monitor/src/opencli_budget.py`(6)、`monitor/tests/test_opencli_budget.py`(26)、`analyzer/src/xueqiu_analyzer/{waf.py:32,182, fetcher_opencli.py:124, opencli_rate_limiter.py:4,53,92, opencli_call_logger.py:34}`、三个仓的 `PROJECT_LOG.md` | 逐个改路径；不影响行为，但「文档说谎」正是 `CONTRACT.md` 存在的原因 |
| 9 | memory 分片 `project_xueqiu-opencli-adapter-shared-module.md` | 记着 `<repo>/opencli-adapters` 这个路径 | cutover 后更新 |
| 10 | `opencli-adapters/README.md`、`CONTRACT.md` §1/§5/§8 | 自述路径与验证命令 | 随目录搬走并改自述 |

**已确认不受影响的（别去改）**：三个仓的 Python/JS 调用点全是 `subprocess.run(["opencli", ...])`
或 `opencli` CLI —— 它们调的是 CLI，不是插件目录；三个仓的 `.github/workflows/*.yml`
**没有任何一处**提到插件路径或名字（crawler 的 CI 只是「插件恰好在检出目录里」而隐式生效，
见 §7-风险 R5）；`crontab` 与 `~/Library/LaunchAgents/` 里**无** opencli 相关条目。

---

## 2. 目标 / 非目标

**目标**
- 插件有独立仓库、独立 CI、独立提交历史与归属。
- 插件自身的测试跟着插件走；消费方只保留「消费方与插件之间的契约」测试。
- 插件身份与 `~/.opencli/` 状态路径零变化 ⇒ 消费者、cron、agent 会话**都不需要改调用方式**。
- WAF 正则的生成链（analyzer → 插件）有明确归属，不因搬家而断。

**非目标（本轮不做）**
- 不改插件的运行机制（仍是机器级符号链接 + 平铺目录）。
- 不重命名插件 id（见 §3-D1）。
- 不改 `~/.opencli/` 下任何文件名（换名 = 限速排期清零、台账断档）。
- 不重写插件测试为 JS（见 §3-D4）。
- 不动 `waf_bridge.py` 与 analyzer 的 `waf.py`（风控判定的唯一实现）。

---

## 3. 决策点（**开工前请先确认这 7 条**，每条附推荐）

### D1. 仓库名与插件 id
现状：目录叫 `opencli-adapters`，插件 id 叫 `xueqiu-adapters`，而它其实已覆盖 `web article`（通用）与 `xiaohongshu search` —— 名字是历史遗留的窄名。

- **推荐**：仓库名用 `opencli-adapters`（与目录一致）；**插件 id 暂不改**。改 id 要 `plugin uninstall/install`、改 `~/.opencli/plugins/` 下的链接名、改 SKILL 与 CONTRACT，收益只是好看，风险却是身份跳变。留作后续独立一次改动。
- 备选：一并改名（如 `opencli-adapters`）。若要做，**与本次迁移分开**，别混在一个 PR。

### D2. 历史保留
插件在 crawler 里有 **10 个提交**（`git log --oneline -- opencli-adapters | wc -l`）。历史短但含金量高（北交所前缀修正、限速器重构、台账）。

- **推荐**：`git subtree split -P opencli-adapters -b plugin-only` 后推入新仓 —— 保留 blame 与提交信息，成本几乎为零。
- 备选：新仓直接拷文件、首个提交说明来历。仅当 subtree 出问题再用。

### D3. WAF 正则生成器归谁
现状：`crawler/scripts/sync_waf_patterns.py` 读 `waf_bridge.CONTENT_PATTERNS`（源头是 analyzer 的 `xueqiu_analyzer.waf`），重写插件 4 个文件（`news.js` `replies.js` `user-articles.js` `article.js`）里的 `BLOCK_GUARD` 行；并由 `--check` 与 `tests/test_waf_patterns_sync.py` 卡住漂移。

- **推荐**：**生成器搬进新仓**（它产出的是插件代码，属插件域）。新仓以一个**构建期依赖**接上 analyzer：直接 `import xueqiu_analyzer.waf`（新仓可以做正经 Python 包，不需要 crawler 那个靠同级 checkout 的 `waf_bridge`）。CI 里 checkout / 安装 analyzer。
- 不推荐：留在 crawler 里改 `ADAPTERS_DIR` 指新仓 —— 让 A 仓往 B 仓写文件，跨仓写是本次要消除的那类耦合。
- ⚠️ 关键不变量：`xueqiu_analyzer.waf` 仍是**风控判定的唯一实现**，插件侧只能是「由它生成」的副本。别为了解耦把模式拷贝成插件自带的事实来源。

### D4. 测试怎么切
按「这条不变量归谁」切，不按文件在哪个目录切：

| 测试 | 断言什么 | 归属 |
|---|---|---|
| `test_opencli_adapter_plugin_integrity.py` | 插件自身结构（入口导出、`lib/` 完整、`onBeforeExecute(` 字样） | **新仓** |
| `test_opencli_adapter_symbols.py` | 符号归一化（`lib/xueqiu-symbol.js`） | **新仓** |
| `test_opencli_adapter_domains.py` | 域名别名表（换名 = 排期清零） | **新仓** |
| `test_opencli_throttle.py` | 其中「插件内置站点策略声明了 `web` 且 ≥6 秒」那半 | **新仓** |
| 同上文件的「调用方超时盖得住插件最坏等待」那半 | 插件常量 **vs** crawler 调用方超时 | **留 crawler**（跨方契约） |
| `test_opencli_command_names.py` | 插件命令名 **vs** crawler 的 `SITE_OPERATIONS` | **留 crawler**（跨方契约） |
| `monitor/tests/test_opencli_contract.py` | 插件常量 **vs** monitor/analyzer 超时 | **留 monitor**（跨方契约） |

- **推荐**：把「解析插件源码的 Python 测试」原样搬进新仓（它们是用血换来的，重写成 JS 是纯风险）。新仓因此同时有 JS 源码 + 一套 Python 测试 + `pyproject.toml`（ruff/pytest）。这点略微不常规，但比重写划算，且 CI 成本很低。
- 跨方契约测试留在消费方，它们需要插件源码 ⇒ 见 D5。

### D5. 消费方怎么拿到插件源码（跨方契约测试的前提）
今天 crawler 的 CI 里插件「恰好在检出目录里」；搬家后这个隐式前提消失。

- **推荐**：参照 crawler 已有的 analyzer 做法 —— CI 里多 checkout 一个仓到 `_deps/opencli-adapters`，测试里 `PLUGIN_DIR` 先试 `_deps/` 再退回同级 checkout（本地开发用）。crawler 的 `.github/workflows/tests.yml` 已有完全相同的先例（`repository: winterswang/xueqiu-analyzer-skill`，`path: _deps/...`），照抄即可。monitor 侧同理（它现在因为看不到 crawler 而**整条测试跳过**，改完反而能真正生效）。
- 备选（更干净、但多一步）：插件仓生成 `contract.json`（命令名、错误码、超时预算数值），消费方测试读它，不必检出源码。**建议作为后续改进**，本次先用 checkout。

### D6. 发版与版本号
`opencli-plugin.json` 现在是 `1.0.0`，且这个版本号**没人读、没人校验**（`install.sh` 只取 `name`）。

- **推荐**：本次仅建立纪律 —— 改动插件行为时 `opencli-plugin.json` 的 `version` 递增，并在 PR 描述里写明；给新仓打 tag 便于对照。不引入自动发版流水线（没有消费者按版本安装，机器级符号链接也不认版本）。

### D7. 迁移期的冻结窗口
迁移期间如果别的会话还在改插件（**这真实发生过**：2026-10-12 有另一会话在工作区里改了 `search.js` 62 行），搬动会打架。

- **推荐**：开工前 `git status` 确认 crawler 工作区里 `opencli-adapters/` 是干净的；有在制品就先让它提交。整个迁移在一个连续的时间窗内做完（§4 的 S1–S6 大约一小时量级），期间不改插件源码。

---

## 4. 分步实施（每步独立可验证、可回滚）

> 前置纪律：**做之前先整目录备份** —— `cp -a opencli-adapters /tmp/oa-backup-$(date +%F)`。
> 插件是符号链接的活代码，一旦限速/台账整层消失，症状会延后到下一次抓取才显现。

| 步 | 内容 | 判据（做完立刻验） | 回滚 |
|---|---|---|---|
| **S0** | 冻结：`git status` 确认 `opencli-adapters/` 干净；备份目录；记下当前 `opencli list` 输出与 `~/.opencli/*-throttle.json` 文件清单 | 工作区无该目录改动；备份存在 | 无需 |
| **S1** | 建新仓：`git subtree split -P opencli-adapters -b plugin-only`；在 `~/code/claude_code/opencli-adapters/` 初始化并推入（本地可先不推远端） | 新仓 `git log` 有 10 个提交；`git ls-files` 含 7 个命令文件 + `lib/` + 两个 `.sh` | 删新目录 |
| **S2** | 新仓立测试与 CI：搬 §3-D4 表里「归新仓」的测试；补 `pyproject.toml`（ruff + pytest）、`.gitignore`（`node_modules/`，见现有同名文件）、`tests.yml` | 新仓 `ruff check . && ruff format --check . && pytest` 全绿 | revert |
| **S3** | 搬 WAF 生成器：`sync_waf_patterns.py` + 对应测试进新仓，改为直接 import `xueqiu_analyzer.waf`；新仓 CI 里 checkout/安装 analyzer | `python3 scripts/sync_waf_patterns.py --check` 退出码 0（**证明生成结果逐字节一致**） | revert |
| **S4** | 消费方改造：crawler 删掉已搬走的测试、把留下的跨方契约测试的 `PLUGIN_DIR` 改成「先 `_deps/` 再同级」；`.github/workflows/tests.yml` 增 checkout 新仓；monitor 的 `test_opencli_contract.py` 同样改 | crawler/monitor 各自 `pytest` 全绿（monitor 那条应从「跳过」变成「真跑」） | revert |
| **S5** | **Cutover**：新仓位置跑 `./install.sh`（它内部 `uninstall` + `install file://新目录`），符号链接随之重指 | ① `ls -la ~/.opencli/plugins/` 指向新目录；② `opencli list 2>&1 \| grep -iE "plugin\|failed\|duplicate\|not defined"` **无输出**；③ `opencli list` 里 7 条命令全在；④ `ls ~/.opencli/*-throttle.json` 与 S0 记的清单**逐条相同**（换名就说明域名键被改坏了） | 在旧目录重跑 `install.sh` 即可回退 |
| **S6** | 清理与文档：crawler 里 `git rm -r opencli-adapters`（**只在 S5 验证通过之后**）；改 §1.3 表里第 7/8/9 项的路径引用（SKILL、各仓 docstring、memory 分片）；新仓补 README 首段说明与消费方清单 | `grep -rn "opencli-adapters" crawler monitor analyzer` 剩下的引用**都是**指向新仓的、且测试全绿 | 从备份恢复 |
| **S7** | 观察期：等一次真实的定时抓取（次日 08:00 那轮）跑完，看 `adapter-calls.jsonl` 有新行、限速等待正常 | 报告里「管理外流量」「真实并发」区块仍有数据；无 `Failed to load module` | — |

**顺序不可换的两处**：
- S2/S3 必须早于 S5 —— 新位置必须先能自证健康，才动符号链接。
- S6 的 `git rm` 必须晚于 S5 验证 —— 否则一旦新位置有问题，旧源码已经不在树上。

---

## 5. 验证方案（每条给命令与判据）

**A. 等价性（最关键，防行为突变）**
```bash
# ① 插件仍被 opencli 正常加载（一个导出名写错就会静默失效，只留一条 warn）
opencli list 2>&1 | grep -iE "plugin|failed|duplicate|not defined"   # 期望：无输出
# ② 7 条命令全在（这是 install.sh 自己用的校验清单，可复用其 awk 逻辑）
./install.sh                                                     # 期望：7 行 OK、无 MISS、退出码 0
# ③ 限速状态文件没被换名 —— 换名 = 排期清零
ls ~/.opencli/*-throttle.json                                    # 与 S0 记录的清单逐条相同
```
**② 限速行为**：跑一次 `OPENCLI_RATELIMIT_DEBUG=1 OPENCLI_VERBOSE=1 opencli xueqiu news --help`
（两个 debug 开关都要开），断言状态文件推进、`adapter-calls.jsonl` 恰好新增一行且字段齐全。

**③ 台账与报告链**：`python3 scripts/analyze_opencli_usage.py --hours 24`
—— 插件台账、限速等待、真实并发、管理外流量四个区块都要有数据（不是「没有记录」）。

**④ 消费方回归**：crawler `ruff check . && ruff format --check . && pytest tests/ -q`；
monitor `TZ=Asia/Shanghai pytest tests/ -q`；analyzer `pytest tests/ -q`。
**这三条全绿才算完**，其中 monitor 的 `test_opencli_contract.py` 应从跳过变为真跑。

**⑤ 绝不**在验证里打真实站点页面（幂等性 + 风控安全）：只用 `--help`、本地命令、造数据。

---

## 6. 回滚

| 阶段 | 回滚动作 | 代价 |
|---|---|---|
| S1–S4（未动符号链接） | 删新目录 + `git revert` 消费方改动 | 零风险，生产无感 |
| S5 之后 | 回到旧目录重跑 `./install.sh`（软链重指旧源码）；`git revert` S6 | 分钟级；`~/.opencli/` 状态文件自始至终没动，排期与台账不丢 |
| S6 之后 | 从 `/tmp/oa-backup-*` 恢复目录 + 重跑 `install.sh` | 略繁琐，但仍可逆 |

**唯一不可逆的是改 `~/.opencli/` 里的文件名** —— 本计划明确不做。

---

## 7. 风险与盲区

| # | 风险 | 说明与对策 |
|---|---|---|
| R1 | **符号链接指向新路径的窗口期** | S5 的 `uninstall` + `install` 之间插件短暂不可用。不要在定时抓取窗口（crawler 08:00、monitor 分时段 pipeline、analyzer 02:00）附近做。 |
| R2 | **忘了改某处路径引用** | §1.3 表就是清单。S6 用 `grep -rn "opencli-adapters"` 三个仓过一遍，确认剩余引用都指新仓。 |
| R3 | **生成器搬家后与 analyzer 漂移** | S3 的 `--check` 退出码是硬判据；并在新仓 CI 里常跑（今天这条只在 crawler 的 pytest 里）。 |
| R4 | **消费方 CI 拿不到新仓** | 新仓若是私有，crawler/monitor 的 workflow 需要凭据。**推荐新仓 public**（与 analyzer 一致，`tests.yml` 里注释已写明「analyzer 已 public，跨仓库 checkout 用默认凭据即可，无需 PAT」）。 |
| R5 | **crawler CI 的隐式依赖** | 今天 crawler 的 5 个插件测试靠「插件恰好在检出目录里」通过；S4 若漏改 workflow，CI 会在**别人的 PR 上**突然变红。S4 完成后本地就要全绿。 |
| R6 | **别人正在改同一个插件** | 见 D7。真实发生过，务必先冻结。 |
| R7 | **新仓同时是 Python 与 Node 项目** | 略不常规（JS 源码 + Python 测试 + 生成器）。若将来想统一，另开一次改动；本次不做。 |
| R8 | **插件 id 与仓库名长期不一致** | `xueqiu-adapters` 这个名字已经名不副实（还管 xiaohongshu 与通用 web）。记为已知遗留，见 D1。 |

---

## 8. 给新会话的启动提示（可直接粘贴）

> 读 `xueqiu-crawler/docs/opencli_adapters_extraction_plan.md`，按它把 `opencli-adapters`
> 抽成独立仓库。**先做两件事**：① 读第 1 节确认尽调没变（尤其 `git log -- opencli-adapters`
> 的提交数、以及 `git status` 里插件目录是否干净）；② 把第 3 节的 7 个决策点逐条和我确认，
> 尤其是 D1（插件 id 改不改）、D4（测试怎么切）、D5（消费方怎么拿契约）。
> 然后按第 4 节 S0→S7 走，**S2/S3 必须早于 S5**、**S6 的 `git rm` 必须晚于 S5 验证**，
> 每步做完按第 5 节验一遍再进下一步。交付沿用既有流程：分支 → 质量门 → PR → 等 CI 绿 →
> code review → squash 合并（三个仓各一个 PR，或按仓拆开）。
