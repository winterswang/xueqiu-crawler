"""把 analyzer 里的风控判定唯一实现接进 crawler。

背景
----
风控页判定的唯一实现在 ``xueqiu_analyzer.waf``（见该模块文档）。crawler 是纯
脚本目录、不是 pip 包，要靠 ``sys.path`` 注入才能 import 到它。

monitor 早就有同样的做法（其 ``src/crawler.py`` 的 ``_ensure_xueqiu_analyzer_path``），
这里沿用同一套约定：``XUEQIU_ANALYZER_PATH`` 优先，否则用同级 checkout。

为什么导入失败要「大声死」
--------------------------
爬取主路径在服务器上 02:00 无人值守运行。依赖缺失必须在**导入期**就炸掉，
让 cron 日志里第一眼就能看到；绝不能兜底成「判定恒为 False」—— 那会把风控页
当成正常页面，数据静默变少，比直接失败难查得多。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# scripts/ 的上上级 = 三个仓库的共同父目录，analyzer 就在隔壁
_DEFAULT_ANALYZER_SRC = (
    Path(__file__).resolve().parent.parent.parent / 'xueqiu-analyzer-skill' / 'src'
)


def analyzer_src_path() -> str:
    """analyzer 的 src 目录：XUEQIU_ANALYZER_PATH 优先，否则同级 checkout。"""
    return os.path.expanduser(
        os.environ.get('XUEQIU_ANALYZER_PATH') or str(_DEFAULT_ANALYZER_SRC)
    )


def _ensure_importable() -> None:
    path = analyzer_src_path()
    if path not in sys.path:
        sys.path.insert(0, path)


_ensure_importable()

try:
    from xueqiu_analyzer.waf import (  # noqa: E402
        CONTENT_PATTERNS,
        PAGE_MARKERS,
        TITLE_EXACT,
        block_guard_js,
        classify_failure,
        contains_waf_text,
        has_waf_marker,
        is_error_page,
        is_error_title,
        is_waf_blocked,
        looks_like_waf_content,
    )
except ImportError as exc:  # pragma: no cover - 启动期硬失败
    raise SystemExit(
        "\n[crawler] 无法导入 xueqiu_analyzer.waf（风控判定唯一实现）。\n"
        f"  已尝试路径: {analyzer_src_path()}\n"
        "  修复方式二选一:\n"
        "    1) 设置环境变量 XUEQIU_ANALYZER_PATH=<xueqiu-analyzer-skill>/src\n"
        "    2) 把 xueqiu-analyzer-skill 检出到 xueqiu-crawler 的同级目录\n"
        f"  原始错误: {exc}\n"
    )

__all__ = [
    'CONTENT_PATTERNS',
    'PAGE_MARKERS',
    'TITLE_EXACT',
    'block_guard_js',
    'classify_failure',
    'contains_waf_text',
    'has_waf_marker',
    'is_error_page',
    'is_error_title',
    'is_waf_blocked',
    'looks_like_waf_content',
    'analyzer_src_path',
]
