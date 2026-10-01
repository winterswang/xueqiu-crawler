#!/usr/bin/env python3
"""风控模式同源回归测试。

opencli 适配器跑在 Node 里，拿不到 Python 的模式表，只能携带一份**生成**的
正则（scripts/sync_waf_patterns.py）。这些测试卡住两边不能各走各的 ——
漂移了的后果是同一个页面在适配器里判成「被拦截」、在 Python 里判成「正常」。
"""

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / 'scripts'))

from sync_waf_patterns import ADAPTERS_DIR, ADAPTER_FILES, check_file, expected_patterns


def test_block_guard_matches_waf_patterns():
    """适配器里的 BLOCK_GUARD 必须与 xueqiu_analyzer.waf.CONTENT_PATTERNS 同源。"""
    patterns = expected_patterns()
    problems = []
    for name in ADAPTER_FILES:
        path = ADAPTERS_DIR / name
        assert path.exists(), f'opencli 适配器缺失: {name}'
        problems.extend(check_file(path, patterns))
    assert not problems, (
        'BLOCK_GUARD 已漂移，运行 python3 scripts/sync_waf_patterns.py 修复:\n'
        + '\n'.join(problems)
    )


def test_block_guard_covers_every_pattern():
    """每个模式都要真的出现在适配器文件里（防止生成逻辑漏项）。"""
    patterns = expected_patterns().split('|')
    for name in ADAPTER_FILES:
        text = (ADAPTERS_DIR / name).read_text(encoding='utf-8')
        for p in patterns:
            assert p in text, f'{name} 缺少模式 {p!r}'


def test_waf_bridge_reaches_analyzer():
    """crawler 必须能拿到 analyzer 的风控判定实现（拿不到会 SystemExit）。"""
    import waf_bridge

    assert waf_bridge.is_error_page('405') is True
    assert waf_bridge.contains_waf_text('滑动验证') is True
    assert waf_bridge.has_waf_marker('x aliyun_waf y') is True
    assert waf_bridge.classify_failure('滑动验证') == '风控验证页'


def test_opencli_extractor_no_longer_flags_405_in_body():
    """回归：opencli_extractor 旧实现把 '405' 当正文子串，误判正常文章并重试。"""
    from opencli_extractor import _is_error_page

    assert _is_error_page('滑动验证') is False          # 太短 → 不判
    assert _is_error_page('滑动验证' + '填充' * 100) is True
    assert _is_error_page('贵州茅台 405 亿元营收' + '正文' * 200) is False


def test_generate_report_filter_uses_unified_judgement():
    """回归：generate_report 的过滤逻辑与 crawler_nodriver 不能再各写一套。"""
    import crawler_nodriver
    from waf_bridge import is_error_page

    # crawler_nodriver 现在直接别名到唯一实现
    assert crawler_nodriver._is_content_error is is_error_page
