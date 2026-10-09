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
from llm_config import resolve_model

_logger = get_logger()

PROJECT_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = PROJECT_DIR / "data" / "daily_reports"

# 飞书 webhook 从环境变量读取，或使用配置文件
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
# OpenAI 兼容客户端（字节 coding plan，模型与 config.yaml 保持同步）
client = OpenAI(
    api_key=os.environ.get("ARK_API_KEY", os.environ.get("MINIMAX_API_KEY", "")),
    base_url=os.environ.get(
        "ARK_CODING_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3"
    ),
)
# 模型 id 走唯一解析入口（config/config.yaml），与 analyzer 同源，
# 避免两边各自维护一个模型名（PROJECT_LOG D-009）
MODEL = resolve_model()


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


def _section_body(report_md: str, keyword: str) -> str:
    """取 `### <含 keyword 的标题>` 到下一个 `### ` 之间的正文（不含标题行）。"""
    body: list[str] = []
    capture = False
    for line in report_md.split("\n"):
        if line.startswith("### "):
            capture = keyword in line
            continue
        if capture:
            body.append(line)
    return "\n".join(body).strip()


def _clean_item(text: str) -> str:
    """把条目清成「标题」本身.

    「参考」段是编号列表，条目形如
    `[标题](https://xueqiu.com/...)（作者）` —— 整条喂给模型既占 token 又是噪音
    （2026-10-09 实测：模型面对这种输入直接回了空串）。
    """
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # markdown 链接 → 文字
    text = re.sub(r"（[^）]*）\s*$", "", text)  # 去掉尾部（作者）
    return text.strip()


def _section_items(report_md: str, keyword: str) -> list[str]:
    """取某一段里的条目行（`#### xxx` 或 `1. xxx`），去掉编号/井号前缀。"""
    items = []
    for line in _section_body(report_md, keyword).split("\n"):
        m = re.match(r"^####\s+(.*)$", line) or re.match(r"^\d+\.\s+(.*)$", line)
        if m:
            cleaned = _clean_item(m.group(1))
            if cleaned:
                items.append(cleaned)
    return items


def _extract_article_titles(report_md: str) -> list[str]:
    """报告里可用的文章标题.

    优先「必读 / 值得关注」；两者都空时退到「参考」——
    2026-10-09 实测踩到：当天 3 篇全是参考级，前两段不存在，于是标题列表为空，
    prompt 里放了个空列表，模型回「未收到文章标题。请重新提供内容。」
    （2026-10-05 就是这么把模型错误串当热点发出去的）。
    """
    titles = _section_items(report_md, "必读") + _section_items(report_md, "值得关注")
    if titles:
        return titles[:15]
    return _section_items(report_md, "参考")[:15]


# 模型「没拿到料」时常见的推脱/报错措辞 —— 这些**不能**当成热点推给用户
_LLM_REFUSAL_MARKERS = (
    "未收到",
    "请重新提供",
    "请提供",
    "无法",
    "抱歉",
    "作为AI",
    "作为 AI",
    "不能帮",
    "没有任何",
)
# 「暂无…」这类占位符不算有效内容（报告里本来就有）
_PLACEHOLDER_MARKERS = ("暂无", "今日无", "无集中", "无核心")


def _looks_usable(text: str) -> bool:
    if len(text) < 10:
        return False
    return not any(marker in text for marker in _LLM_REFUSAL_MARKERS)


def _fallback_topics(report_md: str) -> str:
    """LLM 拿不到可用输出时的兜底：用报告里已有的核心观点 / 风险段.

    宁可少说，也不能把模型的推脱串或空白当成「今日热点」发出去。
    """
    for keyword in ("核心观点", "风险与机会"):
        body = _section_body(report_md, keyword)
        cleaned = "\n".join(
            line
            for line in body.split("\n")
            if line.strip()
            and not line.lstrip().startswith("#")
            and not any(p in line for p in _PLACEHOLDER_MARKERS)
        ).strip()
        cleaned = re.sub(r"^[-*]\s*", "", cleaned, flags=re.MULTILINE).strip()
        if len(cleaned) >= 10:
            return cleaned.split("\n\n")[0][:200]
    return "今日无值得提炼的热点（当日文章以短帖/图片帖为主）"


def generate_hot_topics_summary(report_md: str) -> str:
    """用 LLM 生成今日热点话题 3 句话摘要.

    拿不到可用结果时**一定**回退到 `_fallback_topics`，绝不把空白或模型的推脱
    串原样发出去（2026-10-05 就发过「未收到文章标题。请重新提供内容。」）。
    """
    titles = _extract_article_titles(report_md)
    if not titles:
        _logger.warning("热点摘要：报告里没有可用标题，直接走兜底")
        return _fallback_topics(report_md)

    articles_text = "\n".join(f"- {t}" for t in titles)
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
        content = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        _logger.warning(f"生成热点摘要失败: {e}")
        return _fallback_topics(report_md)

    if not _looks_usable(content):
        _logger.warning(f"热点摘要不可用（{content[:50]!r}），走兜底")
        return _fallback_topics(report_md)
    return content


def send_feishu_card(
    date: str, hot_topics: str, must_read: list, note_url: str, stats: dict
):
    """发送飞书卡片消息"""
    if not FEISHU_WEBHOOK:
        _logger.warning("未配置 FEISHU_WEBHOOK，跳过飞书推送")
        return False

    # 构建卡片内容
    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**📰 今日热点**\n{hot_topics}"},
        },
        {"tag": "hr"},
    ]

    # 必读部分
    if must_read:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**🔴 必读文章（" + str(len(must_read)) + "篇）**",
                },
            }
        )
        for i, title in enumerate(must_read[:5], 1):  # 最多显示5篇
            elements.append(
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": f"{i}. {title[:80]}"},
                }
            )
        elements.append({"tag": "hr"})

    # 统计部分
    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📊 统计**\n🔴 必读 {stats['must_read']} 篇 | 🟡 值得关注 {stats['worth_reading']} 篇 | 📰 市场资讯 {stats['market_news']} 篇",
            },
        }
    )

    # 跳转按钮
    if note_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "📄 查看完整日报 (IMA)",
                        },
                        "type": "primary",
                        "url": note_url,
                    }
                ],
            }
        )

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 价值投资日报 - {date}"},
                "template": "blue",
            },
            "elements": elements,
        },
    }

    # 发送请求
    req = urllib.request.Request(
        FEISHU_WEBHOOK,
        data=json.dumps(card, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
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
            except ValueError:
                pass
        elif "🟡 值得关注" in parts[1]:
            try:
                stats["worth_reading"] = int(parts[2])
            except ValueError:
                pass
        elif "📰 市场资讯" in parts[1]:
            try:
                stats["market_news"] = int(parts[2])
            except ValueError:
                pass
        elif "🔵 参考" in parts[1]:
            try:
                stats["reference"] = int(parts[2])
            except ValueError:
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
    output.append("📰 **今日热点**")
    output.append(hot_topics)
    output.append("")
    output.append(f"🔴 **必读文章（{stats['must_read']}篇）**")
    if must_read:
        for i, title in enumerate(must_read[:5], 1):
            output.append(f"{i}. {title[:80]}")
    else:
        output.append("今日无必读文章")
    output.append("")
    output.append(
        f"📊 统计：🔴 必读 {stats['must_read']} | 🟡 值得关注 {stats['worth_reading']} | 📰 市场资讯 {stats['market_news']}"
    )
    if note_url:
        output.append("")
        output.append(f"📄 查看完整日报：{note_url}")

    result = "\n".join(output)
    print(result)
    _logger.info("摘要生成完成")

    return 0


if __name__ == "__main__":
    sys.exit(main())
