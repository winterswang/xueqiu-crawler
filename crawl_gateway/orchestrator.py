from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from crawl_gateway.config import SiteConfig
from crawl_gateway.health import CircuitBreaker, CircuitState
from crawl_gateway.models import FAILURE_STATUSES, AttemptResult, AttemptStatus
from crawl_gateway.rate_limiter import SiteRateLimiter
from crawl_gateway.retry import RetryPolicy
from crawl_gateway.storage import GatewayStore


@dataclass(frozen=True)
class TaskSpec:
    resource_type: str
    resource_id: str


class TaskBackend(Protocol):
    def execute(self, task: TaskSpec, backend: str) -> AttemptResult:
        pass


class Orchestrator:
    def __init__(
        self,
        *,
        store: GatewayStore,
        site_config: SiteConfig,
        backend: TaskBackend | None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._site_config = site_config
        self._backend = backend
        self._clock = clock
        self._sleep = sleep
        self._retry_policy = RetryPolicy(site_config.retry)

    def run(
        self,
        *,
        purpose: str,
        tasks: Sequence[TaskSpec],
        dry_run: bool = False,
    ) -> dict[str, object]:
        started_at = self._clock()
        job_id = self._store.claim_job(
            site=self._site_config.code,
            purpose=purpose,
            now=started_at,
            lock_ttl_seconds=3600,
            config_snapshot=self._config_snapshot(),
        )

        try:
            if dry_run:
                summary = self._run_dry_job(job_id, tasks, started_at)
            else:
                if self._backend is None:
                    raise ValueError("a task backend is required unless dry_run is set")
                summary = self._run_job(job_id, tasks)
            job_status = self._job_status(summary, dry_run=dry_run, tasks=tasks)
            self._store.finish_job(job_id, job_status, self._clock())
            return {"job_id": job_id, **summary}
        except Exception:
            self._store.finish_job(job_id, "failed", self._clock())
            raise

    def _run_job(self, job_id: int, tasks: Sequence[TaskSpec]) -> dict[str, object]:
        breaker = self._load_breaker()
        limiter = self._load_limiter()
        successful = 0
        failed = 0
        skipped = 0
        new_articles = 0
        saved_articles = 0

        for task_spec in tasks:
            task_id = self._store.create_task(
                job_id=job_id,
                resource_type=task_spec.resource_type,
                resource_id=task_spec.resource_id,
                now=self._clock(),
            )
            result = self._execute_with_policy(task_spec, task_id, breaker, limiter)
            if result.status in {
                AttemptStatus.SUCCESS,
                AttemptStatus.NO_UPDATE,
                AttemptStatus.DUPLICATE,
            }:
                successful += 1
                new_articles += result.new_articles
                saved_articles += result.saved_articles
            elif result.status in {AttemptStatus.SKIPPED, AttemptStatus.CIRCUIT_OPEN}:
                skipped += 1
            else:
                failed += 1

        return {
            "successful": successful,
            "failed": failed,
            "skipped": skipped,
            "new_articles": new_articles,
            "saved_articles": saved_articles,
        }

    @staticmethod
    def _job_status(
        summary: dict[str, object], *, dry_run: bool, tasks: Sequence[TaskSpec]
    ) -> str:
        if dry_run:
            return "succeeded"
        if summary["failed"]:
            return "completed_with_failures"
        if tasks and not summary["successful"]:
            return "blocked"
        return "succeeded"

    def _execute_with_policy(
        self,
        task_spec: TaskSpec,
        task_id: int,
        breaker: CircuitBreaker,
        limiter: SiteRateLimiter,
    ) -> AttemptResult:
        assert self._backend is not None
        completed_attempts = 0

        while True:
            now = self._clock()
            circuit_decision = breaker.allow(now)
            if not circuit_decision.allowed:
                result = AttemptResult(
                    AttemptStatus.CIRCUIT_OPEN,
                    "circuit-breaker",
                    error=f"retry_after={circuit_decision.retry_after_seconds}",
                )
                self._record(task_id, result, now)
                self._store.finish_task(task_id, "skipped", self._clock())
                return result

            reservation = limiter.reserve(now)
            if not reservation.allowed:
                result = AttemptResult(
                    AttemptStatus.SKIPPED,
                    "rate-limiter",
                    error=reservation.reason,
                )
                self._record(task_id, result, now)
                self._store.finish_task(task_id, "skipped", self._clock())
                return result
            if reservation.wait_seconds:
                self._sleep(reservation.wait_seconds)

            attempt_started_at = self._clock()
            executed = self._backend.execute(
                task_spec,
                self._site_config.backend_priority[
                    min(completed_attempts, len(self._site_config.backend_priority) - 1)
                ],
            )
            completed_attempts += 1
            self._record(task_id, executed, attempt_started_at)
            breaker.record(executed.status, self._clock(), scope=executed.scope)
            self._save_health(breaker)

            if executed.successful:
                self._store.finish_task(task_id, "succeeded", self._clock())
                return executed

            retry_decision = self._retry_policy.decide(
                executed.status, completed_attempts
            )
            has_next_backend = completed_attempts < len(
                self._site_config.backend_priority
            )
            can_fallback = executed.status in FAILURE_STATUSES and has_next_backend
            if not retry_decision.retry and not can_fallback:
                self._store.finish_task(task_id, "failed", self._clock())
                return executed
            fallback_delay = retry_decision.delay_seconds
            if can_fallback and not retry_decision.retry:
                fallback_delay = self._site_config.retry.backoff_base_seconds
            if fallback_delay:
                self._sleep(fallback_delay)

    def _run_dry_job(
        self, job_id: int, tasks: Sequence[TaskSpec], started_at: float
    ) -> dict[str, object]:
        for task_spec in tasks:
            task_id = self._store.create_task(
                job_id=job_id,
                resource_type=task_spec.resource_type,
                resource_id=task_spec.resource_id,
                now=started_at,
            )
            result = AttemptResult(AttemptStatus.SKIPPED, "dry-run")
            self._record(task_id, result, started_at)
            self._store.finish_task(task_id, "skipped", started_at)
        return {
            "successful": 0,
            "failed": 0,
            "skipped": len(tasks),
            "new_articles": 0,
            "saved_articles": 0,
        }

    def _load_breaker(self) -> CircuitBreaker:
        breaker = CircuitBreaker(self._site_config.circuit_breaker)
        snapshot = self._store.load_health(self._site_config.code)
        if snapshot is not None:
            breaker.score = snapshot.score
            breaker.state = CircuitState(snapshot.state)
            breaker.opened_at = snapshot.opened_at
            breaker.open_count = snapshot.open_count
            breaker.seed_hard_failures(list(snapshot.hard_failure_times))
        return breaker

    def _load_limiter(self) -> SiteRateLimiter:
        now = self._clock()
        limiter = SiteRateLimiter(self._site_config.rate_limit)
        timestamps = self._store.attempt_times(
            self._site_config.code,
            now - 86400,
            {"circuit-breaker", "rate-limiter", "dry-run"},
        )
        limiter.seed(timestamps)
        return limiter

    def _save_health(self, breaker: CircuitBreaker) -> None:
        self._store.save_health(
            self._site_config.code,
            breaker.score,
            breaker.state.value,
            breaker.opened_at,
            breaker.hard_failure_times,
            self._clock(),
            breaker.open_count,
        )

    def _record(self, task_id: int, result: AttemptResult, started_at: float) -> None:
        self._store.record_attempt(
            task_id=task_id,
            backend=result.backend,
            status=result.status.value,
            started_at=started_at,
            duration_ms=result.duration_ms,
            error=result.error,
            new_articles=result.new_articles,
            saved_articles=result.saved_articles,
        )

    def _config_snapshot(self) -> dict[str, object]:
        rate_limit = self._site_config.rate_limit
        retry = self._site_config.retry
        circuit_breaker = self._site_config.circuit_breaker
        return {
            "enabled": self._site_config.enabled,
            "backend_priority": list(self._site_config.backend_priority),
            "rate_limit": {
                "concurrency": rate_limit.concurrency,
                "min_interval_seconds": rate_limit.min_interval_seconds,
                "max_interval_seconds": rate_limit.max_interval_seconds,
                "hourly_requests": rate_limit.hourly_requests,
                "daily_requests": rate_limit.daily_requests,
            },
            "retry": {
                "max_attempts": retry.max_attempts,
                "backoff_base_seconds": retry.backoff_base_seconds,
                "backoff_multiplier": retry.backoff_multiplier,
                "retry_statuses": sorted(
                    status.value for status in retry.retry_statuses
                ),
            },
            "circuit_breaker": {
                "failure_threshold": circuit_breaker.failure_threshold,
                "window_minutes": circuit_breaker.window_minutes,
                "cooldown_minutes": circuit_breaker.cooldown_minutes,
                "hard_failure_statuses": sorted(
                    status.value for status in circuit_breaker.hard_failure_statuses
                ),
            },
        }
