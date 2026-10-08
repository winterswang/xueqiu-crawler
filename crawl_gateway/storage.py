from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ActiveJobError(RuntimeError):
    pass


@dataclass(frozen=True)
class HealthSnapshot:
    score: int
    state: str
    opened_at: float
    hard_failure_times: tuple[float, ...]
    open_count: int = 0


class GatewayStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self.migrate()

    def close(self) -> None:
        self._connection.close()

    def ping(self) -> None:
        """验证数据库可写（拿一次写锁即释放）。"""
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")

    def migrate(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    config_snapshot_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id),
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    finished_at REAL
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id),
                    backend TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    started_at REAL NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    new_articles INTEGER NOT NULL,
                    saved_articles INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS site_health (
                    site TEXT PRIMARY KEY,
                    score INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    opened_at REAL NOT NULL,
                    hard_failure_times TEXT NOT NULL DEFAULT '[]',
                    open_count INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS site_locks (
                    site TEXT PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES jobs(id),
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_site_started
                    ON jobs(site, started_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_job
                    ON tasks(job_id);
                CREATE INDEX IF NOT EXISTS idx_attempts_task
                    ON attempts(task_id);
                """
            )
            self._ensure_column(
                "site_health",
                "hard_failure_times",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                "site_health",
                "open_count",
                "INTEGER NOT NULL DEFAULT 0",
            )

    def claim_job(
        self,
        *,
        site: str,
        purpose: str,
        now: float,
        lock_ttl_seconds: float,
        config_snapshot: dict[str, Any],
    ) -> int:
        snapshot_json = json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            active = self._connection.execute(
                """
                SELECT job_id, expires_at
                FROM site_locks
                WHERE site = ? AND expires_at > ?
                """,
                (site, now),
            ).fetchone()
            if active is not None:
                raise ActiveJobError(
                    f"site {site} already has active job {active['job_id']}"
                )

            cursor = self._connection.execute(
                """
                INSERT INTO jobs(site, purpose, status, started_at, config_snapshot_json)
                VALUES (?, ?, 'running', ?, ?)
                """,
                (site, purpose, now, snapshot_json),
            )
            job_id = int(cursor.lastrowid)
            self._connection.execute(
                """
                INSERT INTO site_locks(site, job_id, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(site) DO UPDATE SET
                    job_id = excluded.job_id,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                """,
                (site, job_id, now, now + lock_ttl_seconds),
            )
            return job_id

    def create_task(
        self,
        *,
        job_id: int,
        resource_type: str,
        resource_id: str,
        now: float,
    ) -> int:
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO tasks(job_id, resource_type, resource_id, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (job_id, resource_type, resource_id, now),
            )
            return int(cursor.lastrowid)

    def record_attempt(
        self,
        *,
        task_id: int,
        backend: str,
        status: str,
        started_at: float,
        duration_ms: int,
        error: str | None = None,
        new_articles: int = 0,
        saved_articles: int = 0,
    ) -> int:
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO attempts(
                    task_id, backend, status, error, started_at,
                    duration_ms, new_articles, saved_articles
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    backend,
                    status,
                    error,
                    started_at,
                    duration_ms,
                    new_articles,
                    saved_articles,
                ),
            )
            return int(cursor.lastrowid)

    def finish_task(self, task_id: int, status: str, now: float) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE tasks SET status = ?, finished_at = ? WHERE id = ?",
                (status, now, task_id),
            )

    def finish_job(self, job_id: int, status: str, now: float) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                (status, now, job_id),
            )
            self._connection.execute(
                "DELETE FROM site_locks WHERE job_id = ?",
                (job_id,),
            )

    def job(self, job_id: int) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def failed_tasks(self, job_id: int) -> list[dict[str, Any]]:
        """取一个 job 下终态为 failed 的任务，供 retry-failed 重放。

        注意：orchestrator 把 task 终态写成 succeeded / failed / skipped 三值，
        具体失败原因（blocked_waf 等）记在 attempts.status 上，不在 tasks.status 里。
        """
        rows = self._connection.execute(
            """
            SELECT id, resource_type, resource_id
            FROM tasks
            WHERE job_id = ? AND status = 'failed'
            ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def load_health(self, site: str) -> HealthSnapshot | None:
        row = self._connection.execute(
            """
            SELECT score, state, opened_at, hard_failure_times, open_count
            FROM site_health
            WHERE site = ?
            """,
            (site,),
        ).fetchone()
        if row is None:
            return None
        return HealthSnapshot(
            score=int(row["score"]),
            state=str(row["state"]),
            opened_at=float(row["opened_at"]),
            hard_failure_times=tuple(
                float(value) for value in json.loads(row["hard_failure_times"])
            ),
            open_count=int(row["open_count"]),
        )

    def attempt_times(
        self, site: str, since: float, excluded_backends: set[str]
    ) -> list[float]:
        placeholders = ",".join("?" for _ in excluded_backends)
        rows = self._connection.execute(
            f"""
            SELECT attempts.started_at
            FROM attempts
            JOIN tasks ON tasks.id = attempts.task_id
            JOIN jobs ON jobs.id = tasks.job_id
            WHERE jobs.site = ?
              AND attempts.started_at >= ?
              AND attempts.backend NOT IN ({placeholders})
            ORDER BY attempts.started_at
            """,
            (site, since, *excluded_backends),
        ).fetchall()
        return [float(row["started_at"]) for row in rows]

    def site_lock(self, site: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT site, job_id, acquired_at, expires_at
            FROM site_locks
            WHERE site = ?
            """,
            (site,),
        ).fetchone()
        return dict(row) if row is not None else None

    def save_health(
        self,
        site: str,
        score: int,
        state: str,
        opened_at: float,
        hard_failure_times: tuple[float, ...],
        now: float,
        *,
        open_count: int = 0,
    ) -> None:
        hard_failures_json = json.dumps(hard_failure_times, separators=(",", ":"))
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO site_health(
                    site, score, state, opened_at,
                    hard_failure_times, open_count, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(site) DO UPDATE SET
                    score = excluded.score,
                    state = excluded.state,
                    opened_at = excluded.opened_at,
                    hard_failure_times = excluded.hard_failure_times,
                    open_count = excluded.open_count,
                    updated_at = excluded.updated_at
                """,
                (site, score, state, opened_at, hard_failures_json, open_count, now),
            )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def stats(self, site: str | None = None) -> dict[str, Any]:
        job_where = "WHERE site = ?" if site else ""
        job_params = (site,) if site else ()
        jobs = self._connection.execute(
            f"SELECT * FROM jobs {job_where} ORDER BY id DESC LIMIT 20",
            job_params,
        ).fetchall()
        attempts_where = "WHERE jobs.site = ?" if site else ""
        attempt_params = (site,) if site else ()
        attempts = self._connection.execute(
            f"""
            SELECT
                attempts.id, jobs.site, attempts.task_id, attempts.backend,
                attempts.status, attempts.error, attempts.started_at,
                attempts.duration_ms, attempts.new_articles, attempts.saved_articles
            FROM attempts
            JOIN tasks ON tasks.id = attempts.task_id
            JOIN jobs ON jobs.id = tasks.job_id
            {attempts_where}
            ORDER BY attempts.id DESC
            LIMIT 50
            """,
            attempt_params,
        ).fetchall()

        attempt_where = attempts_where
        totals = self._connection.execute(
            """
            SELECT
                COUNT(*) AS total_attempts,
                SUM(CASE WHEN attempts.status IN
                    ('success', 'no_update', 'duplicate')
                    THEN 1 ELSE 0 END) AS successful,
                SUM(CASE WHEN attempts.status IN (
                    'blocked_waf', 'captcha_required', 'auth_expired',
                    'rate_limited', 'http_error', 'parse_error', 'network_error'
                ) THEN 1 ELSE 0 END) AS failed,
                SUM(attempts.new_articles) AS new_articles,
                SUM(attempts.saved_articles) AS saved_articles
            FROM attempts
            JOIN tasks ON tasks.id = attempts.task_id
            JOIN jobs ON jobs.id = tasks.job_id
            {attempt_where}
            """.format(attempt_where=attempt_where),
            attempt_params,
        ).fetchone()
        return {
            "jobs": [dict(row) for row in jobs],
            "attempts": [dict(row) for row in attempts],
            "totals": {
                "total_attempts": int(totals["total_attempts"] or 0),
                "successful": int(totals["successful"] or 0),
                "failed": int(totals["failed"] or 0),
                "new_articles": int(totals["new_articles"] or 0),
                "saved_articles": int(totals["saved_articles"] or 0),
            },
        }
