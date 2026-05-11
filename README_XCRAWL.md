# 雪球爬虫 XCrawl 版本

基于 XCrawl API 的云端爬取方案，替代本地 Playwright 版本。

## 优势对比

| 维度 | Playwright 版 | XCrawl 版 |
|------|---------------|-----------|
| 爬取时间 | 7-15 分钟 | 3-5 分钟 |
| 本地资源 | Chromium ~500MB | 无需浏览器 |
| 稳定性 | 本地依赖 | 云端服务 |
| 反爬能力 | 自建脚本 | 内置代理 |
| 成本 | 免费 | 500-800 credits/月 |

## 安装

```bash
# XCrawl 配置
mkdir -p ~/.xcrawl
echo '{"XCRAWL_API_KEY": "your-api-key"}' > ~/.xcrawl/config.json

# 依赖
pip install requests pyyaml
```

## 使用

### 爬取单个用户

```bash
# 仅爬取文章列表
python3 scripts/crawler_xcrawl.py -u 5739488179 -m 20

# 爬取文章列表 + 详情
python3 scripts/crawler_xcrawl.py -u 5739488179 -m 20 --detail
```

### 爬取所有用户

```bash
# 仅列表
python3 scripts/crawler_xcrawl.py --all -m 20

# 列表 + 详情
python3 scripts/crawler_xcrawl.py --all -m 20 --detail
```

### 检查配置

```bash
python3 scripts/crawler_xcrawl.py --check
```

## 登录态管理

```bash
# 检查 cookies 状态
python3 scripts/cookies.py --check

# 手动导入 cookies
python3 scripts/cookies.py --import "name=value; name2=value2"
```

## 定时任务

使用新版脚本：

```bash
# 替代 run_daily.sh
bash scripts/run_daily_xcrawl.sh
```

或更新 crontab：

```cron
0 2 * * * cd /root/.openclaw/workspace/xueqiu-crawler && python3 scripts/crawler_xcrawl.py --all -m 20
```

## 文件结构

```
xueqiu-crawler/
├── scripts/
│   ├── crawler_xcrawl.py      # XCrawl 版本爬虫
│   ├── cookies.py             # 登录态管理
│   └── run_daily_xcrawl.sh    # 完整流程脚本
├── config/
│   ├── accounts.yaml          # 账号配置
│   ├── config.yaml            # 爬虫配置
│   └── xueqiu_cookies.json    # 保存的 cookies
└── data/
    ├── {user_id}/             # 每个用户的文章
    └── index.json             # 索引文件
```

## 与 Playwright 版本兼容

- 输出数据格式完全一致
- 共用同一套 index.json 索引
- 可切换使用（XCrawl 失败时自动 fallback）

## 成本估算

| 操作 | Credits | 说明 |
|------|---------|------|
| 文章列表 | 1/账号 | sync + json |
| 文章详情 | 1-2/篇 | sync + markdown |
| 每日爬取 | ~500 | 11 账号 × 20 篇 |

## 注意事项

1. XCrawl 需要有效的 API Key
2. 部分账号可能需要登录态才能看到完整内容
3. 建议先用单个账号测试，确认正常后再批量爬取