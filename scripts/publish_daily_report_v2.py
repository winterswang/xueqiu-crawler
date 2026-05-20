#!/usr/bin/env python3
"""
价值投资日报发布 - V2 (含 infocard 卡片)

流程：
1. 读取日报 Markdown 内容
2. 提取关键信息，生成 infocard 卡片
3. 创建 IMA 笔记
4. 发送飞书消息（含笔记链接 + 卡片图片）
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
OUTPUT_DIR = PROJECT_DIR / "output"
INFOCARD_SKILL = Path("/root/.openclaw/workspace/skills/infocard-skills")

IMA_CLIENT_ID = os.environ.get("IMA_OPENAPI_CLIENTID") or Path("~/.config/ima/client_id").expanduser().read_text().strip()
IMA_API_KEY = os.environ.get("IMA_OPENAPI_APIKEY") or Path("~/.config/ima/api_key").expanduser().read_text().strip()


def log(message: str):
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


def extract_card_info(content: str) -> dict:
    """
    从日报内容提取卡片所需信息
    
    Args:
        content: 日报 Markdown 内容
    
    Returns:
        卡片信息字典
    """
    lines = content.split("\n")
    
    # 提取标题
    title = ""
    for line in lines[:5]:
        if line.startswith("# "):
            title = line[2:].strip()
            break
    
    # 提取概览数据
    total_articles = 0
    valid_analysis = 0
    must_read = 0
    worth_attention = 0
    
    import re
    for i, line in enumerate(lines):
        if "今日新增" in line:
            match = re.search(r'(\d+)\s*篇', line)
            if match:
                total_articles = int(match.group(1))
        if "有效分析" in line:
            match = re.search(r'(\d+)\s*篇', line)
            if match:
                valid_analysis = int(match.group(1))
        if "| 🔴 必读" in line:
            match = re.search(r'\|\s*(\d+)\s*\|', line)
            if match:
                must_read = int(match.group(1))
        if "| 🟡 值得关注" in line:
            match = re.search(r'\|\s*(\d+)\s*\|', line)
            if match:
                worth_attention = int(match.group(1))
    
    # 提取必读文章详细信息
    must_read_articles = []
    in_must_read = False
    current_article = {}
    
    for i, line in enumerate(lines):
        # 检测必读文章开始
        if "### 🔴 必读文章" in line:
            in_must_read = True
            continue
        
        # 检测必读文章结束
        if in_must_read and (line.startswith("### 🔵") or line.startswith("## 四")):
            if current_article:
                must_read_articles.append(current_article)
            break
        
        # 提取文章标题
        if in_must_read and line.startswith("####"):
            if current_article:
                must_read_articles.append(current_article)
            current_article = {}
            title_match = line.replace("####", "").strip()
            # 提取股票代码
            stock_match = re.search(r'\$([^\$]+)\$', title_match)
            stock = stock_match.group(1) if stock_match else ""
            # 截取标题（去掉股票代码）
            title_clean = re.sub(r'\$[^\$]+\$', '', title_match).strip()[:40]
            current_article = {
                "title": title_clean,
                "stock": stock,
                "summary": ""
            }
        
        # 提取总结（在 "**总结**：" 后面）
        if in_must_read and current_article and "**总结**：" in line:
            summary_match = re.search(r'\*\*总结\*\*：\s*(.+)', line)
            if summary_match:
                current_article["summary"] = summary_match.group(1).strip()[:60]
        
        # 提取核心观点（取第一条）
        if in_must_read and current_article and not current_article.get("key_point"):
            if line.strip().startswith("1."):
                key_point = line.strip()[2:].strip()[:80]
                current_article["key_point"] = key_point
    
    # 添加最后一篇文章
    if current_article:
        must_read_articles.append(current_article)
    
    return {
        "title": title,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_articles": total_articles,
        "valid_analysis": valid_analysis,
        "must_read": must_read,
        "worth_attention": worth_attention,
        "must_read_articles": must_read_articles[:3]  # 最多 3 篇
    }


def generate_infocard_html(card_info: dict) -> str:
    """
    生成信息卡片 HTML
    
    Args:
        card_info: 卡片信息字典
    
    Returns:
        HTML 内容
    """
    date = card_info.get("date", "")
    total = card_info.get("total_articles", 0)
    valid = card_info.get("valid_analysis", 0)
    must_read = card_info.get("must_read", 0)
    worth_attention = card_info.get("worth_attention", 0)
    articles = card_info.get("must_read_articles", [])
    
    articles_html = ""
    for i, article in enumerate(articles, 1):
        title = article.get("title", "未知标题")
        stock = article.get("stock", "")
        summary = article.get("summary", "")
        
        articles_html += f'''<div class="article-card">
<div class="article-header">🔴 必读 {i}</div>
<div class="article-title">{title}{"<span class='article-stock'>" + stock + "</span>" if stock else ""}</div>
{f'<div class="article-summary">{summary}</div>' if summary else ''}
</div>\n'''
    
    html += articles_html + '''    </div>
</div>
</body>
</html>'''
    
    return html
    
    html = f'''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700&family=Inter:wght@600;700&display=swap" rel="stylesheet">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ margin: 0; }}
.frame {{
    width: 2000px;
    height: 1000px;
    background: #f5f3ed;
    padding: 40px 60px;
    font-family: 'Noto Sans SC', sans-serif;
}}
.header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 3px solid #1a1a1a;
    padding-bottom: 15px;
    margin-bottom: 25px;
}}
.title {{
    font-size: 42px;
    font-weight: 700;
    color: #1a1a1a;
    letter-spacing: -1px;
}}
.date {{
    font-size: 28px;
    color: #666;
}}
.stats {{
    display: flex;
    gap: 30px;
    margin-bottom: 25px;
}}
.stat-box {{
    background: #1a1a1a;
    color: #fff;
    padding: 15px 30px;
    border-radius: 6px;
    text-align: center;
}}
.stat-num {{
    font-size: 42px;
    font-weight: 700;
    font-family: 'Inter', sans-serif;
}}
.stat-label {{
    font-size: 16px;
    margin-top: 2px;
}}
.articles {{
    display: flex;
    gap: 25px;
}}
.article-card {{
    flex: 1;
    background: #fff;
    padding: 25px;
    border-radius: 8px;
    border: 2px solid #e0e0e0;
}}
.article-header {{
    font-size: 22px;
    font-weight: 700;
    color: #c41e3a;
    margin-bottom: 15px;
    padding-bottom: 10px;
    border-bottom: 2px solid #c41e3a;
}}
.article-title {{
    font-size: 18px;
    color: #1a1a1a;
    line-height: 1.4;
    font-weight: 500;
    margin-bottom: 10px;
}}
.article-stock {{
    display: inline-block;
    font-size: 14px;
    color: #666;
    background: #f0f0f0;
    padding: 2px 8px;
    border-radius: 4px;
    margin-left: 8px;
}}
.article-summary {{
    font-size: 14px;
    color: #555;
    line-height: 1.5;
    background: #f8f8f8;
    padding: 10px 12px;
    border-radius: 4px;
    border-left: 3px solid #c41e3a;
}}
</style>
</head>
<body>
<div class="frame">
    <div class="header">
        <div class="title">📊 价值投资日报</div>
        <div class="date">{date}</div>
    </div>
    
    <div class="stats">
        <div class="stat-box">
            <div class="stat-num">{total}</div>
            <div class="stat-label">新增</div>
        </div>
        <div class="stat-box">
            <div class="stat-num">{valid}</div>
            <div class="stat-label">有效</div>
        </div>
        <div class="stat-box">
            <div class="stat-num">{must_read}</div>
            <div class="stat-label">必读</div>
        </div>
        <div class="stat-box">
            <div class="stat-num">{worth_attention}</div>
            <div class="stat-label">关注</div>
        </div>
    </div>
    
    <div class="articles">
'''


def render_infocard(html: str, output_path: str) -> bool:
    """
    渲染信息卡片为 PNG
    
    Args:
        html: HTML 内容
        output_path: 输出路径
    
    Returns:
        是否成功
    """
    # 写入临时 HTML 文件
    html_file = Path("/tmp/xueqiu_card_temp.html")
    html_file.write_text(html, encoding="utf-8")
    
    # 使用 playwright 截图
    try:
        from playwright.sync_api import sync_playwright
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 2000, "height": 1500})
            page.goto(f"file://{html_file.absolute()}")
            page.screenshot(path=output_path, full_page=False)
            browser.close()
        
        log(f"卡片生成成功: {output_path}")
        return True
        
    except Exception as e:
        log(f"卡片生成失败: {e}")
        return False


def check_existing_note(date: str) -> str:
    """
    检查是否已存在今天的笔记
    
    Returns:
        已存在的 doc_id 或 None
    """
    import urllib.request
    import urllib.error
    
    # 搜索今天的笔记
    url = "https://ima.qq.com/openapi/note/v1/search_note_book"
    
    body = {
        "search_type": 0,  # 标题搜索
        "query_info": {"title": f"价值投资日报 - {date}"},
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
            log(f"搜索结果: code={result.get('code')}, docs={len(result.get('data', {}).get('docs', []))}")
            if result.get("code") == 0:
                docs = result.get("data", {}).get("docs", [])
                for doc in docs:
                    basic = doc.get("doc", {}).get("basic_info", {})
                    title = basic.get("title", "")
                    log(f"  找到笔记: {title}")
                    # 匹配标题（兼容带日期和不带日期的格式）
                    if "价值投资日报" in title and date in title:
                        log(f"  匹配成功，返回已有笔记: {basic.get('docid')}")
                        return basic.get("docid")
                    # 也匹配不带日期的（今天的）
                    if title.strip() in [f"📊 价值投资日报", "价值投资日报"]:
                        # 检查创建时间是否是今天
                        create_time = basic.get("create_time", 0)
                        try:
                            create_ts = int(create_time) / 1000 if create_time else 0
                            from datetime import datetime
                            note_date = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d")
                            if note_date == date:
                                log(f"  匹配成功（今日笔记），返回已有笔记: {basic.get('docid')}")
                                return basic.get("docid")
                        except (ValueError, TypeError) as e:
                            log(f"  解析时间失败: {e}")
                            pass
            return None
    except Exception as e:
        log(f"搜索笔记异常: {e}")
        return None


def create_ima_note(title: str, content: str) -> str:
    """创建 IMA 笔记，返回 doc_id"""
    
    import urllib.request
    import urllib.error
    
    # 检查是否已存在
    date = datetime.now().strftime("%Y-%m-%d")
    existing_doc_id = check_existing_note(date)
    if existing_doc_id:
        log(f"笔记已存在: {existing_doc_id}")
        return existing_doc_id
    
    url = "https://ima.qq.com/openapi/note/v1/import_doc"
    
    body = {
        "content_format": 1,  # Markdown
        "content": content
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
                return result.get("data", {}).get("doc_id")
            else:
                log(f"IMA 创建失败: {result.get('msg')}")
                return None
    except Exception as e:
        log(f"IMA 请求异常: {e}")
        return None


def send_feishu_with_image(message: str, image_path: str = None):
    """发送飞书消息（含图片）"""
    pending_file = Path("/tmp/pending_feishu_daily.json")
    
    data = {
        "channel": "feishu",
        "target": "user:ou_0451c7608ba9c337b4f92ddc069bb810",
        "account": "engineer",
        "message": message
    }
    
    pending_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log(f"消息已写入: {pending_file}")
    
    # 如果有图片，单独发送
    if image_path and Path(image_path).exists():
        # 使用 feishu-img-tool 发送图片（指定 engineer 账号）
        img_tool = Path("/root/.openclaw/workspace/skills/feishu-img-tool/feishu-image-tool.js")
        if img_tool.exists():
            result = subprocess.run([
                "node", str(img_tool), "send",
                "--target", "ou_0451c7608ba9c337b4f92ddc069bb810",
                "--file", image_path,
                "--account", "engineer"  # 使用 engineer 应用
            ], capture_output=True, text=True)
            if result.returncode == 0:
                log("卡片图片已发送")
            else:
                log(f"卡片图片发送失败: {result.stderr}")


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
    
    # 2. 提取卡片信息
    log("提取卡片信息...")
    card_info = extract_card_info(content)
    log(f"今日新增: {card_info['total_articles']}, 必读: {card_info['must_read']}")
    
    # 3. 生成信息卡片
    log("生成信息卡片...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    card_path = str(OUTPUT_DIR / f"xueqiu_card_{date}.png")
    
    html = generate_infocard_html(card_info)
    if render_infocard(html, card_path):
        log(f"卡片已保存: {card_path}")
    else:
        log("卡片生成失败，继续...")
        card_path = None
    
    # 4. 创建 IMA 笔记
    log("创建 IMA 笔记...")
    title = f"价值投资日报 - {date}"
    doc_id = create_ima_note(title, content)
    
    if doc_id:
        note_url = f"https://ima.qq.com/note/{doc_id}"
        log(f"笔记创建成功: {note_url}")
    else:
        note_url = None
        log("笔记创建失败")
    
    # 5. 发送飞书消息
    log("发送飞书消息...")
    
    message = f"""📊 **价值投资日报 - {date}**

**统计**: 今日新增 {card_info['total_articles']} 篇，有效分析 {card_info['valid_analysis']} 篇，必读 {card_info['must_read']} 篇

{"查看完整日报：" + note_url if note_url else ""}

---
*分析模型：智谱 GLM-5*"""
    
    send_feishu_with_image(message, card_path)
    
    log("完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())