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
| `utils.js` | — | — | **非命令模块**，仅供 `stock-notices.js` 相对引用；是官方基线 `clis/xueqiu/utils.js` 的副本 |

> `utils.js` 只导出 `formatChinaDate` / `stripHtml` / `fetchXueqiuJson`。
> 因为 `@jackwener/opencli/clis/...` 不在包的 `exports` 里，无法深层 import，只能随插件携带。
> **官方升级后若改动了这个文件，需要手动同步**（见下方漂移检查）。

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
