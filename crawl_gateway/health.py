from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from crawl_gateway.config import CircuitBreakerConfig
from crawl_gateway.models import SUCCESS_STATUSES, AttemptScope, AttemptStatus


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitDecision:
    allowed: bool
    state: CircuitState
    probe: bool = False
    retry_after_seconds: float = 0.0


class CircuitBreaker:
    """站点级熔断器：站点对这个账号不可达时才停。

    2026-10-08 调整了两处策略，起因是当天 shadow 对比整轮作废：

    1. **按范围计数**。原来任何 `blocked_waf` 都算硬失败，于是「列表页正常、
       只是某篇文章正文被拦」也会把站点判成挂了 —— 当晚阿里云 WAF 只拦
       `/uid/statusid` 这种文章 URL（whoami / watchlist / 首页全正常），
       3 次详情级 WAF 就跳闸 60 分钟、把剩下 8 个账号全跳过。
       现在只有账号级（时间线都拿不到）才计数；详情级只记 score。
    2. **冷却递进**。原来固定 60 分钟，而单轮 job 预算约 40 分钟 —— 一跳闸当轮
       必然报废。现在首跳 `cooldown_minutes`，每次复犯乘 `cooldown_multiplier`，
       封顶 `max_cooldown_minutes`，让一轮内还有「暂停 → 恢复」的机会。
    """

    def __init__(self, config: CircuitBreakerConfig) -> None:
        self._config = config
        self.score = 80
        self.state = CircuitState.CLOSED
        self.opened_at = 0.0
        # 跳闸次数（复犯计数）→ 冷却递进的依据；真成功时清零。
        self.open_count = 0
        self._probe_pending = False
        self._hard_failures: deque[float] = deque()

    @property
    def hard_failure_times(self) -> tuple[float, ...]:
        return tuple(self._hard_failures)

    @property
    def cooldown_seconds(self) -> float:
        """本次跳闸应冷却多久：首跳 cooldown_minutes，每复犯一次乘 multiplier，封顶.

        只在 `_open()` 里自增 open_count，所以熔断开启期间本值是稳定的 ——
        `allow()` 每次重算不会漂移。
        """
        if self.open_count <= 1:
            minutes = self._config.cooldown_minutes
        else:
            minutes = self._config.cooldown_minutes * (
                self._config.cooldown_multiplier ** (self.open_count - 1)
            )
        return min(minutes, self._config.max_cooldown_minutes) * 60

    def seed_hard_failures(self, timestamps: list[float]) -> None:
        self._hard_failures.clear()
        self._hard_failures.extend(sorted(timestamps))

    def allow(self, now: float) -> CircuitDecision:
        opened_until = self.opened_at + self.cooldown_seconds
        if self.state == CircuitState.OPEN:
            if now < opened_until:
                return CircuitDecision(
                    allowed=False,
                    state=CircuitState.OPEN,
                    retry_after_seconds=opened_until - now,
                )
            self.state = CircuitState.HALF_OPEN
            self._probe_pending = True
            return CircuitDecision(True, CircuitState.HALF_OPEN, probe=True)

        if self.state == CircuitState.HALF_OPEN and self._probe_pending:
            return CircuitDecision(False, CircuitState.HALF_OPEN)
        return CircuitDecision(True, CircuitState.CLOSED)

    def record(
        self,
        status: AttemptStatus,
        now: float,
        *,
        scope: AttemptScope = AttemptScope.ACCOUNT,
    ) -> None:
        """记一次尝试结果。scope 决定这次失败是否算「站点不可达」。"""
        # 只有账号级的硬失败才算熔断信号；详情级失败不计数（见类 docstring）。
        is_hard_failure = (
            status in self._config.hard_failure_statuses
            and scope is AttemptScope.ACCOUNT
        )

        if status in SUCCESS_STATUSES:
            self.score = min(100, self.score + 2)
            self._hard_failures.clear()
            self._probe_pending = False
            self.state = CircuitState.CLOSED
            self.open_count = 0
            return

        self.score = max(0, self.score - self._penalty(status))

        if is_hard_failure:
            cutoff = now - self._config.window_minutes * 60
            while self._hard_failures and self._hard_failures[0] <= cutoff:
                self._hard_failures.popleft()
            self._hard_failures.append(now)

        if (
            is_hard_failure
            and len(self._hard_failures) >= self._config.failure_threshold
        ):
            self._open(now)
            return

        if self.state == CircuitState.HALF_OPEN:
            # 探针拿到「详情级硬失败」= 列表页通了，只是某篇正文被拦 → 站点对这个
            # 账号可达，闭合放行。**不**清硬失败计数、不重置 open_count：那不是
            # 「恢复」，不该冒充成功，也不该抹掉复犯递进。
            # 其余任何失败（账号级 WAF、网络错误…）都维持原有语义：复开。
            detail_scoped = (
                scope is AttemptScope.DETAIL
                and status in self._config.hard_failure_statuses
            )
            if detail_scoped:
                self.state = CircuitState.CLOSED
                self._probe_pending = False
            else:
                self._open(now)

    def _open(self, now: float) -> None:
        self.state = CircuitState.OPEN
        self.opened_at = now
        self.open_count += 1
        self._probe_pending = False

    @staticmethod
    def _penalty(status: AttemptStatus) -> int:
        if status == AttemptStatus.RATE_LIMITED:
            return 20
        if status in {
            AttemptStatus.BLOCKED_WAF,
            AttemptStatus.CAPTCHA_REQUIRED,
            AttemptStatus.AUTH_EXPIRED,
        }:
            return 40
        return 10
