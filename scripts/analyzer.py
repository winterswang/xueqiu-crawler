#!/usr/bin/env python3
"""
AI 分析总结模块 - 重构版

使用智谱 GLM-5 模型对投资文章进行深度分析
- 质量检测：内容 > 200 字符才走 GLM-5
- 优先级分类：必读/值得关注/参考
- 详细分析：核心观点、价值投资评估、相关股票
"""

import os
import sys
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from openai import OpenAI
except ImportError:
    print("请安装 openai: pip install openai")
    OpenAI = None


# ============ 质量检测 ============

def check_article_quality(article: dict) -> Tuple[bool, List[str]]:
    """
    质量检测
    
    Returns:
        (passed, issues): 是否通过，问题列表
    """
    issues = []
    
    title = article.get('title', '')
    content = article.get('content', '')
    
    # 标题检测
    if len(title) < 3:
        issues.append("标题过短")
    
    # 内容检测
    if len(content) < 200:
        issues.append(f"内容过短({len(content)}字)")
    
    return len(issues) == 0, issues


def classify_priority(article: dict, analysis: dict = None) -> str:
    """
    优先级分类
    
    Returns:
        'must_read' | 'worth_reading' | 'reference'
    """
    content = article.get('content', '')
    title = article.get('title', '')
    
    # 基于内容长度
    if len(content) > 3000:
        return 'must_read'
    elif len(content) > 1000:
        return 'worth_reading'
    
    # 基于关键词
    deep_keywords = ['估值', '分析', '研究', '财报', '商业模式', '护城河', '安全边际']
    if any(kw in title + content for kw in deep_keywords):
        if len(content) > 500:
            return 'worth_reading'
    
    return 'reference'


# ============ 文章分析器 ============

class ArticleAnalyzer:
    """文章分析器 - GLM-5 深度分析"""
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get('BAILIAN_API_KEY', '')
        self.client = None
        
        if self.api_key and OpenAI:
            self.client = OpenAI(
                api_key=self.api_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
    
    def analyze_article(self, article: dict) -> dict:
        """分析单篇文章"""
        # 质量检测
        passed, issues = check_article_quality(article)
        
        if not passed:
            return {
                'quality_passed': False,
                'issues': issues,
                'priority': 'reference',
                'analysis': None
            }
        
        # GLM-5 分析
        if not self.client:
            return self._mock_analysis(article)
        
        prompt = self._build_prompt(article)
        
        try:
            completion = self.client.chat.completions.create(
                model="glm-5",
                messages=[{"role": "user", "content": prompt}],
            )
            response = completion.choices[0].message.content
            analysis = self._parse_response(response)
            
            # 计算优先级
            priority = classify_priority(article, analysis)
            
            return {
                'quality_passed': True,
                'issues': [],
                'priority': priority,
                'analysis': analysis
            }
        except Exception as e:
            print(f"分析文章失败: {e}")
            return {
                'quality_passed': True,
                'issues': [f"分析失败: {e}"],
                'priority': 'reference',
                'analysis': None
            }
    
    def _build_prompt(self, article: dict) -> str:
        """构建深度分析 Prompt"""
        title = article.get('title', '无标题')
        author = article.get('author', '未知')
        content = article.get('content', '')
        word_count = len(content)
        
        # 截取内容（保留最多 4000 字符）
        content_text = content[:4000]
        
        return f"""你是一位专业的价值投资研究专家，擅长深度分析投资文章。

## 分析任务

请对以下投资文章进行深度结构化分析。

## 文章信息

- **标题**：{title}
- **作者**：{author}
- **字数**：{word_count} 字

## 正文内容

{content_text}

---

## 分析要求

请严格按照以下 JSON 格式输出分析结果：

```json
{{
    "category": "主题归类",
    "related_stocks": ["股票1", "股票2"],
    "core_points": [
        "核心观点1（50字以上）",
        "核心观点2（50字以上）",
        "核心观点3（50字以上）"
    ],
    "value_investment": {{
        "alignment": "是否符合价值投资原则（是/否/部分）",
        "margin_of_safety": "安全边际评估（高/中/低/不确定）",
        "analysis": "详细分析（100字以上）"
    }},
    "insights": [
        "投资启示1（30字以上）",
        "投资启示2（30字以上）"
    ],
    "summary": "一句话总结（30字以内）"
}}
```

## 输出规范

1. **category**：从以下选项中选择
   - 行业分析：行业趋势、竞争格局、发展前景
   - 公司研究：个股分析、估值研究、商业模式
   - 投资理念：价值投资、安全边际、长期主义
   - 宏观经济：经济周期、政策影响、市场环境
   - 其他：不属于以上类别

2. **related_stocks**：提取文中提及的具体股票名称
   - 例如：["中海油(00883.HK)", "携程(TCOM)"]
   - 如果没有提及股票，返回空数组 []

3. **core_points**：提取 3-5 条核心观点
   - 每条观点至少 50 字
   - 观点要有深度，不是简单复述

4. **value_investment**：
   - alignment：判断文章是否符合价值投资原则
   - margin_of_safety：评估安全边际
   - analysis：详细分析原因（100字以上）

5. **insights**：提炼 2-3 条投资启示
   - 每条至少 30 字
   - 要有实操价值

6. **summary**：一句话总结
   - 30 字以内
   - 突出核心要点

请确保输出是有效的 JSON 格式。"""
    
    def _parse_response(self, response: str) -> dict:
        """解析 GLM-5 响应"""
        try:
            # 尝试提取 JSON
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            
            # 尝试直接解析
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except Exception as e:
            print(f"解析响应失败: {e}")
        
        # 返回默认结构
        return {
            'category': '其他',
            'related_stocks': [],
            'core_points': ['解析失败，请查看原文'],
            'value_investment': {
                'alignment': '不确定',
                'margin_of_safety': '不确定',
                'analysis': '解析失败'
            },
            'insights': ['解析失败'],
            'summary': response[:50] if response else '解析失败'
        }
    
    def _mock_analysis(self, article: dict) -> dict:
        """模拟分析（无 API Key 时）"""
        content = article.get('content', '')
        
        category = '其他'
        if any(kw in content for kw in ['估值', 'PE', 'PB', 'ROE', '净利润', '现金流']):
            category = '公司研究'
        elif any(kw in content for kw in ['行业', '赛道', '竞争', '格局']):
            category = '行业分析'
        elif any(kw in content for kw in ['巴菲特', '价值投资', '安全边际', '护城河']):
            category = '投资理念'
        
        return {
            'quality_passed': True,
            'issues': ['未配置 API Key'],
            'priority': classify_priority(article),
            'analysis': {
                'category': category,
                'related_stocks': [],
                'core_points': ['需配置 API Key 进行完整分析'],
                'value_investment': {
                    'alignment': '待分析',
                    'margin_of_safety': '待分析',
                    'analysis': '请配置 BAILIAN_API_KEY 环境变量'
                },
                'insights': ['配置 API Key 后可获取完整分析'],
                'summary': article.get('title', '')[:30]
            }
        }


# ============ 报告生成器 ============

def generate_daily_report(articles: List[dict], results: List[dict], output_path: str = None) -> str:
    """
    生成每日投研分析报告
    
    Args:
        articles: 文章列表
        results: 分析结果列表
        output_path: 输出路径
    """
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 统计
    total = len(articles)
    passed = sum(1 for r in results if r.get('quality_passed'))
    failed = total - passed
    
    must_read = sum(1 for r in results if r.get('priority') == 'must_read')
    worth_reading = sum(1 for r in results if r.get('priority') == 'worth_reading')
    reference = sum(1 for r in results if r.get('priority') == 'reference')
    
    # 股票汇总
    stock_mentions = {}
    for article, result in zip(articles, results):
        if result.get('analysis'):
            for stock in result['analysis'].get('related_stocks', []):
                if stock not in stock_mentions:
                    stock_mentions[stock] = []
                stock_mentions[stock].append({
                    'title': article.get('title', ''),
                    'url': f"https://xueqiu.com/{article.get('user_id', '')}/{article.get('article_id', '')}"
                })
    
    # 构建报告
    lines = [
        f"# 📊 价值投资日报",
        "",
        f"**日期**：{today}",
        "",
        "---",
        "",
        "## 一、概览",
        "",
        f"- **今日新增**：{total} 篇",
        f"- **有效分析**：{passed} 篇（内容 > 200 字）",
        f"- **无效文章**：{failed} 篇（短状态/评论）",
        "",
        "---",
        "",
        "## 二、优先级分类",
        "",
        f"| 优先级 | 数量 | 说明 |",
        f"|--------|------|------|",
        f"| 🔴 必读 | {must_read} | 高质量长文，深度分析 |",
        f"| 🟡 值得关注 | {worth_reading} | 有价值的观点和分析 |",
        f"| 🔵 参考 | {reference} | 短状态、预告类 |",
        "",
        "---",
        "",
        "## 三、文章集合",
        ""
    ]
    
    # 按优先级分组
    priorities = {'must_read': [], 'worth_reading': [], 'reference': []}
    for article, result in zip(articles, results):
        priority = result.get('priority', 'reference')
        priorities[priority].append((article, result))
    
    # 必读文章
    if priorities['must_read']:
        lines.append("### 🔴 必读文章")
        lines.append("")
        for i, (article, result) in enumerate(priorities['must_read'], 1):
            lines.extend(_format_article(i, article, result))
    
    # 值得关注
    if priorities['worth_reading']:
        lines.append("### 🟡 值得关注")
        lines.append("")
        for i, (article, result) in enumerate(priorities['worth_reading'], 1):
            lines.extend(_format_article(i, article, result))
    
    # 参考
    if priorities['reference']:
        lines.append("### 🔵 参考文章")
        lines.append("")
        for i, (article, result) in enumerate(priorities['reference'], 1):
            lines.extend(_format_article_brief(i, article, result))
    
    # 相关股票汇总
    if stock_mentions:
        lines.append("---")
        lines.append("")
        lines.append("## 四、相关股票汇总")
        lines.append("")
        lines.append("| 股票 | 出现次数 | 相关文章 |")
        lines.append("|------|----------|----------|")
        
        for stock, mentions in sorted(stock_mentions.items(), key=lambda x: -len(x[1])):
            article_links = ', '.join([f"[{m['title'][:15]}]({m['url']})" for m in mentions[:3]])
            lines.append(f"| {stock} | {len(mentions)} | {article_links} |")
    
    # 总结
    lines.extend([
        "",
        "---",
        "",
        "## 五、今日总结",
        "",
        f"- **主要话题**：{', '.join(set(r['analysis']['category'] for r in results if r.get('analysis')))}",
        f"- **热门股票**：{', '.join(list(stock_mentions.keys())[:5]) if stock_mentions else '无'}",
        "",
        "---",
        "",
        f"*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        f"*分析模型：智谱 GLM-5*"
    ])
    
    report = '\n'.join(lines)
    
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"报告已保存: {output_path}")
    
    return report


def _format_article(index: int, article: dict, result: dict) -> List[str]:
    """格式化文章详情"""
    lines = []
    
    title = article.get('title', '无标题')
    author = article.get('author', '未知')
    article_id = article.get('article_id', '')
    user_id = article.get('user_id', '')
    url = f"https://xueqiu.com/{user_id}/{article_id}"
    word_count = len(article.get('content', ''))
    
    lines.append(f"#### {index}. {title}")
    lines.append("")
    lines.append(f"- **作者**：{author}")
    lines.append(f"- **链接**：{url}")
    lines.append(f"- **字数**：{word_count} 字")
    
    analysis = result.get('analysis')
    if analysis:
        # 相关股票
        stocks = analysis.get('related_stocks', [])
        if stocks:
            lines.append(f"- **相关股票**：{', '.join(stocks)}")
        
        lines.append("")
        lines.append("**GLM-5 分析：**")
        lines.append("")
        
        # 主题归类
        lines.append(f"- **主题归类**：{analysis.get('category', '其他')}")
        
        # 核心观点
        core_points = analysis.get('core_points', [])
        if core_points:
            lines.append("")
            lines.append("**核心观点：**")
            for j, point in enumerate(core_points[:5], 1):
                lines.append(f"  {j}. {point}")
        
        # 价值投资评估
        vi = analysis.get('value_investment', {})
        if vi:
            lines.append("")
            lines.append("**价值投资评估：**")
            lines.append(f"  - 符合价值投资原则：{vi.get('alignment', '不确定')}")
            lines.append(f"  - 安全边际：{vi.get('margin_of_safety', '不确定')}")
            if vi.get('analysis'):
                lines.append(f"  - 分析：{vi['analysis']}")
        
        # 投资启示
        insights = analysis.get('insights', [])
        if insights:
            lines.append("")
            lines.append("**投资启示：**")
            for insight in insights:
                lines.append(f"  - {insight}")
        
        # 一句话总结
        summary = analysis.get('summary', '')
        if summary:
            lines.append("")
            lines.append(f"**总结**：{summary}")
    
    lines.append("")
    lines.append("---")
    lines.append("")
    
    return lines


def _format_article_brief(index: int, article: dict, result: dict) -> List[str]:
    """格式化文章简要信息"""
    lines = []
    
    title = article.get('title', '无标题')
    author = article.get('author', '未知')
    article_id = article.get('article_id', '')
    user_id = article.get('user_id', '')
    url = f"https://xueqiu.com/{user_id}/{article_id}"
    
    lines.append(f"{index}. [{title}]({url})（{author}）")
    
    if not result.get('quality_passed'):
        lines.append(f"   - ⚠️ {', '.join(result.get('issues', []))}")
    
    return lines


if __name__ == '__main__':
    # 测试
    analyzer = ArticleAnalyzer()
    
    test_article = {
        'title': '中海油估值分析',
        'author': 'czy710',
        'user_id': '6308001210',
        'article_id': '123456',
        'content': '2024年布油均价80美元下，中海油估值分析...' * 100
    }
    
    result = analyzer.analyze_article(test_article)
    print(json.dumps(result, ensure_ascii=False, indent=2))