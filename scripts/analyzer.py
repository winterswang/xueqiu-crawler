#!/usr/bin/env python3
"""
AI 分析总结模块

使用 GLM-5 模型对新增文章进行分析总结
"""

import os
import sys
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from openai import OpenAI
except ImportError:
    print("请安装 openai: pip install openai")
    OpenAI = None


class ArticleAnalyzer:
    """文章分析器"""
    
    def __init__(self, api_key: str = None):
        """
        初始化分析器
        
        Args:
            api_key: 百炼 API Key（优先从参数读取，其次从环境变量）
        """
        self.api_key = api_key or os.environ.get('BAILIAN_API_KEY', '')
        self.client = None
        
        if self.api_key and OpenAI:
            self.client = OpenAI(
                api_key=self.api_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
        
    def analyze_article(self, article: dict) -> dict:
        """分析单篇文章"""
        if not self.client:
            return self._mock_analysis(article)
        
        prompt = self._build_prompt(article)
        
        try:
            completion = self.client.chat.completions.create(
                model="glm-5",
                messages=[{"role": "user", "content": prompt}],
            )
            response = completion.choices[0].message.content
            return self._parse_response(response)
        except Exception as e:
            print(f"分析文章失败: {e}")
            return self._mock_analysis(article)
    
    def _build_prompt(self, article: dict) -> str:
        """构建分析提示词"""
        title = article.get('title', '无标题')
        author = article.get('author', '未知')
        content = article.get('content', '')[:3000]
        
        return f"""你是一位价值投资研究助手，请分析以下投资文章：

标题：{title}
作者：{author}

正文：
{content}

请从以下维度分析，以 JSON 格式返回：
{{
    "category": "主题归类（行业分析/公司研究/投资理念/宏观经济/其他）",
    "core_points": ["观点1", "观点2", "观点3"],
    "value_investment_view": {{
        "alignment": "是否符合价值投资原则",
        "margin_of_safety": "安全边际评估（高/中/低/不确定）",
        "analysis": "简短分析"
    }},
    "insights": ["认知启发1", "认知启发2"],
    "summary": "一句话总结"
}}"""
    
    def _parse_response(self, response: str) -> dict:
        """解析响应"""
        import re
        try:
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except:
            pass
        return {
            'category': '其他',
            'core_points': [],
            'value_investment_view': {'alignment': '不确定', 'margin_of_safety': '不确定', 'analysis': ''},
            'insights': [],
            'summary': response[:100]
        }
    
    def _mock_analysis(self, article: dict) -> dict:
        """模拟分析"""
        content = article.get('content', '')
        category = '其他'
        if any(kw in content for kw in ['估值', 'PE', 'PB', 'ROE', '净利润']):
            category = '公司研究'
        elif any(kw in content for kw in ['行业', '赛道']):
            category = '行业分析'
        elif any(kw in content for kw in ['巴菲特', '价值投资', '安全边际']):
            category = '投资理念'
        
        return {
            'category': category,
            'core_points': ['需配置 API Key 进行完整分析'],
            'value_investment_view': {'alignment': '待分析', 'margin_of_safety': '待分析', 'analysis': ''},
            'insights': ['配置 BAILIAN_API_KEY 环境变量可获取完整分析'],
            'summary': article.get('title', '')[:50]
        }


def generate_daily_report(articles: List[dict], analyses: List[dict], output_path: str = None) -> str:
    """生成每日分析报告"""
    today = datetime.now().strftime('%Y-%m-%d')
    
    categories = {}
    for analysis in analyses:
        cat = analysis.get('category', '其他')
        categories[cat] = categories.get(cat, 0) + 1
    
    lines = [
        f"# 📊 每日投研总结 - {today}",
        "",
        f"## 📰 今日新增文章",
        "",
        f"共爬取 **{len(articles)}** 篇新文章。",
        "",
        "### 🏷️ 主题分布",
        "",
        "| 主题 | 数量 |",
        "|------|------|",
    ]
    
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        lines.append(f"| {cat} | {count} |")
    
    lines.extend(["", "### 💡 核心观点摘录", ""])
    
    for i, (article, analysis) in enumerate(zip(articles, analyses)):
        title = article.get('title', '无标题')
        author = article.get('author', '未知')
        summary = analysis.get('summary', '')
        core_points = analysis.get('core_points', [])
        
        lines.append(f"#### {i+1}. {title}（{author}）")
        lines.append(f"> {summary}")
        if core_points:
            lines.append("**核心观点：**")
            for point in core_points[:3]:
                lines.append(f"- {point}")
        lines.append("")
    
    lines.extend([
        "---",
        f"*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        "*分析模型：智谱 GLM-5*"
    ])
    
    report = '\n'.join(lines)
    
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"报告已保存: {output_path}")
    
    return report


if __name__ == '__main__':
    analyzer = ArticleAnalyzer()
    
    test_article = {
        'title': '中海油估值分析',
        'author': 'czy710',
        'content': '2024年布油均价80美元下，中海油估值分析...'
    }
    
    result = analyzer.analyze_article(test_article)
    print(json.dumps(result, ensure_ascii=False, indent=2))
