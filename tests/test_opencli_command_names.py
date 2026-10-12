"""插件命令名 ↔ 消费者常量的一致性守卫。

`scripts/analyze_opencli_usage.py` 的 `SITE_OPERATIONS` 是用一批**操作名**做
「站点请求 vs 本地命令」的口径划分：风控归因、`peak.mjs` 的「限速器被绕过」判定都靠它
（分不清会把结论带偏一个量级，见该文件里的注释）。

而插件里 `cli({site, name})` 一改名，这个集合就**静默对不上**：调用照样成功，只是被算进
「本地命令」那一桶，于是站点请求量被低估、峰值判定失真。

此前只有一条测试钉住 `SITE_OPERATIONS` 自身的内容（`test_analyze_opencli_usage.py`），
**没有一处校验它与插件里的真实命令名一致**——这个文件补上那一步。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from analyze_opencli_usage import SITE_OPERATIONS  # noqa: E402

from plugin_source import resolve_plugin_dir  # noqa: E402

PLUGIN_DIR = resolve_plugin_dir()


def plugin_commands() -> dict[str, str]:
    """插件提供的命令：`name` → 文件名。

    只取 `cli({...})` 块里**顶层**的 `name`（`args` 数组里也有 `name:`，别抓错）。
    """
    found: dict[str, str] = {}
    for path in sorted(PLUGIN_DIR.glob("*.js")):
        src = path.read_text(encoding="utf-8")
        head = src.find("cli({")
        if head < 0:
            continue  # 非命令模块（rate-limit.js / utils.js / lib/*）
        body = src[head:]
        args_at = body.find("args:")
        block = body[: args_at if args_at > 0 else 400]
        name = re.search(r"\bname:\s*'([^']+)'", block)
        if name:
            found[name.group(1)] = path.name
    return found


def test_every_plugin_command_is_a_known_site_operation():
    """插件提供的每条命令都必须在 `SITE_OPERATIONS` 里。

    反方向**不**断言：`SITE_OPERATIONS` 还包含基线命令（`stock`、`browser:open`），
    它们不是这个插件提供的。
    """
    commands = plugin_commands()
    assert commands, f"{PLUGIN_DIR} 里没抽到任何命令 —— 结构变了，请同步这个测试"
    missing = {
        name: where for name, where in commands.items() if name not in SITE_OPERATIONS
    }
    assert not missing, (
        "插件提供了这些命令，但 analyze_opencli_usage 的 SITE_OPERATIONS 不认识它们 —— "
        f"改名后站点请求会被静默算进「本地命令」桶：{missing}\n"
        f"（当前 SITE_OPERATIONS = {sorted(SITE_OPERATIONS)}）"
    )


def test_plugin_command_extraction_actually_sees_all_seven():
    """抽命令的解析本身要有牙：少抽到一条就等于这条守卫失效。"""
    commands = plugin_commands()
    assert len(commands) == 7, (
        f"应该抽到 7 条命令，实际 {len(commands)} 条：{sorted(commands)} —— "
        "解析逻辑或插件结构变了"
    )
    assert "article" in commands and commands["article"] == "article.js"
