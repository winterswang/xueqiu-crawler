#!/bin/bash
# 雪球爬虫完整流程：爬取 -> 分析 -> 提交Gist -> 发送链接

set -e

PROJECT_DIR="/root/.openclaw/workspace/xueqiu-crawler"
LOG_FILE="$PROJECT_DIR/logs/cron.log"
DATE=$(date +%Y-%m-%d)

echo "========================================" >> "$LOG_FILE"
echo "[$(date)] 开始执行雪球爬虫流程" >> "$LOG_FILE"

cd "$PROJECT_DIR"

# 1. 爬取新文章
echo "[1/4] 爬取新文章..." >> "$LOG_FILE"
/usr/bin/python3 scripts/crawler.py >> "$LOG_FILE" 2>&1

# 2. 生成分析报告（使用新的分析器）
echo "[2/4] 生成分析报告..." >> "$LOG_FILE"
export BAILIAN_API_KEY="sk-727ad633253f477d84255d434826aabd"
/usr/bin/python3 scripts/generate_report.py --limit 20 >> "$LOG_FILE" 2>&1

# 3. 提交报告到 Gist
echo "[3/4] 提交报告到 Gist..." >> "$LOG_FILE"

REPORT_FILE="$PROJECT_DIR/data/daily_reports/$DATE.md"
if [ -f "$REPORT_FILE" ]; then
    GIST_URL=$(/usr/bin/python3 << 'PYEOF'
import json
import urllib.request
import os

# 读取报告
report_file = "/root/.openclaw/workspace/xueqiu-crawler/data/daily_reports/" + os.popen('date +%Y-%m-%d').read().strip()
date = os.popen('date +%Y-%m-%d').read().strip()

with open(report_file, 'r') as f:
    content = f.read()

# 获取 GitHub Token
token = os.environ.get("GITHUB_TOKEN", "")
if not token:
    with open("/root/.bashrc", "r") as f:
        for line in f:
            if "GITHUB_TOKEN=" in line:
                token = line.split("=")[1].strip().strip('"')
                break

# 创建 Gist
data = {
    "description": f"雪球价值投资日报 - {date}",
    "public": False,
    "files": {
        f"xueqiu_daily_report_{date}.md": {"content": content}
    }
}

req = urllib.request.Request(
    "https://api.github.com/gists",
    data=json.dumps(data).encode(),
    headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    },
    method="POST"
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())
        print(result['html_url'])
except Exception as e:
    print(f"ERROR: {e}")
PYEOF
)
    
    if [[ "$GIST_URL" == https://* ]]; then
        echo "Gist URL: $GIST_URL" >> "$LOG_FILE"
        
        # 4. 发送飞书链接
        echo "[4/4] 发送飞书链接..." >> "$LOG_FILE"
        
        # 写入待发送文件（由心跳检测发送）
        cat > /tmp/pending_feishu_daily.json << JSONEOF
{"channel": "feishu", "message": "📊 **价值投资日报 - $DATE**\n\n报告已生成，请查看：\n$GIST_URL\n\n---\n*分析模型：智谱 GLM-5*"}
JSONEOF
        
        echo "飞书消息已准备" >> "$LOG_FILE"
    else
        echo "Gist 提交失败: $GIST_URL" >> "$LOG_FILE"
    fi
else
    echo "警告: 未找到今日报告文件" >> "$LOG_FILE"
fi

echo "[$(date)] 流程执行完成" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"