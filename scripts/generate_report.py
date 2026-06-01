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

# 自动加载 .env（如存在）
from dotenv import load_dotenv
_dotenv_path = Path(__file__).resolve().parent.parent / '.env'
if _dotenv_path.exists():
    load_dotenv(_dotenv_path, override=True)

sys.path.insert(0, str(Path(__file__).parent))

from logging_utils import get_logger, log_execution_stage, log_execution_summary
from analyzer import ArticleAnalyzer, check_article_quality, generate_daily_report


def get_today_articles(data_dir: str = 'data') -> list:
    """获取今日新增文章（索引优先 + 文件系统兜底）"""
    data_path = Path(data_dir)
    index_file = data_path / 'index.json'
    today = datetime.now().strftime('%Y-%m-%d')

    # 第一步：从索引获取
    indexed_article_ids = set()
    articles = []

    if index_file.exists():
        try:
            with open(index_file, 'r', encoding='utf-8') as f:
                index = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"读取索引失败: {e}")
            index = {'articles': {}}

        for index_key, info in index.get('articles', {}).items():
            crawl_time = info.get('crawl_time', '')
            if crawl_time.startswith(today):
                article_id = info.get('article_id', index_key)
                indexed_article_ids.add(article_id)
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
    else:
        index = {'articles': {}}

    # 第二步：文件系统兜底 — 扫描今日修改的 .md 文件（防止索引丢失）
    fs_articles = 0
    for user_dir in data_path.iterdir():
        if not user_dir.is_dir():
            continue
        if user_dir.name in ('daily_reports', 'history'):
            continue
        for md_file in user_dir.glob('*.md'):
            article_id = md_file.stem
            if article_id in indexed_article_ids:
                continue  # 索引已有，跳过
            # 检查文件修改时间是否是今天
            try:
                mtime = datetime.fromtimestamp(md_file.stat().st_mtime)
            except OSError:
                continue
            if mtime.strftime('%Y-%m-%d') != today:
                continue
            try:
                content = md_file.read_text(encoding='utf-8')
            except OSError:
                continue
            # 提取简单元数据
            title = ''
            author = ''
            for line in content.split('\n'):
                if line.startswith('# ') and not title:
                    title = line[2:].strip()
                if '作者：' in line and not author:
                    author = line.split('作者：')[-1].split('|')[0].split('｜')[0].strip()
            articles.append({
                'article_id': article_id,
                'user_id': user_dir.name,
                'title': title,
                'author': author,
                'publish_time': '',
                'content': content,
                'filepath': str(md_file)
            })
            fs_articles += 1

    if fs_articles:
        print(f"文件系统兜底: {fs_articles} 篇（索引中缺失）")

    return articles


def generate_today_report(data_dir: str = 'data', output_path: str = None,
                          api_key: str = None, limit: int = 50) -> str:
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
    # 读取爬取统计（如有）
    crawl_stats = None
    stats_file = Path(data_dir) / '.last_crawl_stats.json'
    if stats_file.exists():
        try:
            crawl_stats = json.loads(stats_file.read_text(encoding='utf-8'))
            logger.info(f"爬取统计: {crawl_stats.get('successful', 0)}/{crawl_stats.get('total_users', 0)} 账号成功")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"读取爬取统计失败: {e}")

    report = generate_daily_report(articles, results, output_path, crawl_stats=crawl_stats)
    
    # 输出统计
    passed = sum(1 for r in results if r.get('quality_passed'))
    must_read = sum(1 for r in results if r.get('priority') == 'must_read')
    worth_reading = sum(1 for r in results if r.get('priority') == 'worth_reading')
    
    # 记录分析器统计
    analyzer_stats = analyzer.get_stats()
    
    total_latency_ms = analyzer_stats.get("total_latency_ms", 0)
    success_calls = analyzer_stats.get("success_calls", 0)
    avg_latency = f"{total_latency_ms / success_calls / 1000:.1f}s" if success_calls else "N/A"
    
    summary = {
        "total_articles": len(articles),
        "passed_analysis": passed,
        "must_read": must_read,
        "worth_reading": worth_reading,
        "llm_calls": analyzer_stats.get("total_calls", 0),
        "success_calls": analyzer_stats.get("success_calls", 0),
        "retry_count": analyzer_stats.get("retry_count", 0),
        "avg_latency": avg_latency,
        "parse_success": analyzer_stats.get("parse_success", 0),
        "parse_failed": analyzer_stats.get("parse_failed", 0),
        "api_errors": analyzer_stats.get("api_errors", 0),
        "total_latency_ms": total_latency_ms,
        "output_path": output_path,
    }
    log_execution_summary(summary)
    logger.info(
        f"报告生成完成: {len(articles)}篇, 有效{passed}篇, 必读{must_read}篇, "
        f"LLM调用{analyzer_stats.get('total_calls',0)}次, "
        f"成功{analyzer_stats.get('success_calls',0)}次, 重试{analyzer_stats.get('retry_count',0)}次, "
        f"平均延迟{avg_latency}, "
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
    parser.add_argument("--limit", type=int, default=50, help="最大分析文章数")

    args = parser.parse_args()
    generate_today_report(
        data_dir=args.data_dir,
        output_path=args.output,
        api_key=args.api_key,
        limit=args.limit,
    )


if __name__ == '__main__':
    main()