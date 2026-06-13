#!/usr/bin/env python3
"""
价值投资日报发布脚本

流程: 读取日报 → 推送到 IMA 笔记

IMPORTANT: IMA 凭证配置
- 优先读取环境变量: IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY
- 回退读取文件: ~/.config/ima/client_id / ~/.config/ima/api_key
"""

from __future__ import annotations

import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Optional

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


def get_daily_report(date: Optional[str] = None) -> str:
    """读取日报 Markdown 内容"""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    report_file = REPORT_DIR / f"{date}.md"
    if not report_file.exists():
        raise FileNotFoundError(f"日报文件不存在: {report_file}")
    return report_file.read_text(encoding="utf-8")


def check_existing_note(date: str) -> Optional[str]:
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
        _logger.warning(f"搜索笔记异常: {e}")
    return None


def create_ima_note(title: str, content: str) -> Optional[str]:
    """创建 IMA 笔记，返回 note_id"""
    date = datetime.now().strftime("%Y-%m-%d")

    _logger.info(f"开始推送 IMA: {title}, 内容长度={len(content)}")

    existing_doc_id = check_existing_note(date)
    if existing_doc_id:
        _logger.info(f"今日已有笔记 {existing_doc_id}，仍将创建新笔记以确保内容最新")

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
        return 1

    # 2. 推送到 IMA
    note_id = create_ima_note(f"价值投资日报 - {date}", content)
    note_url = f"https://ima.qq.com/note/{note_id}" if note_id else None
    if note_id:
        log_execution_stage("ima_push", "success", f"note_id={note_id}")
        _logger.info(f"IMA 笔记: {note_url}")
    else:
        _logger.error("IMA 推送失败")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
