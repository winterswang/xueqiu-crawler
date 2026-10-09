"""publish_daily_report 的幂等判断必须基于**内容**，不能只看日期。

2026-10-05 实测踩到：08:07 首次爬取被风控拦住 → 产出并发布了 0 篇的空日报
（IMA 笔记 7512664842445628）；10:10 过了验证页重跑 → 日报有了真内容。
旧逻辑是「当天发过就复用」，于是重跑会打印那篇空笔记的 URL 并 `return 0`
—— 看起来完全成功，实际交付的是一篇空日报。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fcntl

from publish_daily_report import (
    _acquire_publish_lock,
    _content_digest,
    _should_reuse_note,
)


def test_same_day_same_content_reuses_note():
    digest = _content_digest("日报正文")
    state = {
        "date": "2026-10-05",
        "note_id": "7512664842445628",
        "content_digest": digest,
    }
    assert _should_reuse_note(state, "2026-10-05", digest) is True


def test_same_day_changed_content_republishes():
    """核心回归：同一天日报被重写后必须重新发布。"""
    state = {
        "date": "2026-10-05",
        "note_id": "7512664842445628",
        "content_digest": _content_digest("今日无必读文章（0 篇）"),
    }
    new_digest = _content_digest("🔴 必读 9 篇 / 🟡 值得关注 14 篇")
    assert _should_reuse_note(state, "2026-10-05", new_digest) is False


def test_previous_day_never_reuses():
    digest = _content_digest("日报正文")
    state = {"date": "2026-10-04", "note_id": "x", "content_digest": digest}
    assert _should_reuse_note(state, "2026-10-05", digest) is False


def test_legacy_state_without_digest_republishes():
    """旧状态文件没有 content_digest —— 视为不一致，重新发布（安全方向）。"""
    state = {"date": "2026-10-05", "note_id": "7512664842445628"}
    assert _should_reuse_note(state, "2026-10-05", _content_digest("x")) is False


def test_missing_note_id_never_reuses():
    digest = _content_digest("日报正文")
    assert (
        _should_reuse_note(
            {"date": "2026-10-05", "content_digest": digest}, "2026-10-05", digest
        )
        is False
    )


def test_digest_is_stable_and_content_sensitive():
    assert _content_digest("a") == _content_digest("a")
    assert _content_digest("a") != _content_digest("b")


def test_digest_ignores_generation_timestamp_line():
    """核心回归:尾注时间戳每次运行必变,digest 不能被它牵着走。

    2026-10-08 事故根因:digest 含时间戳 → 任何重跑必然变化 → 必然重发新笔记。
    """
    morning = "正文\n\n---\n\n*报告生成时间：2026-10-08 08:00:00*\n"
    evening = "正文\n\n---\n\n*报告生成时间：2026-10-08 19:45:12*\n"
    assert _content_digest(morning) == _content_digest(evening)


def test_publish_lock_is_exclusive(tmp_path):
    fd1 = _acquire_publish_lock(tmp_path)
    assert fd1 is not None
    try:
        assert _acquire_publish_lock(tmp_path) is None
    finally:
        fcntl.flock(fd1, fcntl.LOCK_UN)
        fd1.close()
    fd2 = _acquire_publish_lock(tmp_path)
    assert fd2 is not None
    fd2.close()
