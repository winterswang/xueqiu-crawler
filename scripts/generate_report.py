#!/usr/bin/env python3
"""
每日分析报告生成器 - 重构版

配合新的 analyzer.py 使用
"""

import os
import sys
import json
import yaml
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analyzer import ArticleAnalyzer, check_article_quality, generate_daily_report


def get_today_articles(data_dir: str = 'data') -> list:
    """获取今日新增文章"""
    data_path = Path(data_dir)
    index_file = data_path / 'index.json'
    
    if not index_file.exists():
        return []
    
    try:
        with open(index_file, 'r', encoding='utf-8') as f:
            index = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"读取索引失败: {e}")
        return []
    
    articles = []
    today = datetime.now().strftime('%Y-%m-%d')
    
    for article_id, info in index.get('articles', {}).items():
        crawl_time = info.get('crawl_time', '')
        if crawl_time.startswith(today):
            filepath = info.get('filepath', '')
            if filepath and Path(filepath).exists():
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                except OSError as e:
                    print(f"读取文章失败 {filepath}: {e}")
                    continue
                
                articles.append({
                    'article_id': article_id,
                    'user_id': info.get('user_id', ''),
                    'title': info.get('title', ''),
                    'author': info.get('author', ''),
                    'publish_time': info.get('publish_time', ''),
                    'content': content,
                    'filepath': filepath
                })
    
    return articles


def generate_today_report(data_dir: str = 'data', output_path: str = None,
                          api_key: str = None, limit: int = 20) -> str:
    """
    分析今日文章并生成日报（可 import 调用）

    Args:
        data_dir: 数据目录
        output_path: 日报输出路径，默认 data/daily_reports/{today}.md
        api_key: 可选 API Key
        limit: 最大分析文章数

    Returns:
        报告 Markdown 文本
    """
    # 获取今日文章
    articles = get_today_articles(data_dir)

    if not articles:
        print("今日无新增文章")
        return ""

    print(f"今日新增文章: {len(articles)} 篇")

    if len(articles) > limit:
        print(f"限制分析前 {limit} 篇（共 {len(articles)} 篇）")
        articles = articles[:limit]

    # 初始化分析器（从 config.yaml 读取模型配置）
    config_path = Path(__file__).parent.parent / 'config' / 'config.yaml'
    cfg = yaml.safe_load(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    analyzer = ArticleAnalyzer(api_key=api_key, config=cfg)

    # 分析每篇文章
    results = []
    for i, article in enumerate(articles):
        title = article.get('title', '')[:30]
        print(f"分析 [{i+1}/{len(articles)}]: {title}...")

        result = analyzer.analyze_article(article)
        results.append(result)

        # 输出状态
        if result.get('quality_passed'):
            priority = result.get('priority', 'reference')
            priority_emoji = {'must_read': '🔴', 'worth_reading': '🟡', 'reference': '🔵'}
            print(f"  ✅ {priority_emoji.get(priority, '🔵')} {priority}")
        else:
            issues = result.get('issues', [])
            print(f"  ⚠️ 跳过: {', '.join(issues)}")

    # 生成报告
    today = datetime.now().strftime('%Y-%m-%d')
    output_path = output_path or f"{data_dir}/daily_reports/{today}.md"
    report = generate_daily_report(articles, results, output_path)
    
    print("\n" + "="*50)
    print("报告预览:")
    print("="*50)
    print(report[:1000] + "...")
    
    # 输出统计
    passed = sum(1 for r in results if r.get('quality_passed'))
    must_read = sum(1 for r in results if r.get('priority') == 'must_read')
    worth_reading = sum(1 for r in results if r.get('priority') == 'worth_reading')
    
    print("\n" + "="*50)
    print("统计:")
    print(f"  总文章: {len(articles)}")
    print(f"  有效分析: {passed}")
    print(f"  必读: {must_read}")
    print(f"  值得关注: {worth_reading}")

    return report


def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description='生成每日投研分析报告')
    parser.add_argument('--data-dir', default='data', help='数据目录')
    parser.add_argument('--output', default=None, help='输出路径')
    parser.add_argument('--api-key', default=None, help='百炼 API Key')
    parser.add_argument('--limit', type=int, default=20, help='最大分析文章数')

    args = parser.parse_args()
    generate_today_report(
        data_dir=args.data_dir,
        output_path=args.output,
        api_key=args.api_key,
        limit=args.limit,
    )


if __name__ == '__main__':
    main()