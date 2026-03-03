#!/usr/bin/env python3
"""
每日分析报告生成器

在爬虫运行后调用，生成每日投研总结
"""

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from analyzer import ArticleAnalyzer, generate_daily_report
from quality_check import check_article_quality, QualityLogger


def get_today_articles(data_dir: str = 'data') -> list:
    """获取今日新增文章"""
    data_path = Path(data_dir)
    index_file = data_path / 'index.json'
    
    if not index_file.exists():
        return []
    
    import json
    with open(index_file, 'r', encoding='utf-8') as f:
        index = json.load(f)
    
    articles = []
    today = datetime.now().strftime('%Y-%m-%d')
    
    for article_id, info in index.get('articles', {}).items():
        crawl_time = info.get('crawl_time', '')
        if crawl_time.startswith(today):
            filepath = info.get('filepath', '')
            if filepath and Path(filepath).exists():
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                articles.append({
                    'article_id': article_id,
                    'title': info.get('title', ''),
                    'author': info.get('author', ''),
                    'publish_time': info.get('publish_time', ''),
                    'content': content,
                    'filepath': filepath
                })
    
    return articles


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='生成每日投研分析报告')
    parser.add_argument('--data-dir', default='data', help='数据目录')
    parser.add_argument('--output', default=None, help='输出路径')
    parser.add_argument('--api-key', default=None, help='GLM-5 API Key')
    
    args = parser.parse_args()
    
    # 获取今日文章
    articles = get_today_articles(args.data_dir)
    
    if not articles:
        print("今日无新增文章")
        return
    
    print(f"今日新增文章: {len(articles)} 篇")
    
    # 初始化质量检测日志
    quality_logger = QualityLogger(f'{args.data_dir}/../logs/quality_check')
    
    # 初始化分析器
    analyzer = ArticleAnalyzer(api_key=args.api_key)
    
    # 分析每篇文章
    analyses = []
    passed_articles = []
    
    for i, article in enumerate(articles):
        print(f"检测文章 [{i+1}/{len(articles)}]: {article.get('title', '')[:30]}...")
        
        # 质量检测
        passed, issues = check_article_quality(article)
        quality_logger.log_article(article, passed, issues)
        
        if passed:
            print(f"  ✅ 质量检测通过")
            analysis = analyzer.analyze_article(article)
            analyses.append(analysis)
            passed_articles.append(article)
        else:
            print(f"  ❌ 质量检测失败: {issues}")
    
    # 保存质量检测日志
    quality_logger.save()
    
    if not passed_articles:
        print("没有通过质量检测的文章，无法生成报告")
        return
    
    # 生成报告
    today = datetime.now().strftime('%Y-%m-%d')
    output_path = args.output or f"{args.data_dir}/daily_reports/{today}.md"
    
    report = generate_daily_report(passed_articles, analyses, output_path)
    
    print("\n" + "="*50)
    print("报告预览:")
    print("="*50)
    print(report[:500] + "...")
    
    # 输出质量摘要
    print("\n" + "="*50)
    print(quality_logger.get_summary())


if __name__ == '__main__':
    main()