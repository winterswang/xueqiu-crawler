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

# 3. 发送飞书推送（通过标记文件）
echo "[3/3] 准备飞书推送..." >> "$LOG_FILE"

REPORT_FILE="$PROJECT_DIR/data/daily_reports/$DATE.md"
if [ -f "$REPORT_FILE" ]; then
    # 创建推送标记文件，由心跳检测发送
    /usr/bin/python3 << 'PYEOF'
import json
import os

# 读取报告
report_file = "/root/.openclaw/workspace/xueqiu-crawler/data/daily_reports/" + os.popen('date +%Y-%m-%d').read().strip()

try:
    with open(report_file, 'r') as f:
        content = f.read()
    
    # 读取文章总数
    with open('/root/.openclaw/workspace/xueqiu-crawler/data/index.json', 'r') as f:
        index = json.load(f)
        total = len(index.get('articles', {}))
    
    # 截取前 3500 字符
    summary = content[:3500]
    
    # 写入待发送文件
    pending_file = "/tmp/pending_feishu_daily.json"
    with open(pending_file, 'w') as f:
        json.dump({
            "channel": "feishu",
            "message": f"📊 价值投资日报 - {os.popen('date +%Y-%m-%d').read().strip()}\n\n今日新增文章，共 {total} 篇\n\n{summary}\n\n---\n完整报告已保存到服务器。\n\n*分析模型：GLM-5 (qwen-plus)*"
        }, f)
    
    print(f"推送文件已创建: {pending_file}")
    
except Exception as e:
    print(f"创建推送文件失败: {e}")
PYEOF

    echo "飞书推送标记文件已创建" >> "$LOG_FILE"
else
    echo "警告: 未找到今日报告文件" >> "$LOG_FILE"
fi

echo "[$(date)] 流程执行完成" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"