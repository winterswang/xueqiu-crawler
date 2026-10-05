"""测试隔离：别把 pytest 的日志写进生产日志。

`scripts/logging_utils.py` 在**导入时**就建 `logs/` 目录、挂上
`logs/cron_daily.log` 的 RotatingFileHandler。所以任何导入它的测试都会往
生产日志里写。2026-10-05 实测 `logs/cron_daily.log` 里积了 69 行 pytest 噪音，
其中包括

    已生成「今日无新增」最小日报: /private/var/folders/.../pytest-of-wangguangchao/pytest-35/...

与真实产出**字样完全同形** —— 而 09:00 的巡检 prompt 明确要求读这个日志的
末尾来判断当天爬取是否跑完，这种噪音足以把判断带偏。

conftest 在测试模块被导入**之前**加载，所以在这里设环境变量就能把日志目录
重定向到临时目录。必须是导入期生效，放进 fixture 就晚了（模块早已 import）。
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

_LOG_DIR = tempfile.mkdtemp(prefix="xueqiu-test-logs-")
os.environ["XUEQIU_LOG_DIR"] = _LOG_DIR
atexit.register(shutil.rmtree, _LOG_DIR, ignore_errors=True)
