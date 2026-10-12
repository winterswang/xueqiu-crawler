"""插件源码必须只存在于独立仓：本仓内的副本不允许再出现。

背景（2026-10-12 第二阶段迁移）：此前本仓 `opencli-adapters/` 里有一份副本，而真正在跑的是
独立仓 —— 于是出现一类**假绿灯**：改副本「没效果」，而守卫测试读的正是那份副本，照样全绿。
副本已删除；这条测试钉住它不会被人（或某次 git 操作 / 脚本）重新塞回来。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_REPO_COPY = REPO_ROOT / "opencli-adapters"


def test_in_repo_copy_is_gone():
    assert not IN_REPO_COPY.exists(), (
        f"{IN_REPO_COPY} 又出现了。插件源码只应存在于独立仓 opencli-adapters；"
        "本仓副本会让 tests/plugin_source.py 的守卫读到不生效的那份（假绿灯）。"
    )
