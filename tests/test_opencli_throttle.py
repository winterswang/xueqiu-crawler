from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import opencli_extractor

# 插件层限速器（opencli-adapters/rate-limit.js，站点 xueqiu）单次可能的最长等待：
# max_delay_s(12) + long_pause_max_s(75) = 87 秒。它的等待发生在 **opencli
# 子进程内部**，会被 subprocess.run 的墙钟一并计时 —— 所以调用方的 timeout 必须
# 大于「这个等待 + 单次真实执行」，否则长停一命中就必然被误杀。
#
# 这两个数是**照抄**插件层的内置策略，不是读出来的：插件层的 long_pause/max_delay
# 改大时，下面的断言不会自动跟着失败。所以这两个测试只钉住调用方这一侧（把
# timeout 改小会红），另一侧需要在改 rate-limit.js 时人工同步。
PLUGIN_THROTTLE_MAX_WAIT_SECONDS = 12 + 75
SINGLE_RUN_HEADROOM_SECONDS = 20


def test_user_articles_preflight_does_not_visit_xueqiu(monkeypatch):
    def fail_if_site_call(*_args, **_kwargs):
        raise AssertionError("preflight must not call _run")

    def fake_help(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Usage: opencli xueqiu user-articles",
            stderr="",
        )

    monkeypatch.setattr(opencli_extractor, "_run", fail_if_site_call)
    monkeypatch.setattr(opencli_extractor.subprocess, "run", fake_help)

    assert opencli_extractor.is_user_articles_available() is True


def test_opencli_run_uses_shared_rate_limiter(monkeypatch):
    slots = []

    def fake_slot(_source, _args):
        slot = {"enabled": True, "waited_seconds": 0.0, "reserved_seconds": 2.0}
        slots.append(slot)
        return slot

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )

    monkeypatch.setattr(opencli_extractor, "acquire_opencli_slot", fake_slot)
    monkeypatch.setattr(opencli_extractor.subprocess, "run", fake_run)
    monkeypatch.setattr(
        opencli_extractor,
        "record_opencli_call",
        lambda *args, **kwargs: None,
    )

    result = opencli_extractor._run("xueqiu", "whoami")

    assert result.returncode == 0
    assert len(slots) == 1


def test_list_call_timeout_covers_plugin_rate_limit_pause(monkeypatch):
    """列表调用的超时必须盖住插件层限速器的长停。

    回归（2026-10-12）：插件层每 30 次调用一次 45–75 秒长停，而列表调用的
    timeout 只有 30 秒 —— 等待发生在子进程里，于是长停每命中一次就把列表调用
    掐死在 30.0s、该账号记失败（当天 2/18 失败；账号 4111857140 因此漏掉一篇
    真文）。这条断言就是当时缺的防线：把调用方的 timeout 改小（例如回退成
    30/60），它会先失败。插件层那一侧读不到，见上方常量注释。
    """
    captured = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )

    monkeypatch.setattr(opencli_extractor, "_run", fake_run)

    opencli_extractor.get_user_articles("1", 20)

    assert captured["timeout"] > (
        PLUGIN_THROTTLE_MAX_WAIT_SECONDS + SINGLE_RUN_HEADROOM_SECONDS
    )


def test_detail_call_timeout_covers_plugin_rate_limit_pause(monkeypatch):
    """详情页调用的超时同样要盖住插件层长停。

    详情页当前还没被插件层限速（策略只声明了 xueqiu，而这条命令的站点键是
    web），所以现在撞不上；但一旦补上 web 的覆盖，它就会拿到同样的 45–75 秒
    长停 —— 提前钉住，避免修一个坑掉进另一个。
    """
    captured = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "url": "https://xueqiu.com/1/1",
                        "title": "某个标题",
                        "content": "正文内容",
                        "selector": "article",
                        "chars": 4,
                    }
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(opencli_extractor, "is_article_available", lambda: True)
    monkeypatch.setattr(opencli_extractor, "_run", fake_run)

    opencli_extractor.get_article_content("https://xueqiu.com/1/1")

    assert captured["timeout"] > (
        PLUGIN_THROTTLE_MAX_WAIT_SECONDS + SINGLE_RUN_HEADROOM_SECONDS
    )
