#!/usr/bin/env python3
"""
价值投资日报发布脚本

流程: 读取日报 → 推送到 IMA 笔记 → 飞书通知（含 IMA 链接）

IMPORTANT: IMA 凭证配置
- 优先读取环境变量: IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY
- 回退读取文件: ~/.config/ima/client_id / ~/.config/ima/api_key
"""

import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from logging_utils import get_logger, log_execution_stage

_logger = get_logger()

PROJECT_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = PROJECT_DIR / "data" / "daily_reports"


def _read_ima_credential(env_key: str, file_path: str) -> str:
    """读取 IMA 凭证：环境变量优先，回退到文件"""
    value = os.environ.get(env_key)
    if value:
        return value
    cred_file = Path(file_path).expanduser()
    if cred_file.exists():
        return cred_file.read_text().strip()
    return ""


IMA_CLIENT_ID = _read_ima_credential("IMA_OPENAPI_CLIENTID", "~/.config/ima/client_id")
IMA_API_KEY = _read_ima_credential("IMA_OPENAPI_APIKEY", "~/.config/ima/api_key")


def log(message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def get_daily_report(date: str = None) -> str:
    """读取日报 Markdown 内容"""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    report_file = REPORT_DIR / f"{date}.md"
    if not report_file.exists():
        raise FileNotFoundError(f"日报文件不存在: {report_file}")
    return report_file.read_text(encoding="utf-8")


def check_existing_note(date: str) -> str | None:
    """检查是否已存在同日期 IMA 笔记（去重）"""
    url = "https://ima.qq.com/openapi/note/v1/search_note_book"
    body = {
        "search_type": 0,
        "query_info": {"title": "价值投资日报"},
        "start": 0,
        "end": 10
    }
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
            if result.get("code") == 0:
                for doc in result.get("data", {}).get("docs", []):
                    basic = doc.get("doc", {}).get("basic_info", {})
                    if "价值投资日报" in basic.get("title", ""):
                        create_time = basic.get("create_time", 0)
                        try:
                            create_ts = int(create_time) / 1000 if create_time else 0
                            note_date = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d")
                            if note_date == date:
                                return basic.get("docid")
                        except (ValueError, OSError):
                            pass
    except Exception as e:
        log(f"搜索笔记异常: {e}")
    return None


def create_ima_note(title: str, content: str) -> str | None:
    """创建 IMA 笔记，返回 note_id"""
    date = datetime.now().strftime("%Y-%m-%d")

    _logger.info(f"开始推送 IMA: {title}, 内容长度={len(content)}")

    existing_doc_id = check_existing_note(date)
    if existing_doc_id:
        log(f"笔记已存在: {existing_doc_id}")
        _logger.info(f"IMA 笔记已存在, 跳过: {existing_doc_id}")
        return existing_doc_id

    url = "https://ima.qq.com/openapi/note/v1/import_doc"
    body = {"content_format": 1, "content": content}
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
            code = result.get("code")
            _logger.info(f"IMA API 响应: code={code}, msg={result.get('message', 'N/A')}")
            if code == 0:
                note_id = result.get("data", {}).get("note_id")
                _logger.info(f"IMA 笔记创建成功: note_id={note_id}")
                return note_id
            else:
                _logger.error(f"IMA 笔记创建失败: code={code}, response={json.dumps(result, ensure_ascii=False)[:500]}")
                log_execution_stage("ima_push", "failed", f"code={code}")
    except urllib.error.HTTPError as e:
        _logger.error(f"IMA HTTP 错误: {e.code} {e.reason}, body={e.read().decode('utf-8', errors='replace')[:500]}")
        log_execution_stage("ima_push", "failed", f"HTTP {e.code}")
    except Exception as e:
        _logger.error(f"IMA 请求异常: {e}", exc_info=True)
        log_execution_stage("ima_push", "failed", str(e)[:100])
    return None


def notify_feishu(date: str, note_url: str | None):
    """发送飞书通知（写入 JSON 文件，由 Gateway 处理）"""
    pending_file = Path("/tmp/pending_feishu_daily.json")

    message_parts = [
        f"📊 **价值投资日报 - {date}**",
    ]
    if note_url:
        message_parts.append(f"📖 [查看完整日报]({note_url})")

    target = os.environ.get(
        "FEISHU_DAILY_TARGET",
        "user:ou_10fd623ef35ada42d7ad772c34c216af"
    )
    data = {
        "channel": "feishu",
        "account": "engineer",
        "target": target,
        "message": "\n\n".join(message_parts)
    }
    pending_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log(f"飞书通知已写入: {pending_file}")


def extract_summary(content: str) -> dict:
    """从日报中提取简要统计信息"""
    import re
    lines = content.split("\n")

    stats = {
        "total": 0,
        "valid": 0,
        "must_read": 0,
        "worth_reading": 0,
    }

    for line in lines:
        if "今日新增" in line:
            m = re.search(r'(\d+)\s*篇', line)
            if m:
                stats["total"] = int(m.group(1))
        if "有效分析" in line:
            m = re.search(r'(\d+)\s*篇', line)
            if m:
                stats["valid"] = int(m.group(1))
        if "| 🔴 必读" in line:
            m = re.search(r'\|\s*(\d+)\s*\|', line)
            if m:
                stats["must_read"] = int(m.group(1))
        if "| 🟡 值得关注" in line:
            m = re.search(r'\|\s*(\d+)\s*\|', line)
            if m:
                stats["worth_reading"] = int(m.group(1))

    return stats


def main():
    date = datetime.now().strftime("%Y-%m-%d")
    _logger.info("=" * 50)
    _logger.info(f"开始发布价值投资日报 - {date}")

    # 1. 读取日报
    try:
        content = get_daily_report(date)
        _logger.info(f"日报读取成功: {len(content)} 字符")
    except FileNotFoundError as e:
        _logger.error(f"日报文件不存在: {e}")
        log(f"错误: {e}")
        return 1

    # 2. 推送到 IMA
    note_id = create_ima_note(f"价值投资日报 - {date}", content)
    note_url = f"https://ima.qq.com/note/{note_id}" if note_id else None
    if note_id:
        log_execution_stage("ima_push", "success", f"note_id={note_id}")

    # 3. 提取摘要信息
    stats = extract_summary(content)
    log(f"推送完成 — 新增: {stats['total']}篇, 必读: {stats['must_read']}篇, IMA: {'✅' if note_id else '❌'}")

    # 4. 飞书通知
    notify_feishu(date, note_url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
