from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import opencli_extractor

# 插件层限速器（opencli-adapters/rate-limit.js）的「单次可能的最长等待」是
# max_delay_s + long_pause_max_s —— 它的等待发生在 **opencli 子进程内部**，会被
# subprocess.run 的墙钟一并计时，所以调用方的 timeout 必须大于「这个等待 + 单次
# 真实执行」，否则长停一命中就必然被误杀（2026-10-12 实测：30s 上限下 2/18 账号失败）。
#
# 这些数**从插件源码里读**，不照抄 —— 这样插件层把 long_pause/max_delay 调大时，
# 下面的断言会一起失败（双向守卫），而不是只有「timeout 被改小」才会红。
PLUGIN_JS = (
    Path(__file__).resolve().parent.parent / "opencli-adapters" / "rate-limit.js"
)
SINGLE_RUN_HEADROOM_SECONDS = 20


def _plugin_policy_js(site: str) -> str:
    """取出 BUILTIN_SITES 里某个站点的策略源码片段（内联对象或它引用的常量）."""
    src = PLUGIN_JS.read_text(encoding="utf-8")
    table = re.search(r"const BUILTIN_SITES = \{(.*?)\n\};", src, re.S)
    assert table, "找不到 BUILTIN_SITES —— rate-limit.js 结构变了，请同步这个测试"
    entry = re.search(
        rf"^\s*{re.escape(site)}\s*:\s*(\{{.*?\}}|[A-Za-z_$][\w$]*)\s*,?\s*$",
        table.group(1),
        re.M | re.S,
    )
    assert entry, f"BUILTIN_SITES 没有声明 `{site}` —— 该站点的调用将完全不限速"
    value = entry.group(1)
    if value.startswith("{"):
        return value
    const = re.search(rf"const {re.escape(value)} = \{{(.*?)\n\}};", src, re.S)
    assert const, f"`{value}` 的定义找不到，无法解析 {site} 的策略"
    return const.group(1)


def _plugin_number(site: str, key: str) -> float:
    policy = _plugin_policy_js(site)
    found = re.search(rf"\b{re.escape(key)}\s*:\s*([0-9.]+)", policy)
    assert found, f"{site} 的策略里没有 `{key}`：{policy[:80]}"
    return float(found.group(1))


def _plugin_worst_wait_seconds(site: str) -> float:
    """单次调用在插件层可能被等待的最长时间（只算预留，不含取锁轮询）."""
    return _plugin_number(site, "max_delay_s") + _plugin_number(
        site, "long_pause_max_s"
    )


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
    真文）。两侧都钉住：调用方的 timeout 改小会红，插件层的 long_pause/max_delay
    改大也会红。
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
        _plugin_worst_wait_seconds("xueqiu") + SINGLE_RUN_HEADROOM_SECONDS
    )


def test_builtin_sites_declare_web_so_detail_pages_are_throttled():
    """内置表必须声明 `web`，且它的策略要带长停。

    回归（2026-10-12）：雪球生产链路的**详情页**走的是通用命令
    `opencli web article`（站点键 `web`）。内置表原先只声明了
    xiaohongshu / xueqiu，`policyFor('web')` 返回 null —— 详情页**连基础的
    6–12 秒间隔都没有**，而它正是被风控墙拦掉的那一类请求
    （台账 625 次 `web article` 全部指向 xueqiu.com）。
    Python 版原本专门覆盖了这条路径，限速上移到插件层时漏了。
    """
    policy = _plugin_policy_js("web")

    assert "long_pause_max_s" in policy, f"web 的策略没有长停：{policy[:80]}"
    assert _plugin_number("web", "max_delay_s") > 0, "web 的策略没有正的基础间隔"


def test_detail_call_timeout_covers_plugin_rate_limit_pause(monkeypatch):
    """详情页调用的超时同样要盖住插件层长停。

    详情页走的是 `web article`（站点键 `web`）—— `web` 策略一旦补上（已补），
    它就会和列表调用一样吃到 45–75 秒长停，所以超时上限必须同样够。
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
        _plugin_worst_wait_seconds("web") + SINGLE_RUN_HEADROOM_SECONDS
    )
