from __future__ import annotations

from crawl_gateway.adapters.xueqiu import XueqiuAdapter
from crawl_gateway.adapters.xueqiu_nodriver import XueqiuNodriverAdapter
from crawl_gateway.models import AttemptResult, AttemptStatus
from crawl_gateway.orchestrator import TaskSpec


class XueqiuBackend:
    def __init__(self, *, opencli: XueqiuAdapter, nodriver: XueqiuNodriverAdapter):
        self.opencli = opencli
        self.nodriver = nodriver

    def execute(self, task: TaskSpec, backend: str) -> AttemptResult:
        if backend == "opencli":
            return self.opencli.execute(task, backend)
        if backend == "nodriver":
            return self.nodriver.execute(task, backend)
        return AttemptResult(
            AttemptStatus.SKIPPED,
            backend,
            error=f"unknown_xueqiu_backend:{backend}",
        )
