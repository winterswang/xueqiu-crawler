#!/usr/bin/env python3
"""把 xueqiu_analyzer.waf 的内容模式同步进 opencli 适配器的 BLOCK_GUARD。

为什么需要「生成」而不是「引用」
--------------------------------
opencli 适配器跑在 Node 里，没法 import Python 的模式表；而插件目录必须自包含
（它是 ``~/.opencli/plugins/`` 下的一个平铺目录）。所以 JS 侧只能携带一份
**由 Python 生成**的正则，两边漂移由本脚本 ``--check`` 卡住（也接进了 pytest：
``tests/test_waf_patterns_sync.py``）。

只改 ``return /.../.test(t)`` 里那段模式，行的其余部分逐字节保留 ——
避免手写整行带来的意外改动。

用法:
    python3 scripts/sync_waf_patterns.py          # 就地重写
    python3 scripts/sync_waf_patterns.py --check  # 只检查；漂移则退出码 1
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from waf_bridge import CONTENT_PATTERNS  # noqa: E402

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / 'opencli-adapters'
ADAPTER_FILES = ('news.js', 'replies.js', 'user-articles.js', 'article.js')

# // 定界符在模式里被 block_guard_js() 拒绝，所以 [^/]* 安全
_GUARD_RE = re.compile(r'(return /)([^/]*)(/\.test\(t\))')


def expected_patterns() -> str:
    """opencli 适配器里应该出现的正则交替式。"""
    from waf_bridge import block_guard_js

    return block_guard_js()


def rewrite_line(line: str, patterns: str) -> str:
    """只替换 BLOCK_GUARD 里的模式段，其余原样返回。"""
    return _GUARD_RE.sub(lambda m: m.group(1) + patterns + m.group(3), line, count=1)


def sync_file(path: Path, patterns: str) -> bool:
    """就地同步一个适配器文件。返回是否发生了改动。"""
    original = path.read_text(encoding='utf-8')
    out = []
    touched = False
    for line in original.splitlines(keepends=True):
        if 'const BLOCK_GUARD' in line:
            new = rewrite_line(line, patterns)
            if new != line:
                touched = True
            out.append(new)
        else:
            out.append(line)
    if touched:
        path.write_text(''.join(out), encoding='utf-8')
    return touched


def check_file(path: Path, patterns: str) -> list[str]:
    """返回漂移说明；一致则返回空列表。"""
    problems = []
    for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if 'const BLOCK_GUARD' not in line:
            continue
        if rewrite_line(line, patterns) != line:
            problems.append(
                f'{path.name}:{lineno} BLOCK_GUARD 与 waf.CONTENT_PATTERNS 不一致'
            )
    return problems


def main(argv: list[str]) -> int:
    patterns = expected_patterns()
    check_only = '--check' in argv

    if check_only:
        problems = []
        for name in ADAPTER_FILES:
            path = ADAPTERS_DIR / name
            if not path.exists():
                problems.append(f'{name} 不存在')
                continue
            problems.extend(check_file(path, patterns))
        if problems:
            print('检测到漂移 —— 运行 python3 scripts/sync_waf_patterns.py 修复:')
            for p in problems:
                print('  ' + p)
            return 1
        print(
            'BLOCK_GUARD 与 waf.CONTENT_PATTERNS 一致（%d 个模式，%d 个文件）'
            % (len(CONTENT_PATTERNS), len(ADAPTER_FILES))
        )
        return 0

    changed = []
    for name in ADAPTER_FILES:
        path = ADAPTERS_DIR / name
        if not path.exists():
            print(f'跳过（不存在）: {name}')
            continue
        if sync_file(path, patterns):
            changed.append(name)
    if changed:
        print('已更新: ' + ', '.join(changed))
    else:
        print('无需改动，已是最新')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
