# 雪球专栏文章爬虫

自动爬取指定雪球用户的专栏文章，保存为 Markdown 格式。

## 功能

- ✅ Playwright 模拟真实浏览器，绕过人机验证
- ✅ 自动过滤专栏文章，排除评论和短状态
- ✅ Markdown 格式保存，方便阅读和管理
- ✅ 增量更新，避免重复爬取
- ✅ 支持多账号配置
- ✅ 详细的日志记录

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

## 配置

### 账号配置 (config/accounts.yaml)

```yaml
accounts:
  - id: "5739488179"
    name: "Elon翻开每一页"
    url: "https://xueqiu.com/u/5739488179"
    enabled: true
```

### 爬虫配置 (config/config.yaml)

```yaml
crawler:
  delay_min: 2
  delay_max: 5
  max_articles: 20
  headless: true
```

## 使用

```bash
# 爬取所有配置的用户
python scripts/crawler.py

# 爬取指定用户
python scripts/crawler.py -u 5739488179
```

## 输出

```
data/
├── 5739488179/
│   ├── 377536268.md
│   └── ...
├── 6308001210/
│   └── ...
└── index.json          # 索引文件
```

## 定时任务

```bash
# 添加到 crontab
0 2 * * * cd /path/to/xueqiu-crawler && python scripts/crawler.py >> logs/cron.log 2>&1
```

## 已验证账号

| ID | 名称 | 状态 |
|------|------|------|
| 5739488179 | Elon翻开每一页 | ✅ |
| 6308001210 | - | ⏳ |
| 6249637706 | - | ⏳ |