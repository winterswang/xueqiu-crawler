#!/usr/bin/env python3
"""
日报飞书推送脚本
- 读取生成好的日报 markdown
- 提取 🔴必读列表和核心观点
- LLM 生成 3 句话今日热点摘要
- 推送飞书卡片给用户
"""

import os
import sys
import re
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent))
from logging_utils import get_logger

_logger = get_logger()

PROJECT_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = PROJECT_DIR / "data" / "daily_reports"

# 飞书 webhook 从环境变量读取，或使用配置文件
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
# OpenAI 兼容客户端（使用字节 coding plan minimax-m3）
client = OpenAI(
    api_key=os.environ.get("ARK_API_KEY", os.environ.get("MINIMAX_API_KEY", "")),
    base_url=os.environ.get("ARK_CODING_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3"),
)
MODEL = os.environ.get("ANALYZE_LLM_MODEL", "deepseek-v4-flash-ga-260731")  # 2026-09-03: 与 config.yaml 同步升级 flash-ga


def read_today_report(date: str = None) -> str:
    """读取今日日报 markdown"""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    report_file = REPORT_DIR / f"{date}.md"
    if not report_file.exists():
        raise FileNotFoundError(f"日报文件不存在: {report_file}")
    return report_file.read_text(encoding="utf-8")


def extract_must_read(report_md: str) -> list:
    """从日报中提取 🔴必读 文章列表"""
    must_read = []
    in_must_read = False
    
    for line in report_md.split("\n"):
        if "### 🔴 必读" in line:
            in_must_read = True
            continue
        if in_must_read and line.startswith("### "):
            break
        if in_must_read and line.startswith("#### "):
            title = line.replace("#### ", "").strip()
            # 去掉开头的数字序号 "1. "
            title = re.sub(r'^\d+\.\s*', '', title)
            # 只取标题部分（截断到第一个空格后面的正文太长就截断）
            if len(title) > 70:
                title = title[:70] + "..."
            must_read.append(title)
    
    return must_read


def generate_hot_topics_summary(report_md: str) -> str:
    """用 LLM 生成今日热点话题 3 句话摘要"""
    # 提取文章标题和简要内容，避免 token 太长
    lines = []
    capture = False
    count = 0
    for line in report_md.split("\n"):
        if line.startswith("### ") and ("必读" in line or "值得关注" in line):
            capture = True
            continue
        if capture and line.startswith("### "):
            break
        if capture and line.startswith("#### "):
            lines.append(line.replace("#### ", "- "))
            count += 1
            if count > 15:
                break
    
    articles_text = "\n".join(lines)
    
    prompt = f"""以下是今天雪球价值投资日报的主要文章标题：

{articles_text}

请用3句话总结今天大V们讨论的核心热点话题，每句不超过50字，口语化，直接说重点。不要开场白，直接输出3句话。"""
    
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.3,
            timeout=30,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        _logger.warning(f"生成热点摘要失败: {e}")
        return "今日热点摘要生成失败，请查看完整日报。"


def send_feishu_card(date: str, hot_topics: str, must_read: list, note_url: str, stats: dict):
    """发送飞书卡片消息"""
    if not FEISHU_WEBHOOK:
        _logger.warning("未配置 FEISHU_WEBHOOK，跳过飞书推送")
        return False
    
    # 构建卡片内容
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📰 今日热点**\n{hot_topics}"
            }
        },
        {"tag": "hr"},
    ]
    
    # 必读部分
    if must_read:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**🔴 必读文章（" + str(len(must_read)) + "篇）**"
            }
        })
        for i, title in enumerate(must_read[:5], 1):  # 最多显示5篇
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"{i}. {title[:80]}"
                }
            })
        elements.append({"tag": "hr"})
    
    # 统计部分
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"**📊 统计**\n🔴 必读 {stats['must_read']} 篇 | 🟡 值得关注 {stats['worth_reading']} 篇 | 📰 市场资讯 {stats['market_news']} 篇"
        }
    })
    
    # 跳转按钮
    if note_url:
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "📄 查看完整日报 (IMA)"
                    },
                    "type": "primary",
                    "url": note_url
                }
            ]
        })
    
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📊 价值投资日报 - {date}"
                },
                "template": "blue"
            },
            "elements": elements
        }
    }
    
    # 发送请求
    req = urllib.request.Request(
        FEISHU_WEBHOOK,
        data=json.dumps(card, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 0:
                _logger.info("✅ 飞书卡片推送成功")
                return True
            else:
                _logger.error(f"飞书推送失败: {result}")
                return False
    except Exception as e:
        _logger.error(f"飞书请求异常: {e}")
        return False


def extract_stats(report_md: str) -> dict:
    """从日报中提取统计数据"""
    stats = {"must_read": 0, "worth_reading": 0, "market_news": 0, "reference": 0}
    for line in report_md.split("\n"):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        if "🔴 必读" in parts[1]:
            try:
                stats["must_read"] = int(parts[2])
            except:
                pass
        elif "🟡 值得关注" in parts[1]:
            try:
                stats["worth_reading"] = int(parts[2])
            except:
                pass
        elif "📰 市场资讯" in parts[1]:
            try:
                stats["market_news"] = int(parts[2])
            except:
                pass
        elif "🔵 参考" in parts[1]:
            try:
                stats["reference"] = int(parts[2])
            except:
                pass
    return stats


def main():
    date = datetime.now().strftime("%Y-%m-%d")
    _logger.info(f"生成日报推送摘要 - {date}")
    
    # 1. 读取日报
    try:
        report_md = read_today_report(date)
        _logger.info(f"日报读取成功: {len(report_md)} 字符")
    except FileNotFoundError as e:
        _logger.error(str(e))
        print("⚠️ 今日日报文件不存在")
        return 1
    
    # 2. 提取数据
    must_read = extract_must_read(report_md)
    stats = extract_stats(report_md)
    
    # 3. 生成热点摘要
    _logger.info("生成今日热点摘要...")
    hot_topics = generate_hot_topics_summary(report_md)
    
    # 4. IMA 链接（从环境变量读取）
    note_url = os.environ.get("IMA_NOTE_URL", "")
    
    # 5. 直接输出 markdown 摘要，由 cron agent 发送
    output = []
    output.append(f"📊 **价值投资日报 - {date}**")
    output.append("")
    output.append(f"📰 **今日热点**")
    output.append(hot_topics)
    output.append("")
    output.append(f"🔴 **必读文章（{stats['must_read']}篇）**")
    if must_read:
        for i, title in enumerate(must_read[:5], 1):
            output.append(f"{i}. {title[:80]}")
    else:
        output.append("今日无必读文章")
    output.append("")
    output.append(f"📊 统计：🔴 必读 {stats['must_read']} | 🟡 值得关注 {stats['worth_reading']} | 📰 市场资讯 {stats['market_news']}")
    if note_url:
        output.append("")
        output.append(f"📄 查看完整日报：{note_url}")
    
    result = "\n".join(output)
    print(result)
    _logger.info("摘要生成完成")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
