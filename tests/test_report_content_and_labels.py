"""日报生成：正文长度的口径 + 报告头标签.

2026-10-09 发现：`generate_report.py` 把**整份 .md** 当 `content` 喂给质量检测，
而 .md 里有标题/作者/原文链接/爬取时间那一圈固定头部（约 250-300 字）——
于是 analyzer 的「内容 > 200 字符才走 GLM-5」门槛**恒真**：一篇正文只有 128 字的
纯图片帖（整文件 272 字）照样过闸、白调一次 LLM，报告头还写着「（内容 > 200 字）」。
"""

from __future__ import annotations

import scripts.generate_report as gr
from scripts.analyzer import generate_daily_report

SAMPLE = """# 某个标题

> 作者：某人 | 发布时间：昨天 21:19
> 点赞：0 | 评论：0
> 原文链接：https://xueqiu.com/1/2

---

这里是正文。

---

*爬取时间：2026-10-09 08:07:18*
"""


# ── 正文提取 ────────────────────────────────────────────────────────────


def test_extracts_body_between_the_two_rules():
    assert gr._extract_article_body(SAMPLE) == "这里是正文。"


def test_keeps_inner_rules_intact():
    """正文自身含 `---`（markdown 分隔线）时不能截断 —— 1244 篇真实文章里有 60 篇如此."""
    text = SAMPLE.replace("这里是正文。", "上段。\n\n---\n\n下段。")

    assert gr._extract_article_body(text) == "上段。\n\n---\n\n下段。"


def test_falls_back_to_whole_text_when_no_rules():
    """没有分隔符的畸形文件不能炸，也不该把内容吃掉."""
    assert (
        gr._extract_article_body("就是一段没有分隔符的文字")
        == "就是一段没有分隔符的文字"
    )


def test_empty_input_is_safe():
    assert gr._extract_article_body("") == ""


def test_short_post_is_now_below_the_gate():
    """回归：128 字的图片帖以前整文件 272 字 → 过闸；剥出正文后应被拦."""
    text = SAMPLE.replace("这里是正文。", "x" * 128)

    assert len(gr._extract_article_body(text)) < 200, "剥出正文后必须低于门槛"
    assert len(text) >= 200, "整份文件本来是高过门槛的（这正是原来的问题）"


def test_long_post_still_passes_the_gate():
    text = SAMPLE.replace("这里是正文。", "正文" * 300)

    assert len(gr._extract_article_body(text)) > 200


def test_get_today_articles_returns_the_body_not_the_whole_file(tmp_path):
    """接线回归：`get_today_articles` 交出去的必须是**正文**.

    只测 `_extract_article_body` 本身挡不住「调用方忘了用它」—— 今天已经在别处
    吃过同一个亏（假对象越过了真正出问题的那一层）。
    """
    import datetime
    import json

    data = tmp_path / "data"
    user = data / "9"
    user.mkdir(parents=True)
    md = user / "2.md"
    md.write_text(SAMPLE.replace("这里是正文。", "x" * 128), encoding="utf-8")

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    (data / "index.json").write_text(
        json.dumps(
            {
                "articles": {
                    "9_2": {
                        "article_id": "2",
                        "user_id": "9",
                        # title 必须给：索引缺 title 时 is_error_page('') 会把这篇
                        # 直接当成错误页过滤掉（空标题命中风控页判定）
                        "title": "某个标题",
                        "author": "某人",
                        "crawl_time": f"{today}T08:00:00",
                        "filepath": str(md),
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    articles = gr.get_today_articles(str(data))

    assert len(articles) == 1
    assert len(articles[0]["content"]) < 200, "交出去的必须是正文，不是整份 .md"


# ── 报告头标签 ──────────────────────────────────────────────────────────


def _render(tmp_path, articles, results) -> str:
    return generate_daily_report(articles, results, str(tmp_path / "r.md"))


def test_header_labels_match_the_real_rule(tmp_path):
    """标签不该再写「内容 > 200 字」—— 实际计的是 quality_passed."""
    articles = [{"title": "T", "author": "A", "article_id": "1", "user_id": "9"}]
    results = [{"quality_passed": True, "priority": "reference"}]

    report = _render(tmp_path, articles, results)

    assert "**有效分析**：1 篇（标题与正文通过质量检测）" in report
    assert "内容 > 200 字" not in report
    assert "**无效文章**：0 篇（标题或正文过短）" in report


def test_header_counts_failed_articles(tmp_path):
    articles = [
        {"title": "T1", "author": "A", "article_id": "1", "user_id": "9"},
        {"title": "T2", "author": "A", "article_id": "2", "user_id": "9"},
    ]
    results = [
        {"quality_passed": True, "priority": "reference"},
        {
            "quality_passed": False,
            "priority": "reference",
            "issues": ["正文为空或过短"],
        },
    ]

    report = _render(tmp_path, articles, results)

    assert "**有效分析**：1 篇" in report
    assert "**无效文章**：1 篇（标题或正文过短）" in report
