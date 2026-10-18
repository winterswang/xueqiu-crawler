#!/usr/bin/env python3
"""热榜贴与自选账号文章融合的回归测试（全程离线，不调 LLM、不发请求）。

背景：2026-10-18 之前日报里是两大块 —— 自选账号文章走 LLM 分析进
🔴/🟡/📰/🔵，热榜贴只是 `## 🔥 全网热贴 Top 10` 一个无分析的列表。
现在要求热榜贴也走同一套分析，两大部分合成一个：

1. `hot_top10.json` 的条目按 `status_id == article_id` 去重合并；
2. 热榜独有条目抓全文（撞 WAF / 熔断冷却立即停手，回落首页摘要）；
3. 报告里不再有独立热榜章节，热度以标记形式挂在条目上。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / 'scripts'))

import analyzer as an  # noqa: E402
import generate_report as gr  # noqa: E402

TZ = ZoneInfo('Asia/Shanghai')
BODY = '这是一篇够长的正文，用于通过 200 字质量门。' * 12


def _today() -> str:
    return datetime.now(TZ).strftime('%Y-%m-%d')


def _write_article(data_dir: Path, user_id: str, article_id: str, title: str) -> Path:
    path = data_dir / user_id / f'{article_id}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'# {title}\n\n> 作者：作者{user_id} | 发布时间：10-17 09:00\n\n---\n\n'
        f'{BODY}\n\n---\n\n*爬取时间：2026-10-18 08:00:00*\n',
        encoding='utf-8',
    )
    return path


def _write_index(data_dir: Path, rows: list[tuple[str, str, str, Path]]) -> None:
    today = _today()
    articles = {}
    for user_id, article_id, title, filepath in rows:
        articles[f'{user_id}_{article_id}'] = {
            'article_id': article_id,
            'user_id': user_id,
            'title': title,
            'author': f'作者{user_id}',
            'publish_time': '10-17 09:00',
            'filepath': str(filepath),
            'crawl_time': f'{today}T08:00:00',
        }
    (data_dir / 'index.json').write_text(
        json.dumps({'articles': articles}, ensure_ascii=False), encoding='utf-8'
    )


def _hot_item(status_id: str, rank: int = 1, author: str = '热榜作者', **extra) -> dict:
    item = {
        'status_id': status_id,
        'author': author,
        'author_id': '',
        'url': f'https://xueqiu.com/999/{status_id}',
        'text': '热榜首页摘要，' * 20,
        'created_at': '2026-10-18T07:30:00+08:00',
        'likes': 59,
        'comments': 49,
        'forwards': 0,
        'rank': rank,
        'score': 64.2,
        'is_overnight': False,
        'followed': False,
        'symbols_hit': [],
    }
    item.update(extra)
    return item


def _prepared_hot_item(status_id: str, rank: int = 1, **extra) -> dict:
    """模拟 01:00 prepare job 的产物：全文 + 真实标题已写进 item。"""
    return _hot_item(
        status_id,
        rank,
        content='抓到的完整正文。' * 40,
        content_source='full',
        title=f'真实标题{status_id}',
        **extra,
    )


def _write_hot(data_dir: Path, items: list[dict], raw: str | None = None) -> None:
    out_dir = data_dir / 'hot_posts' / _today()
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = raw or json.dumps(
        {'items': items, 'candidates': len(items), 'window': {}}, ensure_ascii=False
    )
    (out_dir / 'hot_top10.json').write_text(payload, encoding='utf-8')


class FakeAnalyzer:
    """记录分析入参的分析器替身，绝不发起 LLM 调用。"""

    instances: list['FakeAnalyzer'] = []

    def __init__(self, *args, **kwargs):
        self.model_name = 'fake-model'
        self.seen: list[dict] = []
        FakeAnalyzer.instances.append(self)

    def analyze_article(self, article: dict) -> dict:
        self.seen.append(article)
        return {
            'quality_passed': True,
            'issues': [],
            'priority': 'reference',
            'scores': {'total': 20},
            'analysis': {
                'summary': '摘要',
                'category': '公司研究',
                'topic_category': '科技/AI/互联网',
                'related_stocks': [],
                'core_points': ['观点'],
                'deep_analysis': {},
            },
        }

    def get_stats(self) -> dict:
        return {
            'total_calls': len(self.seen),
            'success_calls': len(self.seen),
            'retry_count': 0,
            'parse_success': len(self.seen),
            'parse_failed': 0,
            'api_errors': 0,
            'total_latency_ms': 0,
        }


@pytest.fixture
def fake_analyzer(monkeypatch):
    FakeAnalyzer.instances = []
    monkeypatch.setattr(gr, 'ArticleAnalyzer', FakeAnalyzer)
    return FakeAnalyzer


@pytest.fixture
def no_fetch(monkeypatch):
    """默认配置下 08:00 不该发任何页面请求 —— 全文由 01:00 的 job 准备好。"""

    def _boom(*args, **kwargs):
        raise AssertionError('08:00 生成日报时不该抓正文（fetch_fulltext 默认 false）')

    monkeypatch.setattr(gr, 'enrich_items_with_fulltext', _boom)


def _data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / 'data'
    (data_dir / 'daily_reports').mkdir(parents=True)
    return data_dir


def _run(data_dir: Path, **kwargs):
    out = data_dir / 'daily_reports' / 'report.md'
    report = gr.generate_today_report(
        data_dir=str(data_dir), output_path=str(out), **kwargs
    )
    return report, out


# ── 融合与去重 ──────────────────────────────────────────────────────────


def test_hot_items_merged_before_analysis_and_deduped(
    tmp_path, fake_analyzer, no_fetch
):
    data_dir = _data_dir(tmp_path)
    path = _write_article(data_dir, '111', '123', '自有账号文章')
    _write_index(data_dir, [('111', '123', '自有账号文章', path)])
    _write_hot(
        data_dir,
        [_prepared_hot_item('123', 1), _prepared_hot_item('999', 2)],
    )

    _run(data_dir)

    seen = fake_analyzer.instances[-1].seen
    assert len(seen) == 2, '重合条目只应分析一次，热榜独有条目再新增一次'
    identical = next(a for a in seen if a['article_id'] == '123')
    assert identical['source'] == 'both'
    assert identical['hot']['rank'] == 1
    assert len(identical['content']) > 200, '重合条目必须保留已爬到的全文'
    hot_only = next(a for a in seen if a['article_id'] == '999')
    assert hot_only['source'] == 'hot'
    assert hot_only['content_source'] == 'full'


def test_prepared_fulltext_is_used_without_fetching(tmp_path, fake_analyzer, no_fetch):
    """01:00 已把全文写进 hot_top10.json —— 08:00 一个请求都不该发。"""
    data_dir = _data_dir(tmp_path)
    path = _write_article(data_dir, '111', '123', '自有账号文章')
    _write_index(data_dir, [('111', '123', '自有账号文章', path)])
    _write_hot(
        data_dir,
        [_prepared_hot_item('123', 1), _prepared_hot_item('999', 2)],
    )

    _run(data_dir)

    seen = fake_analyzer.instances[-1].seen
    hot_only = next(a for a in seen if a['article_id'] == '999')
    assert hot_only['content_source'] == 'full'
    assert hot_only['content'].startswith('抓到的完整正文')
    assert hot_only['title'] == '真实标题999'
    # 重合条目保留自选账号爬到的全文，不被热榜版本覆盖
    identical = next(a for a in seen if a['article_id'] == '123')
    assert identical['source'] == 'both'
    assert len(identical['content']) > 200


def test_limit_applies_only_to_account_articles(tmp_path, fake_analyzer, no_fetch):
    data_dir = _data_dir(tmp_path)
    rows = []
    for article_id in ('1', '2', '3'):
        path = _write_article(data_dir, '111', article_id, f'文章{article_id}')
        rows.append(('111', article_id, f'文章{article_id}', path))
    _write_index(data_dir, rows)
    _write_hot(data_dir, [_hot_item('999', 1)])

    _run(data_dir, limit=2)

    seen = fake_analyzer.instances[-1].seen
    assert [a['article_id'] for a in seen] == ['1', '2', '999']


# ── 降级路径 ────────────────────────────────────────────────────────────


def test_missing_hot_json_degrades(tmp_path, fake_analyzer):
    data_dir = _data_dir(tmp_path)
    path = _write_article(data_dir, '111', '123', '自有账号文章')
    _write_index(data_dir, [('111', '123', '自有账号文章', path)])

    report, out = _run(data_dir)

    assert out.exists()
    assert len(fake_analyzer.instances[-1].seen) == 1
    assert '🔥 全网热贴 Top 10' not in report


def test_corrupt_hot_json_degrades(tmp_path, fake_analyzer):
    data_dir = _data_dir(tmp_path)
    path = _write_article(data_dir, '111', '123', '自有账号文章')
    _write_index(data_dir, [('111', '123', '自有账号文章', path)])
    _write_hot(data_dir, [], raw='{not json')

    report, out = _run(data_dir)

    assert out.exists()
    assert '价值投资日报' in report


def test_empty_articles_branch_ignores_hot(tmp_path, monkeypatch):
    """用户拍板的边界：当天自有账号 0 篇时仍走「今日无新增」最小日报，热榜不参与。"""
    data_dir = _data_dir(tmp_path)
    (data_dir / 'index.json').write_text('{"articles": {}}', encoding='utf-8')
    _write_hot(data_dir, [_hot_item('999', 1)])

    monkeypatch.setattr(
        gr,
        'ArticleAnalyzer',
        lambda *a, **kw: pytest.fail('空数据路径不该实例化分析器'),
    )
    report, out = _run(data_dir)

    assert out.exists()
    assert '🔥 热榜第1' not in report


# ── 报告渲染 ────────────────────────────────────────────────────────────


def _article(article_id: str, **extra) -> dict:
    base = {
        'article_id': article_id,
        'user_id': '111',
        'title': f'标题{article_id}',
        'author': f'作者{article_id}',
        'publish_time': '10-17 09:00',
        'content': BODY,
    }
    base.update(extra)
    return base


def _result(priority: str = 'reference', total: int = 40) -> dict:
    return {
        'quality_passed': True,
        'issues': [],
        'priority': priority,
        'scores': {'total': total},
        'analysis': {
            'summary': '一句话总结',
            'category': '公司研究',
            'topic_category': '科技/AI/互联网',
            'related_stocks': [],
            'core_points': ['观点一'],
            'deep_analysis': {},
        },
    }


HOT_META = {
    'rank': 3,
    'site_rank': 5,
    'score': 64.2,
    'likes': 99,
    'comments': 214,
    'forwards': 2,
    'is_overnight': True,
    'followed': False,
    'symbols_hit': ['SH601899'],
}


def test_report_has_no_standalone_hot_section():
    articles = [_article('1', source='hot', hot=HOT_META, url='https://xueqiu.com/9/1')]
    text = an.generate_daily_report(
        articles, [_result()], hot_stats={'hot_total': 1, 'merged': 0, 'hot_only': 1}
    )
    assert '🔥 全网热贴 Top 10' not in text
    assert '## 🔥 全网热贴' not in text


def test_hot_badge_in_brief_format():
    articles = [_article('1', source='hot', hot=HOT_META, url='https://xueqiu.com/9/1')]
    text = an.generate_daily_report(
        articles, [_result()], hot_stats={'hot_total': 1, 'merged': 0, 'hot_only': 1}
    )
    assert '   - 🔥 热榜第3 · 👍99 💬214 · 64.2分 · 今晨 · ⭐SH601899' in text
    assert '1. [标题1](https://xueqiu.com/9/1)（作者1）' in text


def test_hot_badge_in_full_format():
    hot = dict(HOT_META, is_overnight=False, symbols_hit=[])
    articles = [_article('1', source='hot', hot=hot)]
    text = an.generate_daily_report(
        articles, [_result('must_read')], hot_stats={'hot_total': 1}
    )
    assert '- **热度**：🔥 热榜第3 · 👍99 💬214 · 64.2分' in text


def test_overview_counts_include_hot():
    articles = [
        _article('1'),
        _article('2', source='both', hot=HOT_META),
        _article('3', source='hot', hot=HOT_META),
    ]
    text = an.generate_daily_report(
        articles,
        [_result(), _result(), _result()],
        hot_stats={'hot_total': 2, 'merged': 1, 'hot_only': 1},
    )
    assert '- **今日新增**：2 篇（自选账号）' in text
    assert '**热门贴融合**：2 条（其中 1 条与自有文章重合' in text
    assert '- **分析条目**：3 篇' in text


def test_overview_unchanged_without_hot():
    text = an.generate_daily_report([_article('1')], [_result()])
    assert '- **今日新增**：1 篇' in text
    assert '热门贴融合' not in text
    assert '分析条目' not in text


def test_author_dynamics_marks_hot_authors():
    articles = [
        _article('1', author='自选作者'),
        _article('2', author='热榜作者', source='hot', hot=HOT_META),
    ]
    text = an.generate_daily_report(
        articles, [_result(), _result()], hot_stats={'hot_total': 1}
    )
    assert '- **热榜作者** 🔥（1篇）' in text
    assert '- **自选作者**（1篇）' in text


# ── 质量门 ──────────────────────────────────────────────────────────────


def test_quality_gate_accepts_hot_excerpt():
    article = _article('1', content='摘要' * 75, source='hot')  # 150 字
    passed, _ = an.check_article_quality(article)
    assert passed is True


def test_quality_gate_rejects_short_account_article():
    article = _article('1', content='摘要' * 75)  # 150 字，阈值仍是 200
    passed, issues = an.check_article_quality(article)
    assert passed is False
    assert any('正文' in i for i in issues)


def test_quality_gate_rejects_empty_hot():
    article = _article('1', content='太短了' * 5, source='hot')  # 15 字
    passed, _ = an.check_article_quality(article)
    assert passed is False


def test_quality_gate_does_not_relax_both():
    article = _article('1', content='摘要' * 75, source='both')
    passed, _ = an.check_article_quality(article)
    assert passed is False


def test_prompt_warns_about_excerpt_only():
    analyzer = an.ArticleAnalyzer.__new__(an.ArticleAnalyzer)  # 跳过 __init__
    analyzer.max_content_chars = 8000
    excerpt_prompt = analyzer._build_prompt(_article('1', content_source='excerpt'))
    full_prompt = analyzer._build_prompt(_article('1', content_source='full'))
    assert '页面摘要' in excerpt_prompt
    assert '页面摘要' not in full_prompt


# ── 代码评审发现的边界 ──────────────────────────────────────────────────


def test_enrich_failure_when_enabled_does_not_break_report(
    tmp_path, fake_analyzer, monkeypatch
):
    """兜底开关打开、但补抓炸了 → 只降级为纯自选账号日报，不能零交付。"""
    monkeypatch.setattr(
        gr.yaml, 'safe_load', lambda _text: {'hot_posts': {'fetch_fulltext': True}}
    )

    def _boom(*args, **kwargs):
        raise RuntimeError('opencli crashed')

    monkeypatch.setattr(gr, 'enrich_items_with_fulltext', _boom)
    data_dir = _data_dir(tmp_path)
    path = _write_article(data_dir, '111', '123', '自有账号文章')
    _write_index(data_dir, [('111', '123', '自有账号文章', path)])
    _write_hot(data_dir, [_hot_item('999', 1)])

    report, out = _run(data_dir)

    assert out.exists(), '补抓异常也不能让当天日报消失'
    assert '价值投资日报' in report


def test_overview_accounts_for_skipped_hot_items():
    articles = [_article('1')]
    text = an.generate_daily_report(
        articles,
        [_result()],
        hot_stats={'hot_total': 3, 'merged': 0, 'hot_only': 1, 'skipped': 2},
    )
    assert '**热门贴融合**：1 条' in text
    assert '**热榜未纳入**：2 条' in text
