from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from crawl_gateway.config import load_sites_config
from crawl_gateway.orchestrator import Orchestrator, TaskSpec
from crawl_gateway.storage import GatewayStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crawl-gateway")
    parser.add_argument("--config", default="config/sites.yaml")
    parser.add_argument("--db", default="data/gateway.sqlite3")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run a site job")
    run_parser.add_argument("--site", required=True)
    run_parser.add_argument("--purpose", default="manual")
    run_parser.add_argument(
        "--resource",
        action="append",
        default=[],
        help="resource in type:id format; repeatable",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="persist job/tasks without visiting the site",
    )
    run_parser.add_argument(
        "--execute",
        action="store_true",
        help="experimental real access; currently xueqiu/opencli only",
    )
    run_parser.add_argument(
        "--all-accounts",
        action="store_true",
        help="create one task per enabled account",
    )
    run_parser.add_argument("--data-dir", default="data")

    stats_parser = subparsers.add_parser("stats", help="show recent jobs and totals")
    stats_parser.add_argument("--site")

    health_parser = subparsers.add_parser("health", help="show site health")
    health_parser.add_argument("--site", required=True)

    verify_parser = subparsers.add_parser(
        "verify", help="preflight checks without visiting the site"
    )
    verify_parser.add_argument("--site", required=True)
    verify_parser.add_argument("--data-dir", default="data")

    retry_parser = subparsers.add_parser(
        "retry-failed", help="replay failed tasks of a job"
    )
    retry_parser.add_argument("--job", type=int, required=True)
    retry_parser.add_argument("--data-dir", default="data")
    retry_parser.add_argument(
        "--execute",
        action="store_true",
        help="required; retries perform real site access",
    )
    return parser


def _build_execute_backend(site: str, data_dir: str):
    if site != "xueqiu":
        raise SystemExit("experimental --execute currently supports xueqiu")

    from crawl_gateway.adapters import (
        OpencliArticleClient,
        XueqiuAdapter,
        XueqiuBackend,
        XueqiuNodriverAdapter,
    )

    client = OpencliArticleClient()
    backend = XueqiuBackend(
        opencli=XueqiuAdapter(client=client, data_dir=data_dir),
        nodriver=XueqiuNodriverAdapter(data_dir=data_dir),
    )
    return client, backend


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sites = load_sites_config(args.config).sites

    if args.command == "run":
        if args.site not in sites:
            raise SystemExit(f"unknown site: {args.site}")
        if not sites[args.site].enabled:
            raise SystemExit(f"site is disabled: {args.site}")
        if args.dry_run and args.execute:
            raise SystemExit("--dry-run and --execute are mutually exclusive")
        if not args.dry_run and not args.execute:
            raise SystemExit("specify --dry-run or explicit experimental --execute")

        tasks = []
        for resource in args.resource:
            try:
                resource_type, resource_id = resource.split(":", 1)
            except ValueError as exc:
                raise SystemExit(
                    f"invalid resource {resource!r}; expected type:id"
                ) from exc
            if not resource_type or not resource_id:
                raise SystemExit(f"invalid resource {resource!r}; expected type:id")
            tasks.append(TaskSpec(resource_type, resource_id))

        if args.all_accounts:
            from crawl_gateway.adapters import load_accounts

            existing_ids = {task.resource_id for task in tasks}
            tasks.extend(
                TaskSpec("user_timeline", account_id)
                for account_id, account in load_accounts("config/accounts.yaml").items()
                if account.enabled and account_id not in existing_ids
            )

        store = GatewayStore(args.db)
        client = None
        try:
            if args.execute:
                client, backend = _build_execute_backend(args.site, args.data_dir)
            else:
                backend = None
            result = Orchestrator(
                store=store,
                site_config=sites[args.site],
                backend=backend,
            ).run(
                purpose=args.purpose,
                tasks=tasks,
                dry_run=args.dry_run,
            )
            if args.execute:
                from crawl_gateway.compatibility import export_last_crawl_stats

                export_last_crawl_stats(
                    summary=result,
                    total_tasks=len(tasks),
                    data_dir=args.data_dir,
                )
        finally:
            if client is not None:
                client.close()
            store.close()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "verify":
        if args.site not in sites:
            raise SystemExit(f"unknown site: {args.site}")
        from crawl_gateway.verify import run_verify

        store = GatewayStore(args.db)
        try:
            result = run_verify(
                site_config=sites[args.site],
                store=store,
                data_dir=args.data_dir,
            )
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ok"] else 1

    if args.command == "retry-failed":
        if not args.execute:
            raise SystemExit("retry-failed performs real access; pass --execute")
        store = GatewayStore(args.db)
        client = None
        try:
            job = store.job(args.job)
            if job is None:
                raise SystemExit(f"job not found: {args.job}")
            site = str(job["site"])
            if site not in sites:
                raise SystemExit(f"job site not in config: {site}")
            if not sites[site].enabled:
                raise SystemExit(f"site is disabled: {site}")
            failed = store.failed_tasks(args.job)
            tasks = [
                TaskSpec(str(task["resource_type"]), str(task["resource_id"]))
                for task in failed
            ]
            client, backend = _build_execute_backend(site, args.data_dir)
            # 刻意不导出 .last_crawl_stats.json：该文件是「当日整轮抓取」的聚合口径，
            # generate_report.py 用它渲染「X/Y 账号成功」。重放只是补跑少数失败账号，
            # 覆写会把日报从「9/10」篡改成看似完美的「1/1」。重放结果看 SQLite 审计即可。
            result = Orchestrator(
                store=store,
                site_config=sites[site],
                backend=backend,
            ).run(
                purpose=f"retry:{job['purpose']}",
                tasks=tasks,
            )
        finally:
            if client is not None:
                client.close()
            store.close()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    store = GatewayStore(args.db)
    try:
        if args.command == "stats":
            if args.site is not None and args.site not in sites:
                raise SystemExit(f"unknown site: {args.site}")
            result = store.stats(args.site)
        else:
            if args.site not in sites:
                raise SystemExit(f"unknown site: {args.site}")
            health = store.load_health(args.site)
            result = {
                "site": args.site,
                "enabled": sites[args.site].enabled,
                "health": asdict(health) if health is not None else None,
            }
    finally:
        store.close()

    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
