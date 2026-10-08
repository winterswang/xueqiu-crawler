"""`opencli_extractor` 正文抓取：适配器主通道 + browser 直连回退。

主通道是 `opencli web article`（opencli-adapters/article.js），回退是在该命令
未注册时走的旧 opencli browser open/get/extract 序列。两条路的重试与风控语义
必须一致，这个文件把它们钉住。
"""

from __future__ import annotations

import json
import subprocess

import scripts.opencli_extractor


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _no_sleep(monkeypatch):
    monkeypatch.setattr(scripts.opencli_extractor.time, "sleep", lambda _seconds: None)


def test_adapter_success_strips_title_suffix_and_returns_content(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return _completed(
            0,
            stdout=json.dumps(
                [
                    {
                        "url": "https://xueqiu.com/1/1",
                        "title": "某个标题\xa0-\xa0雪球",
                        "content": "正文内容",
                        "selector": "article",
                        "chars": 4,
                    }
                ]
            ),
        )

    monkeypatch.setattr(scripts.opencli_extractor, "is_article_available", lambda: True)
    monkeypatch.setattr(scripts.opencli_extractor, "_run", fake_run)
    _no_sleep(monkeypatch)

    detail = scripts.opencli_extractor.get_article_content("https://xueqiu.com/1/1")

    assert detail["title"] == "某个标题"
    assert detail["content"] == "正文内容"
    assert detail["waf_detected"] is False
    # 必须带 -f json：table/md 会按 columns 裁掉 content
    assert calls[0] == ("web", "article", "https://xueqiu.com/1/1", "-f", "json")


def test_adapter_waf_then_success_is_not_marked_as_waf(monkeypatch):
    outputs = iter(
        [
            _completed(1, stderr="error:\n  code: BLOCKED_WAF\n  message: 风控验证页"),
            _completed(
                0,
                stdout=json.dumps(
                    [{"title": "T", "content": "Good body", "selector": "article"}]
                ),
            ),
        ]
    )
    monkeypatch.setattr(scripts.opencli_extractor, "is_article_available", lambda: True)
    monkeypatch.setattr(
        scripts.opencli_extractor, "_run", lambda *a, **kw: next(outputs)
    )
    _no_sleep(monkeypatch)

    detail = scripts.opencli_extractor.get_article_content(
        "https://xueqiu.com/1/1", max_retries=1
    )

    assert detail["content"] == "Good body"
    assert detail["waf_detected"] is False


def test_adapter_waf_exhausting_retries_reports_waf(monkeypatch):
    monkeypatch.setattr(scripts.opencli_extractor, "is_article_available", lambda: True)
    monkeypatch.setattr(
        scripts.opencli_extractor,
        "_run",
        lambda *a, **kw: _completed(1, stderr="  code: BLOCKED_WAF"),
    )
    _no_sleep(monkeypatch)

    detail = scripts.opencli_extractor.get_article_content(
        "https://xueqiu.com/1/1", max_retries=2
    )

    assert detail["waf_detected"] is True
    assert detail["content"] == ""
    assert detail["title"] == ""


def test_adapter_non_waf_failure_still_retries_and_reports_not_waf(monkeypatch):
    """普通失败（非风控）不能标成 waf —— 否则调用方会去重试一个需要人工过滑块的状态。"""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return _completed(1, stderr="error:\n  code: COMMAND_EXEC")

    monkeypatch.setattr(scripts.opencli_extractor, "is_article_available", lambda: True)
    monkeypatch.setattr(scripts.opencli_extractor, "_run", fake_run)
    _no_sleep(monkeypatch)

    detail = scripts.opencli_extractor.get_article_content(
        "https://xueqiu.com/1/1", max_retries=2
    )

    assert detail["waf_detected"] is False
    assert detail["content"] == ""
    assert len(calls) == 3  # max_retries + 1


def test_adapter_timeout_is_retried_not_raised(monkeypatch):
    """适配器超时必须被吃掉并重试，不能冒泡出去.

    `_run` 会原样重抛 TimeoutExpired，而上游 crawl_gateway 只把 SiteAccessError
    认作站点异常 —— 裸异常会打断整个账号的文章循环，而不是降级掉这一篇。
    """
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(cmd=args, timeout=60)

    monkeypatch.setattr(scripts.opencli_extractor, "is_article_available", lambda: True)
    monkeypatch.setattr(scripts.opencli_extractor, "_run", fake_run)
    _no_sleep(monkeypatch)

    detail = scripts.opencli_extractor.get_article_content(
        "https://xueqiu.com/1/1", max_retries=2
    )

    assert detail["waf_detected"] is False
    assert detail["content"] == ""
    assert len(calls) == 3  # max_retries + 1，说明每一轮都被吃掉并重试了


def test_falls_back_to_browser_when_adapter_missing(monkeypatch):
    calls = []
    outputs = iter(
        [
            json.dumps({"page": "abc"}),  # open
            "标题 - 雪球",  # get title
            json.dumps({"content": "Body"}),  # extract
        ]
    )

    def fake_run(*args, **kwargs):
        calls.append(args)
        return _completed(0, stdout=next(outputs))

    monkeypatch.setattr(
        scripts.opencli_extractor, "is_article_available", lambda: False
    )
    monkeypatch.setattr(scripts.opencli_extractor, "_run", fake_run)
    _no_sleep(monkeypatch)

    detail = scripts.opencli_extractor.get_article_content("https://xueqiu.com/1/1")

    assert detail["content"] == "Body"
    assert detail["title"] == "标题"
    assert [c[:2] for c in calls] == [
        ("browser", "xq-crawler"),
        ("browser", "xq-crawler"),
        ("browser", "xq-crawler"),
    ]
    assert [c[2] for c in calls] == ["open", "get", "extract"]


def test_clean_output_does_not_truncate_payload_containing_the_notice_phrase():
    """正文里出现 "Update available" 不能把 JSON 截断.

    回归：旧实现按子串匹配，命中后把**其后所有行**都丢掉；正文是 JSON 里的单独
    一行，于是整个 payload 断在中间 → 解析失败 → 三轮重试 → 整篇静默变成空内容。
    """
    body = "Chrome 提示 Update available 时该怎么办"
    payload = json.dumps([{"title": "T", "content": body, "chars": len(body)}])
    notice = (
        "\n  Update available: v1.8.8 → v1.8.9\n"
        "  Run: npm install -g @jackwener/opencli\n"
    )

    cleaned = scripts.opencli_extractor._clean_output(payload + notice)

    assert json.loads(cleaned) == [{"title": "T", "content": body, "chars": len(body)}]


def test_clean_output_strips_both_notice_forms():
    payload = '{"a":1}'
    cli_notice = "\n  Update available: v1.8.8 → v1.8.9\n  Run: npm i -g x\n"
    ext_notice = (
        "\n  Extension update available: v1.0.1 → v1.0.2\n  Download: https://x\n"
    )

    assert scripts.opencli_extractor._clean_output(payload + cli_notice) == payload
    assert scripts.opencli_extractor._clean_output(payload + ext_notice) == payload
    # 无提示时逐字不动
    assert scripts.opencli_extractor._clean_output(payload) == payload


def test_is_article_available_probes_help_without_site_request(monkeypatch):
    def fail_if_site_call(*_args, **_kwargs):
        raise AssertionError("probe must not call _run")

    monkeypatch.setattr(scripts.opencli_extractor, "_run", fail_if_site_call)
    monkeypatch.setattr(
        scripts.opencli_extractor.subprocess,
        "run",
        lambda *_a, **_k: _completed(0, stdout="Usage: opencli web article [url]"),
    )
    try:
        assert scripts.opencli_extractor.is_article_available() is True
    finally:
        scripts.opencli_extractor.is_article_available.cache_clear()

    monkeypatch.setattr(
        scripts.opencli_extractor.subprocess, "run", lambda *_a, **_k: _completed(1)
    )
    try:
        assert scripts.opencli_extractor.is_article_available() is False
    finally:
        scripts.opencli_extractor.is_article_available.cache_clear()


def test_is_article_available_survives_missing_opencli(monkeypatch):
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("opencli")

    monkeypatch.setattr(scripts.opencli_extractor.subprocess, "run", boom)
    try:
        assert scripts.opencli_extractor.is_article_available() is False
    finally:
        scripts.opencli_extractor.is_article_available.cache_clear()
