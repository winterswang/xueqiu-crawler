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

> `utils.js` 只导出 `formatChinaDate` / `stripHtml` / `fetchXueqiuJson`。
> 因为 `@jackwener/opencli/clis/...` 不在包的 `exports` 里，无法深层 import，只能随插件携带。
> **官方升级后若改动了这个文件，需要手动同步**（见下方漂移检查）。

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
