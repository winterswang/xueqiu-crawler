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


def test_adapter_waf_is_not_retried(monkeypatch):
    """适配器的 BLOCKED_WAF 是**确定性**信号 —— 不能重试.

    回归（2026-10-09 通盘审查）：原来它和「正文看着像错误页」共用一个 continue，
    于是一次被拦变成 3 次导航（每次还带 3 秒 sleep + 6–12 秒限速间隔）——
    对着刚被拦的那个 URL 反复打，正是 2026-10-08 那类风控放大的来源。
    """
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return _completed(
            1, stderr="error:\n  code: BLOCKED_WAF\n  message: 风控验证页"
        )

    monkeypatch.setattr(scripts.opencli_extractor, "is_article_available", lambda: True)
    monkeypatch.setattr(scripts.opencli_extractor, "_run", fake_run)
    _no_sleep(monkeypatch)

    detail = scripts.opencli_extractor.get_article_content(
        "https://xueqiu.com/1/1", max_retries=2
    )

    assert detail["waf_detected"] is True
    assert detail["content"] == ""
    assert len(calls) == 1, "确定性信号不该重试"


def test_browser_fallback_waf_still_retries_then_succeeds(monkeypatch):
    """直连回退路径没有确定信号，只能看正文 —— 保留原有的重试行为."""
    outputs = iter(
        [
            _completed(0, stdout=json.dumps({"page": "p"})),
            _completed(0, stdout="T - 雪球"),
            _completed(0, stdout=json.dumps({"content": "风控页"})),
            _completed(0, stdout=json.dumps({"page": "p"})),
            _completed(0, stdout="T - 雪球"),
            _completed(0, stdout=json.dumps({"content": "Good body"})),
        ]
    )
    monkeypatch.setattr(
        scripts.opencli_extractor, "is_article_available", lambda: False
    )
    monkeypatch.setattr(
        scripts.opencli_extractor, "_run", lambda *a, **kw: next(outputs)
    )
    monkeypatch.setattr(
        scripts.opencli_extractor, "_is_error_page", lambda content: content == "风控页"
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


def test_cooldown_skips_the_legacy_browser_fallback(monkeypatch):
    """熔断冷却期内**不走** legacy 浏览器回退。

    适配器不可用时才会走 `_attempt_via_browser`（`opencli browser …`），而那条通道
    **不经插件钩子、不限速**。站点正因为被打才开的冷却，回退回去等于继续打它 ——
    正是熔断器要停止的事。
    """
    monkeypatch.setattr(
        scripts.opencli_extractor, "is_article_available", lambda: False
    )
    monkeypatch.setattr(
        scripts.opencli_extractor, "in_cooldown", lambda *_a, **_k: True
    )
    called = []
    monkeypatch.setattr(
        scripts.opencli_extractor,
        "_attempt_via_browser",
        lambda *a, **k: called.append(1),
    )

    out = scripts.opencli_extractor.get_article_content("https://xueqiu.com/1/2")

    assert called == [], "冷却期内绝不能走 legacy 浏览器序列"
    assert out["cooldown"] is True, "要能区分「我们停手」与「站点拦了我们」"
    assert out["waf_detected"] is False, "冷却期没发请求，不该计进 blocked_articles"
    assert out["content"] == ""


def test_not_in_cooldown_keeps_the_legacy_fallback(monkeypatch):
    """没冷却时行为不变 —— 这条判断只该在熔断期生效。"""
    monkeypatch.setattr(
        scripts.opencli_extractor, "is_article_available", lambda: False
    )
    monkeypatch.setattr(
        scripts.opencli_extractor, "in_cooldown", lambda *_a, **_k: False
    )
    called = []
    monkeypatch.setattr(
        scripts.opencli_extractor,
        "_attempt_via_browser",
        lambda *a, **k: (
            called.append(1),
            {"title": "t", "content": "x" * 200, "waf_detected": False},
        )[1],
    )

    out = scripts.opencli_extractor.get_article_content("https://xueqiu.com/1/2")

    assert called, "没冷却时该照旧回退"
    assert out["title"] == "t"


def test_zero_second_precheck_runs_before_the_adapter(monkeypatch):
    """0 秒预检必须挡在**适配器调用之前**。

    插件的 `shouldThrottle` 对 `--help` 在冷却检查**之前**早退，所以 `web article --help`
    探测在冷却期照样成功 → `is_article_available()` 为真 → 命令会走适配器分支，
    每次白等约 45 秒 ×(max_retries+1)。回退分支里的守卫拦不到这条主通道。
    """
    monkeypatch.setattr(scripts.opencli_extractor, "is_article_available", lambda: True)
    monkeypatch.setattr(
        scripts.opencli_extractor, "in_cooldown", lambda *_a, **_k: True
    )
    calls = []
    monkeypatch.setattr(
        scripts.opencli_extractor,
        "_attempt_via_adapter",
        lambda *a, **k: calls.append(1),
    )

    out = scripts.opencli_extractor.get_article_content("https://xueqiu.com/1/2")

    assert calls == [], "冷却期不该调用适配器（那次调用注定被拦、还要白等 45 秒）"
    assert out["cooldown"] is True
