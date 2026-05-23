#!/usr/bin/env python3
"""
文章质量检测模块 — 统一包装

所有实现已合并到 analyzer.py 的 check_article_quality。
本文件保留为兼容包装，新代码请直接 from analyzer import check_article_quality。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from analyzer import check_article_quality  # 统一入口


class QualityLogger:
    """质量检测日志记录器"""

    def __init__(self, log_dir: str = 'logs/quality_check'):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.today = datetime.now().strftime('%Y-%m-%d')
        self.log_file = self.log_dir / f'{self.today}.json'
        self.data = self._load_log()

    def _load_log(self) -> dict:
        """加载日志"""
        if self.log_file.exists():
            with open(self.log_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {
            'date': self.today,
            'total_articles': 0,
            'passed': 0,
            'failed': 0,
            'issues': []
        }

    def log_article(self, article: dict, passed: bool, issues: List[str]):
        """记录文章检测结果"""
        self.data['total_articles'] += 1

        if passed:
            self.data['passed'] += 1
        else:
            self.data['failed'] += 1
            self.data['issues'].append({
                'article_id': article.get('article_id', 'unknown'),
                'user_id': article.get('user_id', ''),
                'title': article.get('title', '')[:50],
                'issues': issues,
                'content_length': len(article.get('content', ''))
            })

    def save(self):
        """保存日志"""
        with open(self.log_file, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        print(f"质量检测日志已保存: {self.log_file}")
        print(f"  总数: {self.data['total_articles']}, 通过: {self.data['passed']}, 失败: {self.data['failed']}")

    def get_summary(self) -> str:
        """获取摘要"""
        lines = [
            f"## 📋 质量检测报告 - {self.today}",
            "",
            f"- 总文章数: {self.data['total_articles']}",
            f"- 通过: {self.data['passed']}",
            f"- 失败: {self.data['failed']}",
        ]

        if self.data['issues']:
            lines.append("")
            lines.append("### ⚠️ 异常文章")
            lines.append("")
            for issue in self.data['issues'][:10]:
                lines.append(f"- **{issue['article_id']}**: {', '.join(issue['issues'])}")

        return '\n'.join(lines)


if __name__ == '__main__':
    # 测试
    test_article = {
        'article_id': 'test123',
        'user_id': '6308001210',
        'title': '',
        'content': '短内容',
        'author': 'czy710'
    }

    passed, issues = check_article_quality(test_article)
    print(f"合格: {passed}, 问题: {issues}")
