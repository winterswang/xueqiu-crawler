"""测试期日志必须落在临时目录，不能写进生产日志。

回归（2026-10-05）：`logging_utils` 在导入时就挂上 `logs/cron_daily.log` 的
handler，于是跑一次 pytest 就往生产日志里塞几十行 —— 其中
「已生成「今日无新增」最小日报: /private/var/folders/.../pytest-35/...」
与真实产出同形，而 09:00 巡检靠读该日志末尾判断当天爬取跑没跑完。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent


def test_log_dir_is_redirected_during_tests():
    """本套件运行时 LOG_DIR 必须**不是**仓库的 logs/ 目录。

    这条是整个隔离机制的总闸：conftest 一旦失效，它会立刻变红。
    """
    sys.path.insert(0, str(_REPO / "scripts"))
    import logging_utils

    production = (logging_utils.PROJECT_DIR / "logs").resolve()
    assert logging_utils.LOG_DIR.resolve() != production, (
        f"测试正在往生产日志目录写：{logging_utils.LOG_DIR}"
    )


def test_log_dir_honors_env_override(tmp_path):
    """子进程验证：设了 XUEQIU_LOG_DIR 就按它走，且目录会被建出来。"""
    target = tmp_path / "custom-logs"
    env = dict(os.environ, XUEQIU_LOG_DIR=str(target))

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging_utils, sys; sys.stdout.write(str(logging_utils.LOG_DIR))",
        ],
        cwd=_REPO / "scripts",
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert pathlib.Path(result.stdout).resolve() == target.resolve()
    assert target.is_dir()


def test_log_dir_defaults_into_repo_logs():
    """没设环境变量时仍落回仓库 logs/ —— 生产行为不能被这层隔离改掉。"""
    env = {k: v for k, v in os.environ.items() if k != "XUEQIU_LOG_DIR"}

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging_utils, sys; sys.stdout.write(str(logging_utils.LOG_DIR))",
        ],
        cwd=_REPO / "scripts",
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert pathlib.Path(result.stdout).resolve() == (_REPO / "logs").resolve()
