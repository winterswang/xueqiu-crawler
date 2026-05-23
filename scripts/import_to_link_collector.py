#!/usr/bin/env python3
"""
xueqiu-crawler 批量导入脚本

将现有的 409 篇文章导入到 Link-Collector 知识库
直接读取本地文件，不重新爬取
"""

import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime

# 添加路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "link-collector"))

from link_collector import CollectorService
from link_collector.models import (
    ArticleMeta, ArticleContent, Source, SourceType,
    Classification, Category, SubCategory,
    ImportanceInfo, Importance,
    Timestamps, Relations
)
from link_collector.classifier import Classifier


def load_xueqiu_index(xueqiu_data_dir: Path) -> dict:
    """加载 xueqiu-crawler 的 index.json"""
    index_file = xueqiu_data_dir / "index.json"
    if index_file.exists():
        with open(index_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"articles": {}}


def parse_article_file(file_path: Path) -> dict:
    """解析文章 MD 文件"""
    if not file_path.exists():
        return None
    
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 解析元数据
    title = ""
    author = ""
    publish_time = ""
    article_content = ""
    likes = 0
    comments = 0
    
    lines = content.split('\n')
    
    # 提取标题（第一个 # 开头的行）
    for i, line in enumerate(lines):
        if line.startswith('# '):
            title = line[2:].strip()
            break
    
    # 提取作者（格式：> 作者：@xxx | ...）
    for line in lines:
        if '作者' in line and '@' in line:
            match = re.search(r'@(\S+)', line)
            if match:
                author = match.group(1)
            break
    
    # 提取发布时间（格式：发布于2026-03-14 20:30）
    for line in lines:
        if '发布于' in line or '发布时间' in line:
            match = re.search(r'(\d{4}-\d{2}-\d{2})', line)
            if match:
                publish_time = match.group(1)
            break
    
    # 提取点赞/评论
    for line in lines:
        if '点赞' in line:
            match = re.search(r'点赞：(\d+)', line)
            if match:
                likes = int(match.group(1))
        if '评论' in line:
            match = re.search(r'评论：(\d+)', line)
            if match:
                comments = int(match.group(1))
    
    # 提取正文（--- 分隔后的内容，去除爬取时间）
    # 找到第一个 --- 之后的内容
    content_start = False
    content_lines = []
    for line in lines:
        if line.strip() == '---':
            if content_start:
                # 已经在正文区域，遇到第二个 --- 结束
                break
            else:
                content_start = True
                continue
        if content_start:
            # 去除爬取时间
            if '*爬取时间' in line:
                break
            content_lines.append(line)
    
    article_content = '\n'.join(content_lines).strip()
    
    return {
        "title": title,
        "author": author,
        "publish_time": publish_time,
        "content": article_content,
        "likes": likes,
        "comments": comments
    }


def get_author_name(user_id: str, accounts: list) -> str:
    """获取作者名称"""
    for acc in accounts:
        if acc.get("id") == user_id:
            return acc.get("name", "")
    return ""


def import_articles(xueqiu_data_dir: Path, service: CollectorService, 
                    classifier: Classifier, accounts: list, 
                    limit: int = None, today_only: bool = False):
    """
    导入文章到 Link-Collector
    
    Args:
        xueqiu_data_dir: xueqiu-crawler 数据目录
        service: Link-Collector 服务
        classifier: 分类器
        accounts: 账号列表
        limit: 限制导入数量（用于测试）
        today_only: 只导入今天爬取的文章
    """
    from datetime import datetime
    
    index = load_xueqiu_index(xueqiu_data_dir)
    articles = index.get("articles", {})
    
    # 今天的日期
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # 如果只要今天的，先统计
    if today_only:
        today_articles = {
            k: v for k, v in articles.items() 
            if v.get("crawl_time", "").startswith(today_str)
        }
        print(f"📚 今日新爬取 {len(today_articles)} 篇文章")
        articles = today_articles
    else:
        print(f"📚 发现 {len(articles)} 篇文章")
    
    success_count = 0
    error_count = 0
    skip_count = 0
    
    for i, (article_id, info) in enumerate(articles.items(), 1):
        if limit and i > limit:
            break
        
        user_id = info.get("user_id", "")
        title = info.get("title", "无标题")
        crawl_time = info.get("crawl_time", "")
        
        # 文件路径
        article_file = xueqiu_data_dir / user_id / f"{article_id}.md"
        
        if not article_file.exists():
            skip_count += 1
            continue
        
        # 解析文章
        parsed = parse_article_file(article_file)
        if not parsed:
            error_count += 1
            continue
        
        content = parsed.get("content", "")
        
        # 跳过太短的文章
        if len(content) < 200:
            skip_count += 1
            continue
        
        # 分类
        try:
            classification = classifier.classify(
                parsed.get("title", ""),
                content,
                f"https://xueqiu.com/{user_id}/{article_id}"
            )
            
            # 评估重要性
            importance_info = classifier.calculate_importance(
                parsed.get("title", ""),
                content,
                classification
            )
            
            # 构建元数据
            from uuid import uuid4
            
            author_name = get_author_name(user_id, accounts) or parsed.get("author", "")
            
            meta = ArticleMeta(
                id=str(uuid4()),
                title=parsed.get("title", "无标题"),
                source=Source(
                    type=SourceType.WEB,
                    url=f"https://xueqiu.com/{user_id}/{article_id}",
                    platform="xueqiu",
                    author=author_name,
                    author_id=user_id,
                    original_id=article_id
                ),
                classification=classification,
                importance=importance_info,
                content=ArticleContent(
                    word_count=len(content),
                    raw_content=content[:10000]  # 限制长度
                ),
                timestamps=Timestamps(
                    created=datetime.now(),
                    published=parsed.get("publish_time")
                )
            )
            
            # 保存到 Link-Collector（不额外保存副本）
            paths = service._save_article(meta, {})
            
            # 更新索引
            service.indexer.add_article(meta.to_dict(), paths.get("primary", ""))
            
            success_count += 1
            
            if i % 50 == 0:
                print(f"  [{i}] ✅ 已导入 {success_count} 篇")
                
        except Exception as e:
            print(f"  [{i}] ❌ 失败: {title[:30]}... - {e}")
            error_count += 1
    
    print(f"\n📊 导入完成:")
    print(f"  成功: {success_count} 篇")
    print(f"  跳过: {skip_count} 篇（太短或文件不存在）")
    print(f"  失败: {error_count} 篇")
    
    return success_count, error_count


def load_accounts(xueqiu_dir: Path) -> list:
    """加载账号配置"""
    import yaml
    accounts_file = xueqiu_dir / "config" / "accounts.yaml"
    if accounts_file.exists():
        with open(accounts_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            return data.get("accounts", [])
    return []


def main():
    # xueqiu-crawler 目录（基于脚本位置自动推断）
    xueqiu_dir = Path(__file__).resolve().parent.parent
    xueqiu_data_dir = xueqiu_dir / "data"
    
    # 加载账号配置
    accounts = load_accounts(xueqiu_dir)
    print(f"👥 已加载 {len(accounts)} 个关注账号")
    
    # Link-Collector 服务
    service = CollectorService()
    
    # 分类器
    classifier = Classifier()
    
    # 检查参数
    limit = None
    today_only = False
    
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--test":
            limit = 10
            print("🧪 测试模式：仅导入 10 篇\n")
        elif arg == "--today":
            today_only = True
            print("📅 今日模式：只导入今天爬取的文章\n")
        elif arg.isdigit():
            limit = int(arg)
            print(f"📋 限制模式：导入 {limit} 篇\n")
    
    # 执行导入
    import_articles(xueqiu_data_dir, service, classifier, accounts, limit, today_only)


if __name__ == "__main__":
    main()