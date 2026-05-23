#!/usr/bin/env python3
"""单元测试：核心函数"""

import sys
import json
from pathlib import Path

# 添加项目根目录和 scripts 目录到 path
_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / 'scripts'))

from analyzer import (
    classify_stock_market,
    group_stocks_by_market,
    check_article_quality,
    calculate_priority_score,
    classify_priority,
    _format_article,
    _format_article_brief,
)


# ============================================================
# classify_stock_market
# ============================================================

def test_classify_hk_stocks():
    """港股格式识别"""
    assert classify_stock_market("00883.HK") == "港股"
    assert classify_stock_market("09988.HK") == "港股"
    assert classify_stock_market("3690.HK") == "港股"
    assert classify_stock_market("港股 中海油") == "港股"


def test_classify_us_stocks():
    """美股格式识别（白名单 + 宽泛检测）"""
    # 白名单
    assert classify_stock_market("AAPL") == "美股"
    assert classify_stock_market("TSLA") == "美股"
    assert classify_stock_market("HOOD") == "美股"
    assert classify_stock_market("BMBM") == "美股"
    # 宽泛检测（纯大写，2-5字符）
    assert classify_stock_market("MSFT") == "美股"


def test_classify_a_stocks():
    """A股格式识别"""
    assert classify_stock_market("600519") == "A股"
    assert classify_stock_market("000858") == "A股"
    assert classify_stock_market("300750") == "A股"
    assert classify_stock_market("688981") == "A股"


def test_classify_jp_stocks():
    """日股格式识别"""
    assert classify_stock_market("9984.T") == "日股"
    assert classify_stock_market("日股 丰田") == "日股"


def test_classify_unknown():
    """无法识别的返回 其他"""
    assert classify_stock_market("") == "其他"
    assert classify_stock_market("abc") == "其他"


# ============================================================
# group_stocks_by_market
# ============================================================

def test_group_stocks_by_market():
    """按市场分组"""
    stocks = ["AAPL", "00883.HK", "600519", "TSLA", "09988.HK"]
    groups = group_stocks_by_market(stocks)
    assert "美股" in groups
    assert "港股" in groups
    assert "A股" in groups
    assert len(groups["美股"]) == 2
    assert len(groups["港股"]) == 2
    assert len(groups["A股"]) == 1


def test_group_stocks_empty():
    """空列表"""
    assert group_stocks_by_market([]) == {}


# ============================================================
# check_article_quality
# ============================================================

def test_quality_passed():
    """完整文章通过检测"""
    article = {
        "title": "中海油2024年深度估值分析",
        "content": "这是一篇很长的文章..." * 50,  # > 200 字符
        "author": "czy710",
        "publish_time": "2026-03-11 10:30",
    }
    passed, issues = check_article_quality(article)
    assert passed
    assert len(issues) == 0


def test_quality_empty_title():
    """标题为空 → 不通过"""
    article = {
        "title": "",
        "content": "x" * 300,
        "author": "czy710",
        "publish_time": "2026-03-11",
    }
    passed, issues = check_article_quality(article)
    assert not passed
    assert any("标题" in i for i in issues)


def test_quality_short_content():
    """内容过短（<200字符）→ 不通过"""
    article = {
        "title": "测试文章",
        "content": "短内容",
        "author": "czy710",
        "publish_time": "2026-03-11",
    }
    passed, issues = check_article_quality(article)
    assert not passed
    assert any("正文" in i for i in issues)


def test_quality_missing_author():
    """缺少作者 → warning 但可通过"""
    article = {
        "title": "合格标题",
        "content": "x" * 300,
        "author": "",
        "publish_time": "2026-03-11",
    }
    passed, issues = check_article_quality(article)
    assert passed  # 作者为空不是关键检测
    assert any("作者" in i for i in issues)


def test_quality_missing_publish_time():
    """缺少发布时间 → warning 但可通过"""
    article = {
        "title": "合格标题",
        "content": "x" * 300,
        "author": "czy710",
        "publish_time": "",
    }
    passed, issues = check_article_quality(article)
    assert passed
    assert any("发布时间" in i for i in issues)


# ============================================================
# calculate_priority_score
# ============================================================

def test_calculate_priority_score_deep_article():
    """高质量长文 → 高分"""
    article = {
        "title": "深度分析：中海油估值与护城河",
        "content": "估值 PE PB ROE 自由现金流 护城河 安全边际 商业模式" * 50,  # ~5000 char with keywords
    }
    scores = calculate_priority_score(article)
    assert scores["total"] >= 30
    assert scores["content_depth"] > 0
    assert scores["keywords"] > 0


def test_calculate_priority_score_short():
    """短文 → 低分"""
    article = {
        "title": "简短笔记",
        "content": "今日操作记录",
    }
    scores = calculate_priority_score(article)
    assert scores["total"] < 30


# ============================================================
# classify_priority
# ============================================================

def test_classify_priority_must_read():
    """高分文章 → 必读"""
    article = {
        "title": "深度分析：中海油估值与护城河",
        "content": "估值 PE PB ROE 自由现金流 护城河 安全边际 商业模式 财报 年报 业绩 内在价值 毛利率 净利率 管理层 资本配置" * 150,  # >5000 chars + rich keywords
    }
    priority = classify_priority(article)
    assert priority == "must_read", f"got {priority}"


def test_classify_priority_reference():
    """低分文章 → 参考"""
    article = {
        "title": "短笔记",
        "content": "今天",
    }
    priority = classify_priority(article)
    assert priority == "reference"


# ============================================================
# _format_article / _format_article_brief
# ============================================================

def test_format_article():
    """文章格式化不抛异常"""
    article = {
        "title": "测试标题",
        "author": "测试作者",
        "article_id": "123456",
        "user_id": "5739488179",
        "content": "测试正文",
    }
    result = {
        "quality_passed": True,
        "priority": "must_read",
        "scores": {"total": 80, "content_depth": 30, "keywords": 25, "category": 10, "core_points": 10, "title_quality": 5},
        "analysis": {
            "category": "公司研究",
            "related_stocks": ["AAPL", "TSLA"],
            "core_points": ["观点1", "观点2"],
            "summary": "一句话总结",
            "deep_analysis": {
                "business_quality": "好的商业模式",
                "management": "好的管理层",
                "key_risks": "关键风险",
                "competitive_position": "竞争优势",
                "outlook": "后续关注",
            },
        },
    }
    lines = _format_article(1, article, result)
    assert any("测试标题" in l for l in lines)
    assert any("AAPL" in l for l in lines)
    assert any("TSLA" in l for l in lines)


def test_format_article_brief():
    """简要格式化不抛异常"""
    article = {
        "title": "测试标题",
        "author": "测试作者",
        "article_id": "123456",
        "user_id": "5739488179",
    }
    result = {"quality_passed": True, "issues": []}
    lines = _format_article_brief(1, article, result)
    assert any("测试标题" in l for l in lines)


if __name__ == "__main__":
    # 简单测试运行器
    tests = [
        test_classify_hk_stocks,
        test_classify_us_stocks,
        test_classify_a_stocks,
        test_classify_jp_stocks,
        test_classify_unknown,
        test_group_stocks_by_market,
        test_group_stocks_empty,
        test_quality_passed,
        test_quality_empty_title,
        test_quality_short_content,
        test_quality_missing_author,
        test_quality_missing_publish_time,
        test_calculate_priority_score_deep_article,
        test_calculate_priority_score_short,
        test_classify_priority_must_read,
        test_classify_priority_reference,
        test_format_article,
        test_format_article_brief,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"结果: {passed}/{passed+failed} 通过")
    if failed:
        print(f"失败: {failed}")
