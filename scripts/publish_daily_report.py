#!/usr/bin/env python3
"""
价值投资日报发布 - 新版流程

1. 读取日报 Markdown 内容
2. 创建 IMA 笔记
3. 发送笔记链接到飞书

不再使用 Gist，改用 IMA 笔记。
"""

import os
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path

# 配置
PROJECT_DIR = Path("/root/.openclaw/workspace/xueqiu-crawler")
REPORT_DIR = PROJECT_DIR / "data" / "daily_reports"
IMA_CLIENT_ID = os.environ.get("IMA_OPENAPI_CLIENTID") or Path("~/.config/ima/client_id").expanduser().read_text().strip()
IMA_API_KEY = os.environ.get("IMA_OPENAPI_APIKEY") or Path("~/.config/ima/api_key").expanduser().read_text().strip()


def log(message: str):
    """打印日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def get_daily_report(date: str = None) -> str:
    """读取日报内容"""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    
    report_file = REPORT_DIR / f"{date}.md"
    
    if not report_file.exists():
        raise FileNotFoundError(f"日报文件不存在: {report_file}")
    
    return report_file.read_text(encoding="utf-8")


def create_ima_note(title: str, content: str) -> dict:
    """创建 IMA 笔记"""
    
    # 构建 API 请求
    url = "https://ima.qq.com/openapi/note/v1/import_doc"
    
    # 确保内容是 UTF-8
    content = content.encode("utf-8", errors="ignore").decode("utf-8")
    
    body = {
        "content_format": 1,  # Markdown
        "content": content
    }
    
    # 发送请求
    import urllib.request
    import urllib.error
    
    headers = {
        "ima-openapi-clientid": IMA_CLIENT_ID,
        "ima-openapi-apikey": IMA_API_KEY,
        "Content-Type": "application/json"
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            log(f"API 返回: {json.dumps(result, ensure_ascii=False, indent=2)}")
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise Exception(f"IMA API 错误: {e.code} - {error_body}")


def send_feishu_message(message: str):
    """发送飞书消息"""
    pending_file = Path("/tmp/pending_feishu_daily.json")
    
    data = {
        "channel": "feishu",
        "target": "user:ou_0451c7608ba9c337b4f92ddc069bb810",
        "account": "engineer",
        "message": message
    }
    
    pending_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log(f"消息已写入: {pending_file}")


def main():
    """主流程"""
    date = datetime.now().strftime("%Y-%m-%d")
    
    log(f"开始发布价值投资日报 - {date}")
    
    # 1. 读取日报
    log("读取日报内容...")
    try:
        content = get_daily_report(date)
        log(f"日报长度: {len(content)} 字符")
    except FileNotFoundError as e:
        log(f"错误: {e}")
        return 1
    
    # 2. 创建 IMA 笔记
    log("创建 IMA 笔记...")
    title = f"价值投资日报 - {date}"
    
    try:
        result = create_ima_note(title, content)
        
        # API 返回格式: {"code": 0, "msg": "success", "data": {"doc_id": "xxx"}}
        if result.get("code") != 0:
            log(f"创建失败: {result.get('msg')}")
            return 1
        
        doc_id = result.get("data", {}).get("doc_id")
        log(f"笔记创建成功: {doc_id}")
        
    except Exception as e:
        log(f"创建笔记异常: {e}")
        return 1
    
    # 3. 发送飞书消息
    log("发送飞书消息...")
    
    # IMA 笔记链接格式: https://ima.qq.com/note/{doc_id}
    note_url = f"https://ima.qq.com/note/{doc_id}"
    
    message = f"""📊 **价值投资日报 - {date}**

日报已发布到 IMA 笔记，请查看：
{note_url}

---
*分析模型：智谱 GLM-5*"""
    
    send_feishu_message(message)
    
    log("完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())