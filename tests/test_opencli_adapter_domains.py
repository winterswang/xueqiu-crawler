"""域名解析的**行为**测试：真的把 `lib/domains.js` 跑一遍。

为什么不是「正则读源码」：`tests/test_opencli_throttle.py` 与
`test_opencli_adapter_plugin_integrity.py` 都是静态检查，读得懂「表里写了什么」，
读不懂「解析出来是什么」。2026-10-12 就因此漏掉一个真 bug —— 算完「末两段注册域」
之后没有再过一遍别名表，于是 `api.xueqiu.com` 得到键 `xueqiu.com`：既另开一个状态
文件、策略查找又落回 `web` 的弱档（**丢掉 45–75 秒长停**），正是这套代码注释里
反复强调「绝不能发生」的静默掉档。

`lib/domains.js` 只依赖 `lib/domain-tables.js`（不 import opencli 包），所以**裸 node
就能跑**，不需要 node_modules。CI 的 ubuntu-latest 自带 node；本机没有就跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "opencli-adapters"

_PROBE = """
import { pathToFileURL } from 'node:url';
const mod = await import(pathToFileURL(process.argv[1]).href);
const cases = JSON.parse(process.argv[2]);
const out = cases.map(([site, args]) => {
  const r = mod.resolveDomainKey(site, args);
  return { key: r.key, host: r.host, via: r.via, degraded: r.degraded };
});
process.stdout.write(JSON.stringify(out));
"""

CASES: list[tuple[str, dict, str]] = [
    # --- 别名表：已有键必须原样映射回自己（状态文件不换名） ---
    ("xueqiu", {"user_id": "1"}, "xueqiu"),
    ("web", {"url": "https://xueqiu.com/1/1"}, "xueqiu"),
    ("web", {"url": "https://www.xueqiu.com/1/1"}, "xueqiu"),
    ("web", {"url": "https://stock.xueqiu.com/1/1"}, "xueqiu"),
    ("xiaohongshu", {"keyword": "x"}, "xiaohongshu"),
    # --- ★ 回归：不在别名表里的**子域名**，必须靠「注册域再过一遍别名表」归到 xueqiu ---
    #     漏了这一步就会得到 'xueqiu.com'：新状态文件 + 落回 web 弱档（无长停）。
    ("web", {"url": "https://api.xueqiu.com/1/1"}, "xueqiu"),
    ("web", {"url": "https://a.b.xueqiu.com/1/1"}, "xueqiu"),
    # --- 非雪球域名各拿各的键 ---
    ("web", {"url": "https://www.cninfo.com.cn/a.pdf"}, "cninfo.com.cn"),
    ("web", {"url": "https://www1.hkexnews.hk/a.pdf"}, "hkexnews.hk"),
    # 冒名域名必须拿到**自己**的键（关键是不能被归到 xueqiu、蹭不到那份额度）：
    # 不再做末两段切分后，未知域名就用完整 hostname 当键。
    ("web", {"url": "https://xueqiu.com.evil.com/a"}, "xueqiu.com.evil.com"),
    # --- ★ 回归：多级公共后缀**绝不能**被「末两段」切成 com.cn ---
    #     切了会让同花顺/新浪/东财这些互不相关的站点塞进同一个额度（互相排队），
    #     台账里的 domain_key 也变成 'com.cn' 这种误导性归因。
    ("web", {"url": "https://www.sse.com.cn/a"}, "www.sse.com.cn"),
    ("web", {"url": "https://stock.10jqka.com.cn/x.html"}, "stock.10jqka.com.cn"),
    # --- ★ 回归：尾点（FQDN 形态）必须归一化，否则绕过别名表、静默掉档 ---
    ("web", {"url": "https://xueqiu.com./1/1"}, "xueqiu"),
    # --- IP 字面量不能切末两段（否则会得到 '3.4' 这种垃圾键） ---
    ("web", {"url": "http://93.184.216.34/a"}, "93.184.216.34"),
    # --- 解析不出域名：退回站点键，且 web 要标 degraded 好让调用方告警 ---
    ("web", {}, "web"),
]


def _run_probe(cases: list) -> list[dict]:
    node = shutil.which("node")
    if not node:
        pytest.skip("环境里没有 node，跳过（CI 的 ubuntu-latest 自带）")
    proc = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            _PROBE,
            str(PLUGIN_DIR / "lib" / "domains.js"),
            json.dumps([[s, a] for s, a, _ in cases]),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node 跑失败：{proc.stderr[:400]}"
    return json.loads(proc.stdout)


def test_resolve_domain_key_maps_each_case_to_the_expected_key():
    got = _run_probe(CASES)
    assert len(got) == len(CASES)
    problems = [
        f"  {site} {args} → 期望 {expected!r}，实际 {item!r}"
        for (site, args, expected), item in zip(CASES, got)
        if item["key"] != expected
    ]
    assert not problems, "域名解析结果与预期不符：\n" + "\n".join(problems)


def test_web_calls_without_a_domain_are_flagged_degraded():
    """`web` 找不到目标域名时必须标 degraded —— 否则会静默掉到弱档（无长停）。

    非 `web` 站退回站点键是正常路径（域名固定），不该告警。
    """
    got = _run_probe(
        [
            ("web", {}, "web"),
            ("web", {"url": "not-a-url"}, "web"),
            ("web", {"url": "https://xueqiu.com/1/1"}, "xueqiu"),
            ("xueqiu", {}, "xueqiu"),
        ]
    )
    assert got[0]["degraded"] is True, "web 完全没有 URL 时应标 degraded"
    assert got[1]["degraded"] is True, "web 的 URL 解析不出时应标 degraded"
    assert got[2]["degraded"] is False, "正常解析出的不该标 degraded"
    assert got[3]["degraded"] is False, "非 web 站退回站点键是正常路径，不该告警"
