#!/bin/bash
# 雪球爬虫完整流程：爬取 -> 分析 -> 推送

set -e

PROJECT_DIR="/root/.openclaw/workspace/xueqiu-crawler"
LOG_FILE="$PROJECT_DIR/logs/cron.log"
DATE=$(date +%Y-%m-%d)

echo "========================================" >> "$LOG_FILE"
echo "[$(date)] 开始执行雪球爬虫流程" >> "$LOG_FILE"

cd "$PROJECT_DIR"

# 1. 爬取新文章
echo "[1/3] 爬取新文章..." >> "$LOG_FILE"
/usr/bin/python3 scripts/crawler.py >> "$LOG_FILE" 2>&1

# 2. 生成分析报告
echo "[2/3] 生成分析报告..." >> "$LOG_FILE"
export BAILIAN_API_KEY="sk-727ad633253f477d84255d434826aabd"
/usr/bin/python3 scripts/generate_report.py >> "$LOG_FILE" 2>&1

# 3. 发送飞书推送
echo "[3/3] 发送飞书推送..." >> "$LOG_FILE"

REPORT_FILE="$PROJECT_DIR/data/daily_reports/$DATE.md"
if [ -f "$REPORT_FILE" ]; then
    # 使用 Python 发送飞书消息
    /usr/bin/python3 << 'PYEOF'
import json
import urllib.request
import os

# 读取报告
report_file = "/root/.openclaw/workspace/xueqiu-crawler/data/daily_reports/$(date +%Y-%m-%d).md"
date = os.popen('date +%Y-%m-%d').read().strip()

try:
    with open(report_file, 'r') as f:
        content = f.read()
    
    # 读取文章总数
    with open('/root/.openclaw/workspace/xueqiu-crawler/data/index.json', 'r') as f:
        index = json.load(f)
        total = len(index.get('articles', {}))
    
    # 截取前 3500 字符
    summary = content[:3500]
    
    # 构建消息
    message = f"""📊 价值投资日报 - {date}

今日新增文章，共 {total} 篇

{summary}

---
完整报告已保存到服务器。
"""
    
    # 发送到 OpenClaw 消息接口（飞书）
    req = urllib.request.Request(
        "http://127.0.0.1:3000/api/message",
        data=json.dumps({
            "action": "send",
            "channel": "feishu",
            "message": message
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())
        print(f"飞书推送成功")
        
except Exception as e:
    print(f"飞书推送失败: {e}")
PYEOF

    echo "飞书推送完成" >> "$LOG_FILE"
else
    echo "警告: 未找到今日报告文件" >> "$LOG_FILE"
fi

echo "[$(date)] 流程执行完成" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"