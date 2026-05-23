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

sys.path.insert(0, str(Path(__file__).parent))

from logging_utils import get_logger, log_execution_stage, log_execution_summary
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
    logger = get_logger()
    logger.info("=" * 50)
    logger.info("开始生成每日分析报告")
    
    # 获取今日文章
    articles = get_today_articles(data_dir)

    if not articles:
        logger.info("今日无新增文章")
        return ""

    logger.info(f"今日新增文章: {len(articles)} 篇")

    if len(articles) > limit:
        logger.info(f"限制分析前 {limit} 篇（共 {len(articles)} 篇）")
        articles = articles[:limit]

    # 初始化分析器（从 config.yaml 读取模型配置）
    config_path = Path(__file__).resolve().parent.parent / 'config' / 'config.yaml'
    cfg = yaml.safe_load(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    analyzer = ArticleAnalyzer(api_key=api_key, config=cfg)

    # 分析每篇文章
    results = []
    for i, article in enumerate(articles):
        title = article.get('title', '')[:40]
        logger.debug(f"分析 [{i+1}/{len(articles)}]: {title}")

        result = analyzer.analyze_article(article)
        results.append(result)

        # 输出状态
        if result.get('quality_passed'):
            priority = result.get('priority', 'reference')
            priority_emoji = {'must_read': '🔴', 'worth_reading': '🟡', 'reference': '🔵'}
            status = f"{priority_emoji.get(priority, '🔵')} {priority}"
            logger.debug(f"  ✅ {title}: {status}")
        else:
            issues = result.get('issues', [])
            logger.debug(f"  ⚠️ {title}: 跳过 - {', '.join(issues)}")

    # 生成报告
    today = datetime.now().strftime('%Y-%m-%d')
    output_path = output_path or f"{data_dir}/daily_reports/{today}.md"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    report = generate_daily_report(articles, results, output_path)
    
    # 输出统计
    passed = sum(1 for r in results if r.get('quality_passed'))
    must_read = sum(1 for r in results if r.get('priority') == 'must_read')
    worth_reading = sum(1 for r in results if r.get('priority') == 'worth_reading')
    
    # 记录分析器统计
    analyzer_stats = analyzer.get_stats()
    summary = {
        "total_articles": len(articles),
        "passed_analysis": passed,
        "must_read": must_read,
        "worth_reading": worth_reading,
        "llm_calls": analyzer_stats.get("total_calls", 0),
        "parse_success": analyzer_stats.get("parse_success", 0),
        "parse_failed": analyzer_stats.get("parse_failed", 0),
        "api_errors": analyzer_stats.get("api_errors", 0),
        "output_path": output_path,
    }
    log_execution_summary(summary)
    logger.info(
        f"报告生成完成: {len(articles)}篇, 有效{passed}篇, 必读{must_read}篇, "
        f"LLM调用{analyzer_stats.get('total_calls',0)}次, "
        f"解析成功{analyzer_stats.get('parse_success',0)}次, 解析失败{analyzer_stats.get('parse_failed',0)}次"
    )
    
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