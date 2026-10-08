from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AttemptStatus(StrEnum):
    SUCCESS = "success"
    NO_UPDATE = "no_update"
    DUPLICATE = "duplicate"
    BLOCKED_WAF = "blocked_waf"
    CAPTCHA_REQUIRED = "captcha_required"
    AUTH_EXPIRED = "auth_expired"
    RATE_LIMITED = "rate_limited"
    HTTP_ERROR = "http_error"
    PARSE_ERROR = "parse_error"
    NETWORK_ERROR = "network_error"
    CIRCUIT_OPEN = "circuit_open"
    SKIPPED = "skipped"


class AttemptScope(StrEnum):
    """一次尝试失败到了哪一层 —— 决定它算不算熔断信号。

    ACCOUNT: 账号级。整条任务起不来（时间线都拿不到），站点对这个账号就是不通的。
    DETAIL:  详情级。列表页正常，只是某篇文章的正文被拦 —— 站点对该账号可达，
             不该按「站点挂了」处理（2026-10-08 实测：阿里云 WAF 只拦
             `/uid/statusid` 这类文章 URL，而 whoami / watchlist / 首页全正常，
             3 次详情级 WAF 却让熔断器把剩下 8 个账号全跳过了）。
    """

    ACCOUNT = "account"
    DETAIL = "detail"


SUCCESS_STATUSES = frozenset(
    {
        AttemptStatus.SUCCESS,
        AttemptStatus.NO_UPDATE,
        AttemptStatus.DUPLICATE,
    }
)
FAILURE_STATUSES = frozenset(
    {
        AttemptStatus.BLOCKED_WAF,
        AttemptStatus.CAPTCHA_REQUIRED,
        AttemptStatus.AUTH_EXPIRED,
        AttemptStatus.RATE_LIMITED,
        AttemptStatus.HTTP_ERROR,
        AttemptStatus.PARSE_ERROR,
        AttemptStatus.NETWORK_ERROR,
    }
)
HARD_FAILURE_STATUSES = frozenset(
    {
        AttemptStatus.BLOCKED_WAF,
        AttemptStatus.CAPTCHA_REQUIRED,
        AttemptStatus.AUTH_EXPIRED,
    }
)


@dataclass(frozen=True)
class AttemptResult:
    status: AttemptStatus
    backend: str
    error: str | None = None
    duration_ms: int = 0
    new_articles: int = 0
    saved_articles: int = 0
    # 被风控拦掉正文的篇数。适配器撞上第一篇被拦的文章就中止该账号，所以这里
    # 实际是「遇到被拦的账号数」（0/1）—— 够用来区分「安静日」与「被拦日」，
    # 与 legacy 爬虫同名字段对不上精度是已知取舍。
    blocked_articles: int = 0
    # 默认账号级 = 引入本字段之前的行为，调用方不显式标注时语义不变。
    scope: AttemptScope = AttemptScope.ACCOUNT

    @property
    def successful(self) -> bool:
        return self.status in SUCCESS_STATUSES
