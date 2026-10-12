"""雪球代码 → 市场前缀的**行为**测试（真的跑 `lib/xueqiu-symbol.js`）。

为什么必须有：这段逻辑原先在 `news.js` 与 `stock-notices.js` 里各写一份嵌套三元，
**两边都错、而且错得一致**，失败方式是静默的 —— 会去抓一个不存在的股票页，
结果只是「列表空」，没有任何报错。2026-10-12 实测：

    830799（北交所 8 开头）→ SZ830799 ✗
    920001（北交所 920 新段）→ SH920001 ✗

原表达式是 `'69'→SH / '03802'→SZ / 其余→BJ`，而 `'03802'` 这个字符串里**含 8**，
于是 8 开头落进了 SZ；`9` 开头又无条件判 SH，把 920 那一段吃掉了。

`lib/xueqiu-symbol.js` 不 import opencli 包，裸 node 就能跑；本机没有 node 就跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from plugin_source import resolve_plugin_dir  # noqa: E402

PLUGIN_DIR = resolve_plugin_dir()

_PROBE = """
import { pathToFileURL } from 'node:url';
const mod = await import(pathToFileURL(process.argv[1]).href);
const cases = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(cases.map((s) => mod.normalizeXueqiuSymbol(s))));
"""

# (输入, 期望输出, 说明)
CASES: list[tuple[str, str, str]] = [
    # --- ★ 两条回归：都是「原实现静默抓错页面」的 ---
    ("830799", "BJ830799", "北交所 8 开头（原实现给了 SZ）"),
    ("920001", "BJ920001", "北交所 920 新段（原实现给了 SH）"),
    # --- 其它北交所段 ---
    ("430047", "BJ430047", "北交所 4 开头"),
    ("870204", "BJ870204", "北交所 8 开头"),
    # --- 沪市 ---
    ("600519", "SH600519", "沪主板"),
    ("688111", "SH688111", "科创板"),
    ("900901", "SH900901", "沪 B 股"),
    ("510300", "SH510300", "沪市 ETF（原实现给了 BJ）"),
    # --- 深市 ---
    ("000001", "SZ000001", "深主板"),
    ("002594", "SZ002594", "深中小"),
    ("300750", "SZ300750", "创业板"),
    ("200011", "SZ200011", "深 B 股"),
    ("159915", "SZ159915", "深市 ETF（原实现给了 BJ）"),
    # --- 非 6 位纯数字：原样返回（港股 / 美股 / 已带前缀） ---
    ("00700", "00700", "港股五位代码"),
    ("AAPL", "AAPL", "美股"),
    ("SH600519", "SH600519", "已带前缀"),
]


def _run(symbols: list[str]) -> list[str]:
    node = shutil.which("node")
    if not node:
        pytest.skip("环境里没有 node，跳过（CI 的 ubuntu-latest 自带）")
    proc = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            _PROBE,
            str(PLUGIN_DIR / "lib" / "xueqiu-symbol.js"),
            json.dumps(symbols),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node 跑失败：{proc.stderr[:400]}"
    return json.loads(proc.stdout)


def test_symbol_prefix_matches_the_exchange_segments():
    got = _run([s for s, _, _ in CASES])
    problems = [
        f"  {symbol}（{label}）→ 期望 {expected}，实际 {actual}"
        for (symbol, expected, label), actual in zip(CASES, got)
        if actual != expected
    ]
    assert not problems, "市场前缀映射与预期不符：\n" + "\n".join(problems)


def test_beijing_exchange_is_never_labelled_sh_or_sz():
    """北交所段（4/8 开头与 920）一律 BJ —— 这条单独列出来是因为它曾经错得最久。"""
    got = _run(["830799", "920001", "430047", "870204"])
    assert all(item.startswith("BJ") for item in got), (
        f"北交所段被贴了别的市场前缀：{got}"
    )
