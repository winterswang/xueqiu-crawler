#!/usr/bin/env python3
"""
价值投资分析框架 v2

设计理念：从"阅读报告"到"决策支持"

输出结构：
1. 快速筛选层 - 帮助投资者快速判断是否值得深入
2. 标的定位层 - 涉及哪些股票/行业
3. 核心要点层 - 一句话论点 + 关键数据
4. 投资启示层 - 对投资决策的影响
5. 行动建议层 - 下一步做什么
"""

import os
import json
import re
from typing import Dict, List, Tuple
from openai import OpenAI


class ValueInvestmentAnalyzer:
    """价值投资分析器"""
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get('BAILIAN_API_KEY', '')
        self.client = None
        
        if self.api_key:
            self.client = OpenAI(
                api_key=self.api_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
    
    def analyze_article(self, article: dict) -> dict:
        """
        分析单篇文章
        
        返回结构：
        {
            "importance": "high/medium/low",
            "content_type": "深度研究/市场评论/行业动态/投资理念/其他",
            "related_stocks": ["HOOD", "TSLA"],
            "related_industries": ["券商", "AI"],
            "core_thesis": "一句话核心论点",
            "key_data": {"估值": "15x", "增速": "20%"},
            "investment_implications": "对投资决策的影响",
            "action_required": "需要关注/深入研究/验证假设",
            "confidence": "high/medium/low"
        }
        """
        if not self.client:
            return self._mock_analysis(article)
        
        prompt = self._build_analysis_prompt(article)
        
        try:
            completion = self.client.chat.completions.create(
                model="qwen-plus",
                messages=[{"role": "user", "content": prompt}],
            )
            response = completion.choices[0].message.content
            return self._parse_analysis(response)
        except Exception as e:
            print(f"分析失败: {e}")
            return self._mock_analysis(article)
    
    def _build_analysis_prompt(self, article: dict) -> str:
        """构建分析提示词"""
        title = article.get('title', '无标题')
        content = article.get('content', '')[:3000]
        
        return f"""你是一位资深价值投资研究助手，请分析以下投资文章：

文章标题：{title}
文章内容：
{content}

请输出以下结构化分析（JSON格式）：

{{
    "importance": "high/medium/low（重要性评级：是否涉及持仓标的/新投资机会/重大风险）",
    "content_type": "深度研究/市场评论/行业动态/投资理念/其他",
    "related_stocks": ["涉及的股票代码，如HOOD、TSLA"],
    "related_industries": ["涉及的行业"],
    "core_thesis": "一句话概括核心论点（15字以内，直击要点）",
    "key_data": {{
        "估值": "关键估值数据（如PE、PB、DCF等）",
        "财务": "关键财务数据（如营收、净利、增速等）",
        "其他": "其他重要数据（如市场份额、用户数等）"
    }},
    "investment_implications": "对投资决策的启示（40字以内，聚焦行动指导）",
    "action_required": "深入研究/验证假设/关注/无需行动",
    "confidence": "high/medium/low（分析置信度）"
}}

**分析原则：**
1. 精准识别：快速识别文章涉及的标的和行业
2. 论点提炼：提取最核心的投资逻辑，去除噪音
3. 数据提取：关注估值、财务等关键数字
4. 行动导向：给出明确的投资行动建议

**重要性判断标准：**
- high: 涉及持仓标的、重大投资机会、关键风险警示
- medium: 行业趋势、估值参考、方法论启发
- low: 市场评论、个人感悟、信息性内容"""

    def _parse_analysis(self, response: str) -> dict:
        """解析分析结果"""
        try:
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except:
            pass
        
        return {
            "importance": "medium",
            "content_type": "其他",
            "related_stocks": [],
            "related_industries": [],
            "core_thesis": response[:50],
            "key_data": {},
            "investment_implications": "",
            "action_required": "需要关注",
            "confidence": "low"
        }
    
    def _mock_analysis(self, article: dict) -> dict:
        """模拟分析"""
        content = article.get('content', '')
        title = article.get('title', '')
        
        # 提取股票代码
        stocks = re.findall(r'\$([A-Z]+)', content + title)
        
        return {
            "importance": "medium",
            "content_type": "其他",
            "related_stocks": list(set(stocks))[:5],
            "related_industries": [],
            "core_thesis": title[:30] if title else "待分析",
            "key_data": {},
            "investment_implications": "需要配置 API Key 进行完整分析",
            "action_required": "需要关注",
            "confidence": "low"
        }


def generate_investment_report(articles: List[dict], analyses: List[dict], 
                               output_path: str = None) -> str:
    """
    生成价值投资日报
    
    设计理念：内容质量 > 信息堆砌
    """
    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 按重要性分组
    high_importance = []
    medium_importance = []
    low_importance = []
    
    for article, analysis in zip(articles, analyses):
        imp = analysis.get('importance', 'medium')
        if imp == 'high':
            high_importance.append((article, analysis))
        elif imp == 'medium':
            medium_importance.append((article, analysis))
        else:
            low_importance.append((article, analysis))
    
    # 收集所有股票
    all_stocks = set()
    for analysis in analyses:
        for stock in analysis.get('related_stocks', []):
            all_stocks.add(stock)
    
    # 生成报告
    lines = [
        f"# 📊 价值投资日报 - {today}",
        "",
        "## 📰 今日新增文章",
        "",
        f"共爬取 **{len(articles)}** 篇新文章。",
        "",
    ]
    
    # 文章列表
    lines.append("### 文章列表")
    lines.append("")
    for i, (article, analysis) in enumerate(zip(articles, analyses), 1):
        title = article.get('title', '无标题')[:50]
        url = article.get('url', article.get('link', ''))
        stocks = analysis.get('related_stocks', [])
        stocks_str = f" [${', $'.join(stocks)}]" if stocks else ""
        lines.append(f"{i}. [{title}]({url}){stocks_str}")
    lines.append("")
    
    # 关联股票
    if all_stocks:
        lines.append("### 关联股票")
        lines.append("")
        lines.append(f"$**{', $'.join(sorted(all_stocks))}**")
        lines.append("")
    
    # 高优先级
    if high_importance:
        lines.append("## 🔴 高优先级（值得深入研究）")
        lines.append("")
        for article, analysis in high_importance:
            title = article.get('title', '无标题')
            url = article.get('url', article.get('link', ''))
            stocks = ', '.join([f'${s}' for s in analysis.get('related_stocks', [])]) or '无'
            
            lines.append(f"### [{title}]({url})")
            lines.append("")
            lines.append(f"**关联标的：** {stocks}")
            lines.append("")
            lines.append(f"**核心论点：** {analysis.get('core_thesis', '无')}")
            lines.append("")
            lines.append(f"**投资启示：** {analysis.get('investment_implications', '无')}")
            lines.append("")
            
            # 关键数据
            key_data = analysis.get('key_data', {})
            if key_data:
                lines.append("**关键数据：**")
                for k, v in key_data.items():
                    if v:
                        lines.append(f"- {k}: {v}")
                lines.append("")
            
            lines.append(f"**建议行动：** {analysis.get('action_required', '关注')}")
            lines.append("")
            lines.append("---")
            lines.append("")
    
    # 中优先级
    if medium_importance:
        lines.append("## 🟡 中优先级（值得关注）")
        lines.append("")
        for article, analysis in medium_importance:
            title = article.get('title', '无标题')[:40]
            url = article.get('url', article.get('link', ''))
            thesis = analysis.get('core_thesis', '')[:60]
            stocks = ', '.join([f'${s}' for s in analysis.get('related_stocks', [])]) or '无'
            
            lines.append(f"### [{title}]({url})")
            lines.append(f"- **核心论点：** {thesis}")
            lines.append(f"- **关联标的：** {stocks}")
            lines.append(f"- **启示：** {analysis.get('investment_implications', '')[:80]}")
            lines.append("")
    
    # 低优先级
    if low_importance:
        lines.append("## ⚪ 低优先级（信息性内容）")
        lines.append("")
        for article, analysis in low_importance:
            title = article.get('title', '无标题')[:30]
            url = article.get('url', article.get('link', ''))
            lines.append(f"- [{title}]({url})")
        lines.append("")
    
    # 行动清单
    lines.extend([
        "## ✅ 行动清单",
        "",
    ])
    
    actions = {}
    for article, analysis in zip(articles, analyses):
        action = analysis.get('action_required', '关注')
        if action and action != '无需行动':
            if action not in actions:
                actions[action] = []
            actions[action].append(article.get('title', '')[:30])
    
    for action, titles in sorted(actions.items()):
        lines.append(f"### {action}")
        for title in titles[:5]:
            lines.append(f"- {title}")
        if len(titles) > 5:
            lines.append(f"- ... 还有 {len(titles) - 5} 条")
        lines.append("")
    
    lines.extend([
        "---",
        "",
        f"*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        "*分析模型：GLM-5 (qwen-plus)*"
    ])
    
    report = '\n'.join(lines)
    
    if output_path:
        from pathlib import Path
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"报告已保存: {output_path}")
    
    return report


if __name__ == '__main__':
    # 测试
    analyzer = ValueInvestmentAnalyzer()
    
    test_article = {
        'title': '翻开这一页(6)：今非昔比的Robinhood',
        'content': 'Robinhood已从纯零佣金交易工具进化为财富基础设施...'
    }
    
    result = analyzer.analyze_article(test_article)
    print(json.dumps(result, ensure_ascii=False, indent=2))