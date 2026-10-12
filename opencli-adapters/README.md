# opencli-adapters — 雪球 / 小红书适配器（版本化插件）

雪球、小红书抓取用的 **opencli 本地适配器**。以 opencli 官方 **plugin** 形式安装，
不再依赖 `~/.opencli/clis/` 裸覆盖 —— 后者会被 opencli 升级 / smart-sync 覆盖或删除，
2026-09-30 已实际发生过一次，导致适配器丢失。

## 为什么用 plugin 而不是 `~/.opencli/clis/`

opencli 的启动顺序是：

```
discoverClis(BUILTIN_CLIS) → discoverClis(USER_CLIS) → discoverPlugins()
```

源码注释写明：*"discoverPlugins runs last: plugins may override both built-in and user CLIs."*
（`dist/src/discovery.js`、`dist/src/main.js`）

所以 plugin 是**官方支持的覆盖通道**，且 `~/.opencli/plugins/` 属于用户目录，
不参与内置适配器的同步。本地安装走 `file://`，opencli 会建**符号链接**指向本目录，
改这里立刻生效（`dist/src/plugin.js: installLocalPlugin`）。

## 文件 → 站点映射

适配器文件名与 `cli({ site, name })` 里的 `name` 一致；`site` 决定归属：

| 文件 | site | 命令 | 来源 |
| --- | --- | --- | --- |
| `news.js` | xueqiu | `opencli xueqiu news` | 自研（官方基线无此命令） |
| `replies.js` | xueqiu | `opencli xueqiu replies` | 自研（官方基线无此命令） |
| `stock-notices.js` | xueqiu | `opencli xueqiu stock-notices` | 自研（官方基线无此命令） |
| `user-articles.js` | xueqiu | `opencli xueqiu user-articles` | 自研（官方基线无此命令） |
| `comments.js` | xueqiu | `opencli xueqiu comments` | 覆盖官方基线：3 处 `xueqiu.com` → `www.xueqiu.com`（风控页/405 规避） |
| `search.js` | xiaohongshu | `opencli xiaohongshu search` | 覆盖官方基线：DOM 抓取 + `xsec_token` 直出，只支持 `--sort comprehensive` |
| `article.js` | **web**（通用） | `opencli web article` | 自研；读取任意网页正文（跨站选择器链），输出 markdown |
| `utils.js` | — | — | **非命令模块**，仅供 `stock-notices.js` 相对引用；是官方基线 `clis/xueqiu/utils.js` 的副本 |
| `lib/*.js` | — | — | **非命令模块**，被 `rate-limit.js` import：`paths` / `env` / `util` / `lockfile` / `policy` / `domain-tables` / `domains` / `ratelimit` / `concurrency` / `requestlog` |
| `rate-limit.js` | —（全站点） | — | **非命令模块**：注册 `onBeforeExecute` 钩子，对**走 registry 的** opencli 调用统一跨进程限速（`browser` 子命令例外、通用站 `web` 按目标域名分流，均见下节） |

> `utils.js` 只导出 `formatChinaDate` / `stripHtml` / `fetchXueqiuJson`。
> 因为 `@jackwener/opencli/clis/...` 不在包的 `exports` 里，无法深层 import，只能随插件携带。
> **官方升级后若改动了这个文件，需要手动同步**（见下方漂移检查）。

## 浏览器操作的统一治理：限频 + 并发 + 请求台账

入口是 `rate-limit.js`（插件目录里**唯一**注册钩子的文件），实现在 `lib/` 下。
`lib/*` 不会被 opencli 的 discovery 扫到（它只平铺读顶层 `.js`），必须由入口 import。

### ⚠️ 覆盖面：先看这张表，别以为它管住了「所有浏览器操作」

| 通道 | 限频 | 并发 | 台账 |
| --- | --- | --- | --- |
| 所有 `cli()` 注册命令（`xueqiu/*`、`xiaohongshu/*`、`web/article`） | ✅ | ✅ | ✅ |
| 手敲 `opencli …`、其它脚本的裸 `subprocess` | ✅ | ✅ | ✅（`source_kind: manual`） |
| `opencli doctor` / `list` / `--help` / update-check | ❌ | ❌ | ❌（本地探测，不经 `executeCommand`） |
| **`opencli browser …`** | ❌ | ❌ | ❌ |
| urllib / curl / playwright / nodriver 直连 | ❌ | ❌ | ❌ |

两处「❌」的分量不同，别混为一谈：

- **`opencli browser …`**：钩子的唯一触发点在 `executeCommand`
  （`dist/src/execution.js:206`），而 `browser` 命令直接挂 commander、经 daemon 发指令，
  **不触发任何钩子**。`browser open` 会真导航到目标站点，是唯一有实际风险的缺口；
  已提上游 [jackwener/opencli#2606](https://github.com/jackwener/OpenCLI/issues/2606)。
  当前生产不受影响（`web article` 已注册，`_attempt_via_browser` 只在该命令缺失时才走），
  但**台账里 `browser:*` 不计入域名密度**，排查时要知道。
- **非 opencli 通道**（monitor 直下公告 PDF、`curl` 打 sec.gov、智谱 reader、nodriver
  直连抓取）：根本不经过 opencli，本插件够不着。

### 频率控制：按**域名**，不按 opencli 站点键

风控看的是「打同一个域名的请求总量」，而站点键只是实现细节：
`xueqiu/user-articles`（列表）与 `web/article`（通用站的详情页）打的是同一个
`xueqiu.com`，必须共用一份额度；反过来，同样走 `web/article` 的 cninfo/hkexnews
公告页不该被算进雪球的账。

- 解析：`lib/domains.js` 的 `resolveDomainKey` → 站点默认域名 / 参数里的 URL →
  别名表 → 多级后缀规则 → 末两段兜底。解析不出就退回站点键（**不是**不限速）。
- 查找链：**域名键 → 站点键 → 通配**。站点那一步不能省，否则
  `web article` 抓 cninfo（域名键 `cninfo.com.cn` 无专门配置）会变成「未声明即不限速」。
- 状态：`~/.opencli/<域名键>-throttle.json`。
- ⚠️ **别名表必须把已有键映射回自己**（`xueqiu.com` → `xueqiu`，不是 `'xueqiu.com'`）：
  状态文件名由键直接决定，换名等于限速排期清零、下一次调用零间隔打出去。
- ⚠️ 内置档的下限**不得低于 6 秒**：`scripts/schedule-checks/peak.mjs` 用
  「6 秒下限 ⇒ 一分钟不可能超过 10 次」判定「限速器被绕过」，低于 6 秒会让合法的
  批量公告下载被误报成绕过。

### 并发闸（`lib/concurrency.js`）—— 当前是**观测模式**

限频只管「起始时刻的间隔」，管不住**同时在飞的数量**：一个调用睡完 6–12 秒才开始跑，
而跑完要 10–20 秒；5 组 monitor pipeline 又各自没有锁，单组超时就会与下一组叠加。

- 槽位：`~/.opencli/inflight/<域名键>/<token>.slot`（`ls` 就能看谁在飞）。
  临界区用同一把 `.gate.lock` 互斥。超限时**有界排队**，超预算则放行并记
  `overflowed: true`（与限速器同一套 fail-open 哲学）。
- **默认 `concurrency` 缺省 = 只记录不拦**：照常占位（不占就量不出真实并发）、照常
  释放，但不等待、不拒绝。目的是先拿到真实并发分布再拍上限。
- 泄漏：`onAfterExecute` 在成功与抛错两条路径都会触发，所以只有**进程被 KILL** 才漏；
  槽带 `expires_at`，由后续调用顺手 sweep 回收（`dianping-throttle.json.lock` 就是
  一次真实残留，所以这是必需项而非锦上添花）。
- 🚧 **启用闸门（`concurrency: 1/2/3`）时必须同时**把调用方超时从 120s 抬到 ~150s
  （`scripts/opencli_extractor.py`、`xueqiu-monitor/src/detail_fetcher.py`）：排队等待
  发生在子进程内、会被 `subprocess.run` 一并计时，最坏 = 87 + 2 + 20 + 10 ≈ 119s。

### 请求台账：两份，各管一段，别混

| 台账 | 写的人 | 权威内容 |
| --- | --- | --- |
| `~/.opencli/xueqiu-calls.jsonl` | **Python 调用方**（`record_opencli_call`） | 调用方意图与结局：`source`/`caller`、`expect_miss`、stderr 摘要、**以及插件看不见的 `browser:*`** |
| `~/.opencli/adapter-calls.jsonl` | **本插件**（`lib/requestlog.js`） | 传输层真相：`domain_key`、限频等待/长停、被跳过原因、并发几路、是否 fail-open 放行、手敲与第三方调用 |

join 方式：`adapter-calls.jsonl` 的 `ppid` 就是调用方 Python 进程的 pid（opencli 是
无 shell 包装的 node 直启脚本）；调用方若设了 `XUEQIU_OPENCLI_CALL_ID` 则优先按
`call_id` 1:1 对上。

实现约束（改 `lib/requestlog.js` 前先读）：**禁止写 stdout/stderr**（调用方把 opencli
输出当 JSON 解析）；**写失败一律吞掉、绝不上抛** —— 钩子里抛异常会被 `emitHook` 静默
fail-open，**连限速一起失效**。

### 配置与开关

**配置** `~/.opencli/rate-limits.json`（可省略，省略时用内置表）：

```json
{
  "defaults": { "enabled": true, "min_delay_s": 3, "max_delay_s": 8 },
  "sites": {
    "xiaohongshu": { "min_delay_s": 15, "max_delay_s": 15 },
    "xueqiu": { "min_delay_s": 6, "max_delay_s": 12,
                "long_pause_every": 30, "long_pause_min_s": 45, "long_pause_max_s": 75 },
    "web":    { "min_delay_s": 6, "max_delay_s": 8 }
  },
  "domains": {
    "cninfo.com.cn": { "min_delay_s": 6, "max_delay_s": 8 }
  }
}
```

- 键可覆盖 `enabled` / `min_delay_s` / `max_delay_s` / `long_pause_*` /
  `skip_commands` / `state_path` / `concurrency` / `concurrency_enabled`；
  `sites` 按站点键、`domains` 按域名键，域名优先；`sites["*"]` 为通配。
- **未声明的键不限速**（宁可漏，也不误伤本地命令）。

| 环境变量 | 作用 |
| --- | --- |
| `OPENCLI_RATELIMIT_DISABLED=1` | 关掉整套限速（真值 = 关闭） |
| `OPENCLI_RATELIMIT_<域名键或站点键>_MIN/MAX_DELAY_SECONDS` | 按域名或站点覆盖间隔 |
| `XUEQIU_OPENCLI_THROTTLE=0` | 只关 xueqiu（含打 xueqiu.com 的 `web` 调用） |
| `XUEQIU_OPENCLI_MIN/MAX_DELAY_SECONDS`、`XUEQIU_OPENCLI_LONG_PAUSE_*` | xueqiu 专用数值覆盖 |
| `XUEQIU_OPENCLI_THROTTLE_STATE` | 覆盖 xueqiu 的状态文件路径 |
| `OPENCLI_ADAPTER_LOG` | 插件台账路径；`off` 关闭；验证时指到 `/tmp` 可隔离 |
| `XUEQIU_OPENCLI_INFLIGHT_DIR` | 并发槽根目录（验证时隔离用） |
| `XUEQIU_OPENCLI_CONCURRENCY_WAIT_MS`、`XUEQIU_OPENCLI_INFLIGHT_TTL_MS` | 排队预算 / 槽存活上限 |
| `OPENCLI_RATELIMIT_DEBUG=1` + `OPENCLI_VERBOSE=1` | 打印每次等待（**两个都要设**） |
| `XUEQIU_CALL_SOURCE`、`XUEQIU_OPENCLI_CALL_ID` | 调用方身份（调用方设，插件读取记进台账） |

**因此 Python 侧的限速器已让位**：`xueqiu-analyzer-skill/.../opencli_rate_limiter.py`
默认关闭（`XUEQIU_OPENCLI_THROTTLE=1` 可重新启用做回退），否则会与新钩子叠加成双重等待。
FrontierAgent 的 `plugins/tools/_platform_cli.py` 同理，只保留自己的跨进程锁与失败退避。

> 走回退档时**必须同时关掉插件层**：`XUEQIU_OPENCLI_THROTTLE=1 OPENCLI_RATELIMIT_DISABLED=1`。
> 两个实现的锁机制不互斥 —— Python 用 `fcntl.flock` 且从不删锁文件，本模块用
> 「独占创建 + 释放时删除」锁**同一个路径**（`xueqiu-throttle.json.lock`）。

### 为什么这里有一个通用站命令 `web article`

`article.js` 注册在 **`web`** 这个自建的通用 site 下，不属于雪球也不是小红书 —— 它读的是**任意网页的正文**。
放在本插件里，是因为它的两个调用方都在本项目里，且它的行为规则来自本仓库：

- `xueqiu-crawler` 抓雪球文章正文；
- `xueqiu-monitor` 抓新闻详情与公告页，**域名是混的**（实测 `announcements` 表：xueqiu.com 4429、
  cninfo.com.cn 1035、hkexnews.hk 61、sseinfo.com 21 —— 公告多为 PDF 走本地 pymupdf，
  其余 HTML 页走这条命令）。

它的正文容器链是**跨站实测**出来的（新浪 `div.article`、东财 `#ContentBody`、雪球 `article`），
风控词表也是通用的中文风控页短语，所以没有按站拆开。原先这套逻辑在
`xueqiu-crawler/scripts/opencli_extractor.py` 与 `xueqiu-monitor/src/detail_fetcher.py`
各写了一份，2026-10-08 合并到这里（见「已知取舍」第 3 条）。

正文 markdown 由 `@jackwener/opencli/utils` 的 `htmlToMarkdown` 生成 —— 与 `opencli browser extract`
用的是同一个转换，去噪 JS 也逐行照抄该命令的实现，**两边输出等价**。改这里时不要自写转换。

## 安装

```bash
./install.sh
```

脚本做三件事：安装（或重装）插件、校验命令已注册、打印 `opencli adapter status`。

等价的手工命令：

```bash
opencli plugin uninstall xueqiu-adapters 2>/dev/null || true
opencli plugin install "file://$PWD"
opencli list | grep -E 'xueqiu|xiaohongshu'
```

## 升级 opencli 之后

插件不受影响（它在 `~/.opencli/plugins/`，不在被同步的 `clis/` 下）。但要做一次漂移检查，
确认我们覆盖的两个官方适配器是否已被上游修好 —— 修好了就该删掉对应文件、改用基线：

```bash
./check-drift.sh
```

## 已知取舍

- **`search.js` 钉住了一个旧式适配器**。官方基线的 `xiaohongshu/search.js` 已重写为
  带 MutationObserver 的新版，我们的覆盖是旧的 DOM 抓取实现。当前可用，
  但上游若修掉了旧版的痛点，应优先 `opencli adapter reset xiaohongshu` 回到基线。
- **`comments.js` 的 www 改写**若被上游采纳，本覆盖即可删除。
- 本插件与 `scripts/opencli_extractor.py`（Python 侧调用方）配套，
  改命令签名时要同时改那边。
- **`article.js` 的去噪 JS 是 `browser extract` 的副本**。`buildExtractHtmlJs` 不在包的
  `exports` 里（只有 `./utils` 在），无法 import，只能在适配器内联同款实现。
  **上游若改动那段（`dist/src/browser/extract.js` 的 `drop` 列表 / 属性剥离），这里要手动同步** ——
  不同步不会报错，只会让正文 markdown 与 `browser extract` 悄悄产生差异。
- **`article.js` 保留了旧的 `opencli browser` 直连路径作回退**（两个 Python 调用方各留一份）。
  仅在 `opencli web article --help` 探测失败（命令未注册 / 插件丢失）时才走，
  命令存在但执行报错**不会**回退，避免坏页面静默绕开适配器。回退代码在
  `scripts/opencli_extractor.py::_attempt_via_browser` 与
  `src/detail_fetcher.py::_fetch_page_opencli_legacy`。
