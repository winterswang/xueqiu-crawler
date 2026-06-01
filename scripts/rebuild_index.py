#!/usr/bin/env python3
"""
索引恢复工具 — 从已保存的 .md 文件重建 index.json

用法:
  python3 scripts/rebuild_index.py              # 预览（dry-run）
  python3 scripts/rebuild_index.py --apply       # 实际写入
  python3 scripts/rebuild_index.py --backup      # 修复前备份 index.json

用于修复因 OOM/SIGKILL 导致的 index.json 丢失问题。
"""

import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime


def extract_metadata(filepath: Path) -> dict:
    """从 Markdown 文件提取元数据"""
    try:
        content = filepath.read_text(encoding='utf-8')
    except OSError:
        return {}

    meta = {}
    # 提取标题（第一行 # 开头）
    for line in content.split('\n'):
        if line.startswith('# '):
            meta['title'] = line[2:].strip()
            break

    # 提取元数据行：> 作者：xxx | 发布时间：xxx
    meta_line = ''
    for line in content.split('\n'):
        if line.startswith('> 作者：') or line.startswith('> 原文链接：'):
            meta_line += line[2:] + ' '
    # 解析 作者：xxx | 发布时间：xxx
    author_match = re.search(r'作者：(.+?)\s*[|｜]', meta_line)
    if author_match:
        meta['author'] = author_match.group(1).strip()
    time_match = re.search(r'发布时间：(.+?)(?:\s*[|｜]|\s*$)', meta_line)
    if time_match:
        meta['publish_time'] = time_match.group(1).strip()

    # 提取爬取时间（最后一行）
    crawl_match = re.search(r'\*爬取时间：(.+?)\*', content)
    if crawl_match:
        meta['crawl_time'] = crawl_match.group(1).strip()

    return meta


def rebuild_index(data_dir: str = 'data', apply: bool = False, backup: bool = True):
    """重建 index.json"""
    data_path = Path(data_dir)
    index_file = data_path / 'index.json'

    # 加载现有索引
    if index_file.exists():
        existing = json.loads(index_file.read_text(encoding='utf-8'))
        articles = existing.get('articles', {})
    else:
        existing = {'articles': {}, 'last_update': None, 'history': {}}
        articles = {}

    # 备份
    if backup and apply and index_file.exists():
        backup_file = index_file.with_suffix('.json.bak')
        backup_file.write_text(index_file.read_text(encoding='utf-8'))
        print(f"已备份: {backup_file}")

    # 扫描所有 user_id 目录
    new_count = 0
    exist_count = 0
    skip_count = 0

    for user_dir in sorted(data_path.iterdir()):
        if not user_dir.is_dir():
            continue
        if user_dir.name in ('daily_reports', 'history'):
            continue

        user_id = user_dir.name
        md_files = list(user_dir.glob('*.md'))
        if not md_files:
            continue

        for md_file in md_files:
            article_id = md_file.stem
            index_key = f"{user_id}_{article_id}"

            if index_key in articles:
                exist_count += 1
                continue

            meta = extract_metadata(md_file)
            if not meta:
                skip_count += 1
                continue

            articles[index_key] = {
                'article_id': article_id,
                'user_id': user_id,
                'title': meta.get('title', ''),
                'author': meta.get('author', ''),
                'publish_time': meta.get('publish_time', ''),
                'crawl_time': meta.get('crawl_time', ''),
                'file_path': str(md_file),
                'filepath': str(md_file),
            }
            new_count += 1

    if apply:
        existing['articles'] = articles
        existing['last_update'] = datetime.now().isoformat()
        index_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"\n✅ 索引已更新:")
    else:
        print(f"\n🔍 预览模式 (加 --apply 执行写入):")

    print(f"  现有文章: {exist_count}")
    print(f"  新增补录: {new_count}")
    print(f"  跳过(无元数据): {skip_count}")
    print(f"  索引总计: {len(articles)}")

    return new_count


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='从 .md 文件重建 index.json')
    parser.add_argument('--apply', action='store_true', help='实际写入（默认 dry-run）')
    parser.add_argument('--backup', action='store_true', default=True, help='修复前备份')
    parser.add_argument('--data-dir', default='data', help='数据目录')
    args = parser.parse_args()

    rebuild_index(args.data_dir, apply=args.apply, backup=args.backup)
