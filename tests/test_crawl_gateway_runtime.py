from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from crawl_gateway.cli import main as cli_main
from crawl_gateway.config import load_sites_config
from crawl_gateway.models import AttemptResult, AttemptScope, AttemptStatus
from crawl_gateway.orchestrator import Orchestrator, TaskSpec
from crawl_gateway.storage import ActiveJobError, GatewayStore


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class scripted_backend:
    def __init__(self, results: list[AttemptResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[TaskSpec, str]] = []

    def execute(self, task: TaskSpec, backend: str) -> AttemptResult:
        self.calls.append((task, backend))
        return replace(self.results.pop(0), backend=backend)


def result(status: AttemptStatus, **kwargs) -> AttemptResult:
    return AttemptResult(status, "opencli", **kwargs)


@pytest.fixture
def site_config():
    config = load_sites_config(Path("config/sites.yaml"))
    return config.sites["xueqiu"]


def test_site_lock_blocks_active_job_and_releases_finished_job(tmp_path):
    first = GatewayStore(tmp_path / "gateway.sqlite3")
    second = GatewayStore(tmp_path / "gateway.sqlite3")

    job_id = first.claim_job(
        site="xueqiu",
        purpose="daily",
        now=100,
        lock_ttl_seconds=3600,
        config_snapshot={},
    )
    with pytest.raises(ActiveJobError, match=f"active job {job_id}"):
        second.claim_job(
            site="xueqiu",
            purpose="manual",
            now=101,
            lock_ttl_seconds=3600,
            config_snapshot={},
        )

    first.finish_job(job_id, "succeeded", 102)
    new_job_id = second.claim_job(
        site="xueqiu",
        purpose="manual",
        now=103,
        lock_ttl_seconds=3600,
        config_snapshot={},
    )

    assert new_job_id > job_id
    first.close()
    second.close()


def test_expired_site_lock_can_be_reclaimed_after_crash(tmp_path):
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    old_job_id = store.claim_job(
        site="xueqiu",
        purpose="daily",
        now=100,
        lock_ttl_seconds=60,
        config_snapshot={},
    )

    new_job_id = store.claim_job(
        site="xueqiu",
        purpose="retry",
        now=161,
        lock_ttl_seconds=60,
        config_snapshot={},
    )
    lock = store.site_lock("xueqiu")

    assert new_job_id > old_job_id
    assert lock["job_id"] == new_job_id
    store.close()


def test_dry_run_persists_audit_without_site_health_change(tmp_path, capsys):
    database = tmp_path / "gateway.sqlite3"
    exit_code = cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "run",
            "--site",
            "xueqiu",
            "--purpose",
            "test",
            "--resource",
            "user_timeline:1425236713",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1

    store = GatewayStore(database)
    stats = store.stats("xueqiu")
    assert stats["totals"]["total_attempts"] == 1
    assert stats["attempts"][0]["backend"] == "dry-run"
    assert stats["attempts"][0]["status"] == "skipped"
    assert store.load_health("xueqiu") is None
    store.close()


def test_waf_failures_trip_circuit_and_skip_remaining_tasks(tmp_path, site_config):
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    config = replace(
        site_config,
        backend_priority=("opencli",),
        circuit_breaker=replace(site_config.circuit_breaker, failure_threshold=2),
    )
    backend = scripted_backend(
        [
            result(AttemptStatus.BLOCKED_WAF, error="slider"),
            result(AttemptStatus.BLOCKED_WAF, error="slider"),
        ]
    )
    clock = FakeClock()

    summary = Orchestrator(
        store=store,
        site_config=config,
        backend=backend,
        clock=clock,
        sleep=clock.sleep,
    ).run(
        purpose="daily",
        tasks=[
            TaskSpec("user_timeline", "1"),
            TaskSpec("user_timeline", "2"),
            TaskSpec("user_timeline", "3"),
        ],
    )

    assert summary["failed"] == 2
    assert summary["skipped"] == 1
    job = store.stats("xueqiu")["jobs"][0]
    assert job["status"] == "completed_with_failures"
    assert len(backend.calls) == 2
    health = store.load_health("xueqiu")
    assert health is not None
    assert health.state == "open"
    store.close()


def test_retryable_network_error_backs_off_before_success(tmp_path, site_config):
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    backend = scripted_backend(
        [
            result(AttemptStatus.NETWORK_ERROR, error="timeout"),
            result(AttemptStatus.SUCCESS, new_articles=2, saved_articles=2),
        ]
    )
    clock = FakeClock()

    summary = Orchestrator(
        store=store,
        site_config=site_config,
        backend=backend,
        clock=clock,
        sleep=clock.sleep,
    ).run(purpose="daily", tasks=[TaskSpec("user_timeline", "1")])

    assert summary["successful"] == 1
    assert summary["new_articles"] == 2
    assert 60 in clock.sleeps
    assert len(backend.calls) == 2
    store.close()


def test_backend_failure_falls_back_to_next_backend(tmp_path, site_config):
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    backend = scripted_backend(
        [
            result(AttemptStatus.HTTP_ERROR, error="opencli unavailable"),
            result(AttemptStatus.SUCCESS, new_articles=1, saved_articles=1),
        ]
    )
    clock = FakeClock()

    summary = Orchestrator(
        store=store,
        site_config=site_config,
        backend=backend,
        clock=clock,
        sleep=clock.sleep,
    ).run(purpose="daily", tasks=[TaskSpec("user_timeline", "1")])

    assert summary["successful"] == 1
    assert [backend_name for _, backend_name in backend.calls] == [
        "opencli",
        "nodriver",
    ]
    attempts = store.stats("xueqiu")["attempts"]
    assert [attempt["backend"] for attempt in attempts] == ["nodriver", "opencli"]
    store.close()


def test_hard_failure_gets_one_fallback_but_respects_attempt_limit(
    tmp_path, site_config
):
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    config = replace(
        site_config,
        circuit_breaker=replace(site_config.circuit_breaker, failure_threshold=2),
    )
    backend = scripted_backend(
        [
            result(AttemptStatus.BLOCKED_WAF, error="opencli slider"),
            result(AttemptStatus.BLOCKED_WAF, error="nodriver slider"),
        ]
    )
    clock = FakeClock()

    summary = Orchestrator(
        store=store,
        site_config=config,
        backend=backend,
        clock=clock,
        sleep=clock.sleep,
    ).run(purpose="daily", tasks=[TaskSpec("user_timeline", "1")])

    assert summary["failed"] == 1
    assert [backend_name for _, backend_name in backend.calls] == [
        "opencli",
        "nodriver",
    ]
    health = store.load_health("xueqiu")
    assert health is not None
    assert health.state == "open"
    store.close()


def test_rate_limit_seed_survives_new_orchestrator_process(tmp_path, site_config):
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    first_clock = FakeClock(1000)
    first_backend = scripted_backend([result(AttemptStatus.NO_UPDATE)])
    Orchestrator(
        store=store,
        site_config=site_config,
        backend=first_backend,
        clock=first_clock,
        sleep=first_clock.sleep,
    ).run(purpose="daily", tasks=[TaskSpec("user_timeline", "1")])

    second_clock = FakeClock(1005)
    second_backend = scripted_backend([result(AttemptStatus.NO_UPDATE)])
    Orchestrator(
        store=store,
        site_config=site_config,
        backend=second_backend,
        clock=second_clock,
        sleep=second_clock.sleep,
    ).run(purpose="daily", tasks=[TaskSpec("user_timeline", "2")])

    assert 4 in second_clock.sleeps
    store.close()


def test_open_circuit_persists_into_next_job(tmp_path, site_config):
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    config = replace(
        site_config,
        backend_priority=("opencli",),
        rate_limit=replace(
            site_config.rate_limit,
            min_interval_seconds=0.001,
            max_interval_seconds=0.001,
        ),
        circuit_breaker=replace(
            site_config.circuit_breaker,
            failure_threshold=1,
            cooldown_minutes=60,
        ),
    )
    first_clock = FakeClock(1000)
    failing_backend = scripted_backend(
        [result(AttemptStatus.CAPTCHA_REQUIRED, error="slider")]
    )
    Orchestrator(
        store=store,
        site_config=config,
        backend=failing_backend,
        clock=first_clock,
        sleep=first_clock.sleep,
    ).run(purpose="daily", tasks=[TaskSpec("user_timeline", "1")])

    second_clock = FakeClock(1001)
    blocked_backend = scripted_backend([result(AttemptStatus.SUCCESS)])
    summary = Orchestrator(
        store=store,
        site_config=config,
        backend=blocked_backend,
        clock=second_clock,
        sleep=second_clock.sleep,
    ).run(purpose="retry", tasks=[TaskSpec("user_timeline", "1")])

    assert summary["skipped"] == 1
    assert blocked_backend.calls == []
    store.close()


def test_partial_hard_failure_window_persists_across_processes(tmp_path, site_config):
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    config = replace(
        site_config,
        backend_priority=("opencli",),
        rate_limit=replace(
            site_config.rate_limit,
            min_interval_seconds=0.001,
            max_interval_seconds=0.001,
        ),
        circuit_breaker=replace(
            site_config.circuit_breaker,
            failure_threshold=3,
        ),
    )

    first_clock = FakeClock(1000)
    Orchestrator(
        store=store,
        site_config=config,
        backend=scripted_backend([result(AttemptStatus.BLOCKED_WAF, error="slider")]),
        clock=first_clock,
        sleep=first_clock.sleep,
    ).run(purpose="daily", tasks=[TaskSpec("user_timeline", "1")])

    second_clock = FakeClock(1001)
    summary = Orchestrator(
        store=store,
        site_config=config,
        backend=scripted_backend(
            [
                result(AttemptStatus.BLOCKED_WAF, error="slider"),
                result(AttemptStatus.BLOCKED_WAF, error="slider"),
            ]
        ),
        clock=second_clock,
        sleep=second_clock.sleep,
    ).run(
        purpose="retry",
        tasks=[
            TaskSpec("user_timeline", "2"),
            TaskSpec("user_timeline", "3"),
            TaskSpec("user_timeline", "4"),
        ],
    )

    assert summary["failed"] == 2
    assert summary["skipped"] == 1
    health = store.load_health("xueqiu")
    assert health is not None
    assert len(health.hard_failure_times) == 3
    store.close()


def test_concurrent_claims_produce_one_active_job(tmp_path, site_config):
    database = tmp_path / "gateway.sqlite3"

    def claim():
        store = GatewayStore(database)
        try:
            job_id = store.claim_job(
                site="xueqiu",
                purpose="daily",
                now=100,
                lock_ttl_seconds=3600,
                config_snapshot={},
            )
            return job_id, None
        except ActiveJobError as exc:
            return None, str(exc)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))

    job_ids = [job_id for job_id, _ in results if job_id is not None]
    conflicts = [error for _, error in results if error is not None]
    assert len(job_ids) == 1
    assert len(conflicts) == 1


def test_cli_stats_site_filter_does_not_hit_ambiguous_column(tmp_path, capsys):
    database = tmp_path / "gateway.sqlite3"
    cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "run",
            "--site",
            "xueqiu",
            "--resource",
            "user_timeline:1",
            "--dry-run",
        ]
    )
    capsys.readouterr()

    exit_code = cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "stats",
            "--site",
            "xueqiu",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["total_attempts"] == 1
    assert payload["jobs"][0]["site"] == "xueqiu"


def test_cli_experimental_execute_uses_adapter_and_closes_client(tmp_path, monkeypatch):
    import scripts.opencli_extractor
    import crawl_gateway.adapters as adapters

    class FakeExecuteClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeExecuteAdapter:
        def __init__(self, *, client, data_dir):
            self.client = client
            self.data_dir = data_dir

        def execute(self, task, backend):
            return AttemptResult(AttemptStatus.NO_UPDATE, backend)

    fake_client = FakeExecuteClient()
    fake_adapter = FakeExecuteAdapter(client=fake_client, data_dir="unused")
    monkeypatch.setattr(
        scripts.opencli_extractor,
        "is_user_articles_available",
        lambda: True,
    )
    monkeypatch.setattr(adapters, "OpencliArticleClient", lambda: fake_client)
    monkeypatch.setattr(
        adapters,
        "XueqiuAdapter",
        lambda *, client, data_dir: fake_adapter,
    )
    monkeypatch.setattr(
        adapters,
        "XueqiuBackend",
        lambda *, opencli, nodriver: fake_adapter,
    )
    database = tmp_path / "gateway.sqlite3"

    exit_code = cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "run",
            "--site",
            "xueqiu",
            "--resource",
            "user_timeline:5739488179",
            "--data-dir",
            str(tmp_path / "site-data"),
            "--execute",
        ]
    )

    assert exit_code == 0
    assert fake_client.closed is True
    store = GatewayStore(database)
    stats = store.stats("xueqiu")
    assert stats["totals"]["successful"] == 1
    store.close()


def test_export_last_crawl_stats_matches_legacy_fields(tmp_path):
    from datetime import datetime

    from crawl_gateway.compatibility import export_last_crawl_stats

    summary = {
        "successful": 2,
        "failed": 1,
        "skipped": 1,
        "new_articles": 5,
        "saved_articles": 5,
    }

    path = export_last_crawl_stats(
        summary=summary,
        total_tasks=4,
        data_dir=tmp_path / "site-data",
        now=datetime(2026, 10, 7, 8, 0, 0),
    )

    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "date": "2026-10-07",
        "total_users": 4,
        "successful": 2,
        "failed": 1,
        "new_articles": 5,
        # 与 legacy 爬虫写出的同名字段对齐：切到 gateway 引擎后，
        # 「安静日 vs 被拦日」这个区分不能丢。
        "blocked_articles": 0,
    }


def test_export_last_crawl_stats_carries_blocked_articles(tmp_path):
    from crawl_gateway.compatibility import export_last_crawl_stats

    path = export_last_crawl_stats(
        summary={
            "successful": 1,
            "failed": 2,
            "new_articles": 0,
            "blocked_articles": 2,
        },
        total_tasks=3,
        data_dir=tmp_path / "site-data",
    )

    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    # 这一组正是要防的误读：看起来「1/3 成功、新增 0」，其实有 2 个账号被拦
    assert payload["blocked_articles"] == 2
    assert payload["new_articles"] == 0


def test_export_last_crawl_stats_write_failure_returns_none(tmp_path):
    from crawl_gateway.compatibility import export_last_crawl_stats

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    returned = export_last_crawl_stats(
        summary={"successful": 1, "failed": 0, "new_articles": 2},
        total_tasks=1,
        data_dir=blocker / "site-data",
    )

    assert returned is None


def test_cli_execute_exports_legacy_crawl_stats(tmp_path, monkeypatch):
    from datetime import date

    import crawl_gateway.adapters as adapters
    import scripts.opencli_extractor

    class FakeClient:
        def close(self):
            pass

    class FakeAdapter:
        def execute(self, task, backend):
            if task.resource_id == "5739488179":
                return AttemptResult(
                    AttemptStatus.SUCCESS, backend, new_articles=3, saved_articles=3
                )
            return AttemptResult(AttemptStatus.NO_UPDATE, backend)

    fake_adapter = FakeAdapter()
    monkeypatch.setattr(
        scripts.opencli_extractor, "is_user_articles_available", lambda: True
    )
    monkeypatch.setattr(adapters, "OpencliArticleClient", lambda: FakeClient())
    monkeypatch.setattr(
        adapters, "XueqiuAdapter", lambda *, client, data_dir: fake_adapter
    )
    monkeypatch.setattr(
        adapters, "XueqiuBackend", lambda *, opencli, nodriver: fake_adapter
    )
    database = tmp_path / "gateway.sqlite3"
    data_dir = tmp_path / "site-data"

    exit_code = cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "run",
            "--site",
            "xueqiu",
            "--resource",
            "user_timeline:5739488179",
            "--resource",
            "user_timeline:1111111111",
            "--data-dir",
            str(data_dir),
            "--execute",
        ]
    )

    assert exit_code == 0
    stats = json.loads(
        (data_dir / ".last_crawl_stats.json").read_text(encoding="utf-8")
    )
    assert stats == {
        "date": date.today().isoformat(),
        "total_users": 2,
        "successful": 2,
        "failed": 0,
        "new_articles": 3,
        "blocked_articles": 0,
    }


def test_cli_verify_green_when_all_checks_pass(tmp_path, monkeypatch, site_config):
    import crawl_gateway.verify as verify_mod

    monkeypatch.setattr(
        verify_mod, "opencli_doctor", lambda: (True, "extension connected")
    )
    database = tmp_path / "gateway.sqlite3"

    exit_code = cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "verify",
            "--site",
            "xueqiu",
            "--data-dir",
            str(tmp_path / "site-data"),
        ]
    )

    assert exit_code == 0


def test_cli_verify_fails_when_circuit_open(tmp_path, monkeypatch, site_config):
    import crawl_gateway.verify as verify_mod

    monkeypatch.setattr(
        verify_mod, "opencli_doctor", lambda: (True, "extension connected")
    )
    database = tmp_path / "gateway.sqlite3"
    store = GatewayStore(database)
    # opened_at 用「刚刚」—— 冷却（xueqiu 首跳 15 分钟）还没过，这才该判不健康。
    # 用 1970 那种老时间戳会被判成「冷却已过期」，见下一个用例。
    now = time.time()
    store.save_health(
        "xueqiu",
        score=20,
        state="open",
        opened_at=now,
        hard_failure_times=(now,),
        now=now,
    )
    store.close()

    exit_code = cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "verify",
            "--site",
            "xueqiu",
            "--data-dir",
            str(tmp_path / "site-data"),
        ]
    )

    assert exit_code == 1


def test_cli_verify_passes_when_cooldown_expired(tmp_path, monkeypatch, site_config):
    """冷却已过期的 open 不算不健康 —— 下次运行就会半开探测.

    回归：verify 原来只认 `state == "open"`。而 `run_daily.sh` 是 `set -e`，
    gateway 分支先 verify 再 run —— 一份冷却早就过期的 open 记录会让流水线在
    爬取前中止，而唯一能把状态写回去的就是那次爬取，于是永远卡死。
    """
    import crawl_gateway.verify as verify_mod

    monkeypatch.setattr(
        verify_mod, "opencli_doctor", lambda: (True, "extension connected")
    )
    database = tmp_path / "gateway.sqlite3"
    store = GatewayStore(database)
    store.save_health(
        "xueqiu",
        score=20,
        state="open",
        opened_at=1000.0,  # 1970 —— 冷却早就过了
        hard_failure_times=(1000.0,),
        now=1001.0,
    )
    store.close()

    exit_code = cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "verify",
            "--site",
            "xueqiu",
            "--data-dir",
            str(tmp_path / "site-data"),
        ]
    )

    assert exit_code == 0


def test_detail_scope_failures_never_open_the_circuit(tmp_path, site_config):
    """端到端钉住 scope 传播：适配器的 DETAIL 级失败不能触发站点级熔断.

    回归防线：`breaker.record(..., scope=executed.scope)` 这条接线原来**零覆盖** ——
    评审做变异测试时把它改回 `record(executed.status, ...)`，全量 139 个测试仍全绿。
    这里走真实 Orchestrator + scripted backend：5 个任务各返回一次
    BLOCKED_WAF(DETAIL)，断言熔断器始终 closed、且 5 个任务全都真的被执行过
    （没有被熔断跳过）。
    """
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    config = replace(
        site_config,
        backend_priority=(
            "opencli",
        ),  # 单后端 → 不会回退，一个任务只消耗一条 scripted 结果
        rate_limit=replace(
            site_config.rate_limit,
            min_interval_seconds=0.001,
            max_interval_seconds=0.001,
        ),
        retry=replace(site_config.retry, retry_statuses=frozenset()),
    )
    clock = FakeClock(2000)
    backend = scripted_backend(
        [
            result(
                AttemptStatus.BLOCKED_WAF,
                error=f"waf_content:{i}",
                scope=AttemptScope.DETAIL,
            )
            for i in range(5)
        ]
    )

    summary = Orchestrator(
        store=store,
        site_config=config,
        backend=backend,
        clock=clock,
        sleep=clock.sleep,
    ).run(
        purpose="daily",
        tasks=[TaskSpec("user_timeline", str(i)) for i in range(5)],
    )

    health = store.load_health("xueqiu")
    assert health is not None
    assert health.state == "closed", "详情级 WAF 不该把站点判成不可达"
    assert health.hard_failure_times == ()
    assert len(backend.calls) == 5, "5 个任务都该真的被执行，没被熔断跳过"
    assert summary["skipped"] == 0


def test_config_snapshot_carries_the_cooldown_ladder(tmp_path, site_config):
    """配置快照要能还原当时的冷却策略 —— 少了阶梯两个参数就还原不出实际时长."""
    store = GatewayStore(tmp_path / "gateway.sqlite3")
    orchestrator = Orchestrator(
        store=store,
        site_config=site_config,
        backend=scripted_backend([]),
        clock=FakeClock(3000),
    )

    snapshot = orchestrator._config_snapshot()["circuit_breaker"]

    assert snapshot["cooldown_minutes"] == site_config.circuit_breaker.cooldown_minutes
    assert (
        snapshot["cooldown_multiplier"]
        == site_config.circuit_breaker.cooldown_multiplier
    )
    assert (
        snapshot["max_cooldown_minutes"]
        == site_config.circuit_breaker.max_cooldown_minutes
    )


def test_cli_verify_fails_when_opencli_unavailable(tmp_path, monkeypatch, site_config):
    import crawl_gateway.verify as verify_mod

    monkeypatch.setattr(
        verify_mod,
        "opencli_doctor",
        lambda: (False, "opencli not found in PATH"),
    )
    database = tmp_path / "gateway.sqlite3"

    exit_code = cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "verify",
            "--site",
            "xueqiu",
            "--data-dir",
            str(tmp_path / "site-data"),
        ]
    )

    assert exit_code == 1


def test_cli_retry_failed_replays_only_failed_tasks(tmp_path, monkeypatch, site_config):
    import crawl_gateway.adapters as adapters
    import scripts.opencli_extractor

    clock = FakeClock(1000.0)
    database = tmp_path / "gateway.sqlite3"
    store = GatewayStore(database)
    first_backend = scripted_backend(
        [
            result(AttemptStatus.SUCCESS, new_articles=2, saved_articles=2),
            result(AttemptStatus.BLOCKED_WAF),
            result(AttemptStatus.BLOCKED_WAF),
        ]
    )
    first = Orchestrator(
        store=store,
        site_config=site_config,
        backend=first_backend,
        clock=clock,
        sleep=clock.sleep,
    ).run(
        purpose="daily",
        tasks=[TaskSpec("user_timeline", "a"), TaskSpec("user_timeline", "b")],
    )
    job_id = first["job_id"]
    store.close()
    assert first["successful"] == 1
    assert first["failed"] == 1

    class FakeClient:
        def close(self):
            pass

    class FakeAdapter:
        def __init__(self):
            self.calls: list[str] = []

        def execute(self, task, backend):
            self.calls.append(task.resource_id)
            return AttemptResult(
                AttemptStatus.SUCCESS, backend, new_articles=1, saved_articles=1
            )

    fake_adapter = FakeAdapter()
    monkeypatch.setattr(
        scripts.opencli_extractor, "is_user_articles_available", lambda: True
    )
    monkeypatch.setattr(adapters, "OpencliArticleClient", lambda: FakeClient())
    monkeypatch.setattr(
        adapters, "XueqiuAdapter", lambda *, client, data_dir: fake_adapter
    )
    monkeypatch.setattr(
        adapters, "XueqiuBackend", lambda *, opencli, nodriver: fake_adapter
    )
    data_dir = tmp_path / "site-data"
    data_dir.mkdir()
    daily_stats = data_dir / ".last_crawl_stats.json"
    daily_payload = {
        "date": "2026-10-07",
        "total_users": 10,
        "successful": 9,
        "failed": 1,
        "new_articles": 42,
    }
    daily_stats.write_text(json.dumps(daily_payload), encoding="utf-8")

    exit_code = cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "retry-failed",
            "--job",
            str(job_id),
            "--data-dir",
            str(data_dir),
            "--execute",
        ]
    )

    assert exit_code == 0
    # 只重放失败的 b，已成功的 a 不再访问
    assert fake_adapter.calls == ["b"]
    store = GatewayStore(database)
    retry_job = next(
        job for job in store.stats("xueqiu")["jobs"] if job["purpose"] == "retry:daily"
    )
    store.close()
    assert retry_job["status"] == "succeeded"
    # 重放不得覆写当日整轮统计，否则日报会从 9/10 变成 1/1
    assert json.loads(daily_stats.read_text(encoding="utf-8")) == daily_payload


def test_cli_retry_failed_respects_open_circuit(tmp_path, monkeypatch, site_config):
    """熔断打开时 retry-failed 只标记跳过，不得真实访问站点。"""
    import time

    import crawl_gateway.adapters as adapters
    import scripts.opencli_extractor

    clock = FakeClock(1000.0)
    database = tmp_path / "gateway.sqlite3"
    store = GatewayStore(database)
    first = Orchestrator(
        store=store,
        site_config=site_config,
        backend=scripted_backend(
            [result(AttemptStatus.BLOCKED_WAF), result(AttemptStatus.BLOCKED_WAF)]
        ),
        clock=clock,
        sleep=clock.sleep,
    ).run(purpose="daily", tasks=[TaskSpec("user_timeline", "b")])
    job_id = first["job_id"]
    assert first["failed"] == 1
    health = store.load_health("xueqiu")
    assert health is not None
    assert health.state != "open"  # 2 次硬失败未达阈值 3
    opened_at = time.time()
    store.save_health(
        "xueqiu",
        score=20,
        state="open",
        opened_at=opened_at,
        hard_failure_times=(opened_at,),
        now=opened_at,
    )
    store.close()

    calls: list[str] = []

    class FakeClient:
        def close(self):
            pass

    class FakeAdapter:
        def execute(self, task, backend):
            calls.append(task.resource_id)
            return AttemptResult(AttemptStatus.SUCCESS, backend)

    monkeypatch.setattr(
        scripts.opencli_extractor, "is_user_articles_available", lambda: True
    )
    monkeypatch.setattr(adapters, "OpencliArticleClient", lambda: FakeClient())
    monkeypatch.setattr(
        adapters, "XueqiuAdapter", lambda *, client, data_dir: FakeAdapter()
    )
    monkeypatch.setattr(
        adapters, "XueqiuBackend", lambda *, opencli, nodriver: FakeAdapter()
    )
    data_dir = tmp_path / "site-data"

    exit_code = cli_main(
        [
            "--config",
            "config/sites.yaml",
            "--db",
            str(database),
            "retry-failed",
            "--job",
            str(job_id),
            "--data-dir",
            str(data_dir),
            "--execute",
        ]
    )

    assert exit_code == 0
    assert calls == []
    store = GatewayStore(database)
    retry_job = next(
        job for job in store.stats("xueqiu")["jobs"] if job["purpose"] == "retry:daily"
    )
    store.close()
    assert retry_job["status"] == "blocked"
    assert not (data_dir / ".last_crawl_stats.json").exists()
