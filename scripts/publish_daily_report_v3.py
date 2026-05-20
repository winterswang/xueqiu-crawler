#!/usr/bin/env python3
"""
价值投资日报发布脚本 V3

生成高质量信息卡片 + IMA 笔记 + 飞书推送

## 卡片设计规范

### 尺寸
- 宽度: 1200px
- 高度: 1800px
- 比例: 2:3 (竖向，适合手机)

### 必须包含的信息
- 每篇文章的股票代码
- 安全边际评估 (🟢高/🟡中/🔴低)
- 价值投资符合度 (✅符合/⚠️部分/❌不符合)
- 核心总结 (≤50字)
- 投资启示 (每条≤50字，≥2条)

### 禁止
- 横向排列的文章卡片
- 无评估信息的文章
- 超长文字 (>50字)

详细规范见: docs/infocard_spec.md
"""

import os
import sys
import json
import subprocess
import re
from datetime import datetime
from pathlib import Path

# 配置
PROJECT_DIR = Path("/root/.openclaw/workspace/xueqiu-crawler")
REPORT_DIR = PROJECT_DIR / "data" / "daily_reports"
OUTPUT_DIR = PROJECT_DIR / "output"

IMA_CLIENT_ID = os.environ.get("IMA_OPENAPI_CLIENTID") or Path("~/.config/ima/client_id").expanduser().read_text().strip()
IMA_API_KEY = os.environ.get("IMA_OPENAPI_APIKEY") or Path("~/.config/ima/api_key").expanduser().read_text().strip()


def log(message: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")


def get_daily_report(date: str = None) -> str:
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    report_file = REPORT_DIR / f"{date}.md"
    if not report_file.exists():
        raise FileNotFoundError(f"日报文件不存在: {report_file}")
    return report_file.read_text(encoding="utf-8")


def extract_card_info(content: str) -> dict:
    """提取高价值投资信息"""
    lines = content.split("\n")
    
    # 统计数据
    total_articles = 0
    valid_analysis = 0
    must_read = 0
    worth_attention = 0
    
    for line in lines:
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
    
    # 提取必读文章的深度信息
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
        
        # 提取文章标题和股票
        if in_must_read and line.startswith("####"):
            if current_article:
                must_read_articles.append(current_article)
            current_article = {}
            title_match = line.replace("####", "").strip()
            stock_match = re.search(r'\$([^\$]+)\$', title_match)
            stock = stock_match.group(1) if stock_match else ""
            title_clean = re.sub(r'\$[^\$]+\$', '', title_match).strip()[:35]
            current_article = {
                "title": title_clean,
                "stock": stock,
                "safety_margin": "未知",
                "value_fit": "未知",
                "summary": "",
                "insights": []
            }
        
        # 提取价值投资评估
        if in_must_read and current_article:
            if "**符合价值投资原则**：" in line or "符合价值投资原则：" in line:
                if "是" in line:
                    current_article["value_fit"] = "✅ 符合"
                elif "部分" in line:
                    current_article["value_fit"] = "⚠️ 部分"
                else:
                    current_article["value_fit"] = "❌ 不符合"
            
            if "**安全边际**：" in line or "安全边际：" in line:
                if "高" in line:
                    current_article["safety_margin"] = "🟢 高"
                elif "中" in line:
                    current_article["safety_margin"] = "🟡 中"
                elif "低" in line:
                    current_article["safety_margin"] = "🔴 低"
            
            # 提取总结
            if "**总结**：" in line:
                summary_match = re.search(r'\*\*总结\*\*：\s*(.+)', line)
                if summary_match:
                    current_article["summary"] = summary_match.group(1).strip()[:45]
            
            # 提取投资启示
            if "投资启示：" in line or "### 投资启示" in line:
                # 读取后续几行的启示
                for j in range(i+1, min(i+5, len(lines))):
                    insight_line = lines[j].strip()
                    if insight_line.startswith("- ") and len(insight_line) > 5:
                        insight = insight_line[2:].strip()[:40]
                        current_article["insights"].append(insight)
                    elif insight_line.startswith("##") or insight_line.startswith("###"):
                        break
    
    if current_article:
        must_read_articles.append(current_article)
    
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_articles": total_articles,
        "valid_analysis": valid_analysis,
        "must_read": must_read,
        "worth_attention": worth_attention,
        "must_read_articles": must_read_articles[:3]
    }


def generate_infocard_html(card_info: dict) -> str:
    date = card_info.get("date", "")
    total = card_info.get("total_articles", 0)
    valid = card_info.get("valid_analysis", 0)
    must_read = card_info.get("must_read", 0)
    worth_attention = card_info.get("worth_attention", 0)
    articles = card_info.get("must_read_articles", [])
    
    # 生成文章卡片（竖向排列，更宽更长）
    articles_html = ""
    for i, article in enumerate(articles, 1):
        title = article.get("title", "未知标题")
        stock = article.get("stock", "")
        safety = article.get("safety_margin", "未知")
        value_fit = article.get("value_fit", "未知")
        summary = article.get("summary", "")
        insights = article.get("insights", [])
        
        insights_html = ""
        if insights:
            insights_html = f'''<div class="insights">
<div class="insights-title">💡 投资启示</div>
{''.join([f'<div class="insight-item">• {ins[:50]}{"..." if len(ins) > 50 else ""}</div>' for ins in insights[:3]])}
</div>'''
        
        articles_html += f'''<div class="article-card">
<div class="article-header">
<span class="article-num">🔴 必读 {i}</span>
{f'<span class="article-stock">{stock}</span>' if stock else ''}
</div>
<div class="article-title">{title}</div>
<div class="article-eval">
<span class="eval-label">安全边际：</span><span class="eval-value">{safety}</span>
<span class="eval-label">价值投资：</span><span class="eval-value">{value_fit}</span>
</div>
{f'<div class="article-summary">💡 {summary}</div>' if summary else ''}
{insights_html}
</div>\n'''
    
    return f'''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700&family=Inter:wght@600;700&display=swap" rel="stylesheet">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ margin: 0; }}
.frame {{
    width: 1200px;
    height: 1800px;
    background: #f5f3ed;
    padding: 40px;
    font-family: 'Noto Sans SC', sans-serif;
}}
.header {{
    border-bottom: 4px solid #1a1a1a;
    padding-bottom: 15px;
    margin-bottom: 25px;
}}
.title {{
    font-size: 48px;
    font-weight: 700;
    color: #1a1a1a;
    margin-bottom: 8px;
}}
.date {{
    font-size: 28px;
    color: #666;
}}
.stats {{
    display: flex;
    gap: 20px;
    margin-bottom: 30px;
}}
.stat-box {{
    background: #1a1a1a;
    color: #fff;
    padding: 15px 30px;
    border-radius: 8px;
    text-align: center;
    flex: 1;
}}
.stat-num {{
    font-size: 42px;
    font-weight: 700;
    font-family: 'Inter', sans-serif;
}}
.stat-label {{
    font-size: 16px;
    margin-top: 4px;
}}
.articles {{
    display: flex;
    flex-direction: column;
    gap: 20px;
}}
.article-card {{
    background: #fff;
    padding: 25px 30px;
    border-radius: 10px;
    border: 2px solid #e0e0e0;
}}
.article-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 15px;
    padding-bottom: 12px;
    border-bottom: 3px solid #c41e3a;
}}
.article-num {{
    font-size: 22px;
    font-weight: 700;
    color: #c41e3a;
}}
.article-stock {{
    font-size: 16px;
    color: #666;
    background: #f0f0f0;
    padding: 4px 12px;
    border-radius: 6px;
}}
.article-title {{
    font-size: 20px;
    color: #1a1a1a;
    line-height: 1.5;
    font-weight: 600;
    margin-bottom: 15px;
}}
.article-eval {{
    display: flex;
    gap: 30px;
    margin-bottom: 15px;
    padding: 12px 15px;
    background: #f8f8f8;
    border-radius: 8px;
}}
.eval-label {{
    font-size: 15px;
    color: #666;
}}
.eval-value {{
    font-size: 15px;
    font-weight: 600;
    color: #1a1a1a;
}}
.article-summary {{
    font-size: 16px;
    color: #333;
    line-height: 1.6;
    background: #fff8e6;
    padding: 15px;
    border-radius: 8px;
    margin-bottom: 12px;
    border-left: 4px solid #f5a623;
}}
.insights {{
    padding: 15px;
    background: #f0f7ff;
    border-radius: 8px;
}}
.insights-title {{
    font-size: 15px;
    font-weight: 600;
    color: #1890ff;
    margin-bottom: 10px;
}}
.insight-item {{
    font-size: 14px;
    color: #444;
    line-height: 1.6;
    padding: 6px 0;
    border-bottom: 1px dashed #d9d9d9;
}}
.insight-item:last-child {{
    border-bottom: none;
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
        {articles_html}
    </div>
</div>
</body>
</html>'''


def render_infocard(html: str, output_path: str) -> bool:
    html_file = Path("/tmp/xueqiu_card_temp.html")
    html_file.write_text(html, encoding="utf-8")
    
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 1800})
            page.goto(f"file://{html_file.absolute()}")
            page.screenshot(path=output_path, full_page=False)
            browser.close()
        log(f"卡片生成成功: {output_path}")
        return True
    except Exception as e:
        log(f"卡片生成失败: {e}")
        return False


def check_existing_note(date: str) -> str:
    import urllib.request
    url = "https://ima.qq.com/openapi/note/v1/search_note_book"
    body = {"search_type": 0, "query_info": {"title": f"价值投资日报"}, "start": 0, "end": 10}
    headers = {
        "ima-openapi-clientid": IMA_CLIENT_ID,
        "ima-openapi-apikey": IMA_API_KEY,
        "Content-Type": "application/json"
    }
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
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
                        except:
                            pass
    except Exception as e:
        log(f"搜索笔记异常: {e}")
    return None


def create_ima_note(title: str, content: str) -> str:
    import urllib.request
    date = datetime.now().strftime("%Y-%m-%d")
    existing_doc_id = check_existing_note(date)
    if existing_doc_id:
        log(f"笔记已存在: {existing_doc_id}")
        return existing_doc_id
    
    url = "https://ima.qq.com/openapi/note/v1/import_doc"
    body = {"content_format": 1, "content": content}
    headers = {
        "ima-openapi-clientid": IMA_CLIENT_ID,
        "ima-openapi-apikey": IMA_API_KEY,
        "Content-Type": "application/json"
    }
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 0:
                return result.get("data", {}).get("doc_id")
    except Exception as e:
        log(f"IMA 请求异常: {e}")
    return None


def send_feishu_with_image(message: str, image_path: str = None):
    pending_file = Path("/tmp/pending_feishu_daily.json")
    data = {"channel": "feishu", "target": "user:ou_0451c7608ba9c337b4f92ddc069bb810", "account": "engineer", "message": message}
    pending_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log(f"消息已写入: {pending_file}")
    
    if image_path and Path(image_path).exists():
        img_tool = Path("/root/.openclaw/workspace/skills/feishu-img-tool/feishu-image-tool.js")
        if img_tool.exists():
            result = subprocess.run(["node", str(img_tool), "send", "--target", "ou_0451c7608ba9c337b4f92ddc069bb810", "--file", image_path, "--account", "engineer"], capture_output=True, text=True)
            if result.returncode == 0:
                log("卡片图片已发送")
            else:
                log(f"卡片图片发送失败: {result.stderr}")


def main():
    date = datetime.now().strftime("%Y-%m-%d")
    log(f"开始发布价值投资日报 - {date}")
    
    log("读取日报内容...")
    try:
        content = get_daily_report(date)
    except FileNotFoundError as e:
        log(f"错误: {e}")
        return 1
    
    log("提取卡片信息...")
    card_info = extract_card_info(content)
    log(f"今日新增: {card_info['total_articles']}, 必读: {card_info['must_read']}")
    
    log("生成信息卡片...")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    card_path = str(OUTPUT_DIR / f"xueqiu_card_{date}.png")
    html = generate_infocard_html(card_info)
    render_infocard(html, card_path)
    
    log("创建 IMA 笔记...")
    doc_id = create_ima_note(f"价值投资日报 - {date}", content)
    note_url = f"https://ima.qq.com/note/{doc_id}" if doc_id else None
    
    log("发送飞书消息...")
    message = f"""📊 **价值投资日报 - {date}**

**统计**: 新增 {card_info['total_articles']} 篇，有效 {card_info['valid_analysis']} 篇，必读 {card_info['must_read']} 篇

{f"查看完整日报：{note_url}" if note_url else ""}

---
*分析模型：智谱 GLM-5*"""
    
    send_feishu_with_image(message, card_path)
    log("完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())