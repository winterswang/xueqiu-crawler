"""巡检钩子的内容断言：stats 说成功 ≠ 真抓到了东西。

`scripts/schedule-checks/check.mjs` 的 morning / eod 模式此前只看
`.last_crawl_stats.json` 与日报文件是否存在。而本项目反复发作的一类静默失败是
**正文抓成空**：stats 仍报 `successful=18, failed=0`、日报照样产出（只剩标题），
下游全绿 —— 「错误的结果和正常的结果长得一样」。

这里钉住这条断言。判定标准（2026-10-20 收口误报后）：剥掉头部元数据、
`来源：雪球App` 样板行和 `---` 分隔线之后**什么都不剩**才算故障 ——
短帖/图帖的正文就是标题本身或一两句话，长度短但**有**内容，抽取没坏；
2026-10-16/20 两次按「< 100 字符」误报的都是这类。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

CRAWLER = Path(__file__).resolve().parent.parent
CHECK = CRAWLER / "scripts" / "schedule-checks" / "check.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="没装 node，跳过（钩子是 JS）"
)

TODAY = date.today().isoformat()

_NORMAL_BODY = (
    "# 标题\n\n> 作者：张三 | 发布时间：3分钟前\n> 原文链接：https://xueqiu.com/u1/x\n\n"
    + "这是一段足够长的正文，用来模拟正常抓取。" * 6
    + "\n\n*爬取时间：2026-01-01 08:00:00*\n"
)
_EMPTY_BODY = (
    "# 标题\n\n> 作者：张三 | 发布时间：3分钟前\n> 原文链接：https://xueqiu.com/u1/x\n\n"
    "*爬取时间：2026-01-01 08:00:00*\n"
)
# 2026-10-20 的真实误报（1936609590/412907698）：只发标题、内容在配图里的帖子 ——
# 正文剥完样板后等于标题，抽取没坏。
_TITLE_ONLY_BODY = (
    "# 最近有比较重要的发现，找到跟踪AI小游戏广告流水的方法(ARR）\n\n"
    "> 作者：逸修1 | 发布时间：昨天 19:25\n> 点赞：0 | 评论：0\n"
    "> 原文链接：https://xueqiu.com/1936609590/412907698\n\n---\n\n"
    "来源：雪球App，作者： 逸修1，（https://xueqiu.com/1936609590/412907698）\n\n"
    "最近有比较重要的发现，找到跟踪AI小游戏广告流水的方法(ARR）\n\n---\n\n"
    "*爬取时间：2026-10-20 08:04:52*\n"
)
# 2026-10-16 的真实误报（1936609590/412503141）：一句话短帖 + 引用块。
_SHORT_POST_BODY = (
    "# 没毛病，确实是\n\n"
    "> 作者：逸修1 | 发布时间：昨天 21:36\n> 点赞：67 | 评论：24\n\n---\n\n"
    "来源：雪球App，作者： 逸修1，（https://xueqiu.com/1936609590/412503141）\n\n"
    "没毛病，确实是\n\n"
    "> [@某人：](/1936609590)\n>\n> 应该这样说：AI股票跌、美债收益率也下来了\n\n---\n\n"
    "*爬取时间：2026-10-16 08:06:25*\n"
)


def _fixture(tmp_path: Path, bodies: dict[str, str]) -> Path:
    """造一个最小 crawler 目录：stats + 日报 + history + 各篇 .md。"""
    root = tmp_path / "crawler"
    (root / "data" / "history" / "u1").mkdir(parents=True)
    (root / "data" / "daily_reports").mkdir(parents=True)
    (root / "data" / "u1").mkdir(parents=True)
    (root / "data" / ".last_crawl_stats.json").write_text(
        json.dumps(
            {
                "date": TODAY,
                "total_users": 1,
                "successful": 1,
                "failed": 0,
                "new_articles": len(bodies),
            }
        ),
        encoding="utf-8",
    )
    (root / "data" / "daily_reports" / f"{TODAY}.md").write_text(
        "日报", encoding="utf-8"
    )
    (root / "data" / "history" / "u1" / f"{TODAY}.json").write_text(
        json.dumps([{"article_id": aid} for aid in bodies]), encoding="utf-8"
    )
    for aid, text in bodies.items():
        (root / "data" / "u1" / f"{aid}.md").write_text(text, encoding="utf-8")
    # 热榜贴由 01:00 的 prepare job 落盘（含全文）；没有它 morning 巡检会报。
    hot_dir = root / "data" / "hot_posts" / TODAY
    hot_dir.mkdir(parents=True)
    (hot_dir / "hot_top10.json").write_text(
        json.dumps(
            {
                "items": [
                    {"status_id": "h1", "content_source": "full", "content": "全文"},
                    {"status_id": "h2", "content_source": "full", "content": "全文"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def _run(
    root: Path, mode: str = "morning", monitor: Path | None = None
) -> tuple[int, str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "XQ_CRAWLER_DIR": str(root),
        "XQ_PRECHECK_MODE": mode,
    }
    if monitor is not None:
        env["XQ_MONITOR_DIR"] = str(monitor)
    proc = subprocess.run(
        ["node", str(CHECK)],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout


def _log_run(total: int, with_posts: int, failed: int = 0, day: str = "") -> str:
    """一次运行的收尾三行，字段与线上日志同形（含 `YYYY-MM-DD HH:MM:SS` 前缀）。"""
    empty = total - with_posts - failed
    stamp = f"{day} 13:20:00 " if day else ""
    health = {
        "status": "healthy" if with_posts == total and failed == 0 else "degraded",
        "total": total,
        "success_with_posts": with_posts,
        "empty_success": empty,
        "failed": failed,
        "timeout": 0,
        "posts_coverage": round(with_posts / total, 3) if total else 0.0,
    }
    done = {
        "crawled": total - failed,
        "failed": failed,
        "total_stocks": total,
        "alerts": 0,
        "crawl_health": health,
    }
    return (
        f"{stamp}[INFO] __main__: 成功但零帖: {empty} | 超时: 0 | 失败: {failed}\n"
        f"{stamp}[INFO] __main__: Pipeline完成: {json.dumps(done)}\n"
        f"{stamp}[INFO] __main__: [SUMMARY] stocks={total}/{total} "
        f"success={total - failed} failed={failed} alerts=0 p0=0 p1=0 p2=0 "
        f"filtered=0 elapsed=100s\n"
    )


def _monitor_fixture(
    tmp_path: Path,
    groups: dict[str, list[tuple]],
    stale: tuple[str, ...] = (),
) -> Path:
    """造一个 monitor 目录：每组一份今天更新过的 pipeline 日志。

    ``groups`` 的 value 是**历次运行**的 ``(股票数, 有帖数[, 失败数[, 日期]])``
    列表，最后一轮是今天这轮。主 job 的 ``pipeline.log`` 是跨天**追加**的，
    所以这里要能写出多轮 —— 只写一轮的话，「取第一条还是最后一条」测不出来。

    ``stale`` 里的组把 mtime 往前推一天，模拟「今天还没跑」。
    """
    root = tmp_path / "monitor"
    (root / "logs").mkdir(parents=True)
    # 12:00 的 sync-watchlist 产物；缺了会被 eod 单独报一条，掩盖本组断言。
    (root / "logs" / "sync_watchlist.log").write_text("ok\n", encoding="utf-8")
    # 19:00 的三件套同理：默认造齐，只在专门测「缺产物」的用例里删掉。
    (root / "data" / "daily_reports").mkdir(parents=True)
    for suffix in ("-alerts.md", "-sentiment.md", "-summary.json"):
        (root / "data" / "daily_reports" / f"{TODAY}{suffix}").write_text(
            "ok\n", encoding="utf-8"
        )
    for name, runs in groups.items():
        p = root / "logs" / name
        blocks = []
        for run in runs:
            # 四元组自带日期（用来写昨天的运行）；否则默认今天。
            blocks.append(
                _log_run(*run) if len(run) >= 4 else _log_run(*run, day=TODAY)
            )
        p.write_text("".join(blocks), encoding="utf-8")
        if name in stale:
            yesterday = time.time() - 86400
            os.utime(p, (yesterday, yesterday))
    return root


_OK_GROUPS = {
    "pipeline.log": [(7, 7)],
    "pipeline-g1.log": [(9, 9)],
    "pipeline-g2.log": [(9, 9)],
    "pipeline-g3.log": [(8, 8)],
    "pipeline-g4.log": [(8, 8)],
    "pipeline-g5.log": [(8, 8)],
}


def test_normal_bodies_do_not_trigger_the_assertion(tmp_path):
    """正常正文不该被报 —— 误报会天天白叫醒一个 agent。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY, "a2": _NORMAL_BODY})

    code, out = _run(root)

    assert code == 2, f"应判为一切正常（exit 2），实际 {code}：{out}"
    assert "正文只剩样板" not in out


def test_title_only_post_is_not_reported(tmp_path):
    """2026-10-20 实测回归：只发标题的图帖不是抽取故障，别再叫醒 agent。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY, "a2": _TITLE_ONLY_BODY})

    code, out = _run(root)

    assert code == 2, f"标题帖不该被报（exit 2），实际 {code}：{out}"
    assert "正文只剩样板" not in out
    assert "a2" not in out


def test_short_post_is_not_reported(tmp_path):
    """2026-10-16 实测回归：一句话短帖有真实内容，不是故障。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY, "a2": _SHORT_POST_BODY})

    code, out = _run(root)

    assert code == 2, f"短帖不该被报（exit 2），实际 {code}：{out}"
    assert "正文只剩样板" not in out


def test_empty_body_is_reported(tmp_path):
    """正文剥完样板什么都不剩、stats 又全绿 —— 必须报出来，这正是断言存在的理由。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY, "a2": _EMPTY_BODY})

    code, out = _run(root)

    assert code == 0, "有问题应放行 agent（exit 0）"
    assert "今天有 1 篇正文只剩样板" in out
    assert "a2" in out
    assert "a1" not in out.split("正文只剩样板")[1]


def test_signature_only_body_is_reported(tmp_path):
    """正文只剩「来源：雪球App，作者：…」签名行 —— 历史上真实出现过的失败签名
    （见 test_title_guard 的签名行用例）。样板行带作者后缀，正则必须前缀匹配，
    不能因为行尾锚死而漏掉（PR #122 review 抓过这个空转）。"""
    signature_only = (
        "# 标题\n\n> 作者：张三 | 发布时间：3分钟前\n> 原文链接：https://xueqiu.com/u1/x\n\n"
        "---\n\n"
        "来源：雪球App，作者： 张三，（https://xueqiu.com/u1/x）\n\n---\n\n"
        "*爬取时间：2026-01-01 08:00:00*\n"
    )
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY, "a2": signature_only})

    code, out = _run(root)

    assert code == 0, "只剩签名行应放行 agent（exit 0）"
    assert "今天有 1 篇正文只剩样板" in out
    assert "a2" in out


def test_missing_hot_posts_is_reported(tmp_path):
    """01:00 的 prepare job 没跑成时，日报会**静默**少一块 —— 必须叫出来。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    (root / "data" / "hot_posts" / TODAY / "hot_top10.json").unlink()

    code, out = _run(root)

    assert code == 0
    assert "没有热榜数据" in out


def test_hot_posts_without_fulltext_is_reported(tmp_path):
    """热榜只有首页摘要（正文没抓到）时分析质量会打折，也要报。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    (root / "data" / "hot_posts" / TODAY / "hot_top10.json").write_text(
        json.dumps({"items": [{"status_id": "h1", "content_source": "excerpt"}]}),
        encoding="utf-8",
    )

    code, out = _run(root)

    assert code == 0
    assert "全是首页摘要" in out


def test_missing_body_file_is_not_double_reported(tmp_path):
    """正文文件整个缺失时，这条不管（由「今天没抓够」那类判断覆盖），别重复报。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    (root / "data" / "u1" / "a1.md").unlink()

    code, out = _run(root)

    assert "正文只剩样板" not in out


def test_body_line_mentioning_crawl_time_is_not_stripped(tmp_path):
    """剥头部必须**锚定行首**：正文里提到「爬取时间：」的整行不能被删掉。

    「正则打在全文上」正是影子的归一层刚被 review 抓出来的那类坑。
    """
    long_line = "正文里提到爬取时间：这个词的句子不该被删掉，" * 8
    root = _fixture(
        tmp_path,
        {"a1": f"# 标题\n\n> 作者：张三 | 发布时间：3分钟前\n\n{long_line}\n"},
    )

    code, out = _run(root)

    assert code == 2, f"该篇正文足够长，不该被报空：{out}"
    assert "正文只剩样板" not in out


def test_title_line_only_strips_the_first_heading(tmp_path):
    """只丢首行的 `# 标题`，正文自带的 `#` 小标题要留着。"""
    body = "\n".join(["# 小标题 " + "x" * 30] * 4)
    root = _fixture(
        tmp_path, {"a1": f"# 标题\n\n> 作者：张三 | 发布时间：3分钟前\n\n{body}\n"}
    )

    code, out = _run(root)

    assert code == 2, f"小标题算正文，不该被剥成空：{out}"


# ── pipeline 日志的零帖断言 ──
# 2026-10-28 的真实事故：opencli 的 comments + stock-notices 在 15:00–16:06 全挂，
# 爬虫静默回退 Playwright，回退也抓不到，于是 g2/g4 的 17 只股票整天没数据。
# 而 [SUMMARY] 仍写 success=9/9 failed=0 —— 巡检只看 failed=，全绿放行。


def test_all_zero_post_group_is_reported(tmp_path):
    """整组零帖但 failed=0 —— 这是「成功」和「没抓到东西」长得一样的典型。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    monitor = _monitor_fixture(tmp_path, {**_OK_GROUPS, "pipeline-g2.log": [(9, 0)]})

    code, out = _run(root, mode="eod", monitor=monitor)

    assert code == 0
    assert "pipeline-g2.log 整组零帖（9 只" in out


def test_appended_main_log_reads_the_last_run(tmp_path):
    """主 job 的 pipeline.log 是**追加**的：必须读最后一轮，不能读几天前那轮。

    2026-10-28 复现：文件里 10-23…10-28 六条 [SUMMARY]，`match`（第一次匹配）
    取到 10-23 的 healthy 记录，当天真实的零帖被整条盖掉。
    """
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    monitor = _monitor_fixture(
        tmp_path,
        {**_OK_GROUPS, "pipeline.log": [(7, 7), (7, 7), (7, 0)]},
    )

    code, out = _run(root, mode="eod", monitor=monitor)

    assert code == 0
    assert "pipeline.log 整组零帖（7 只" in out


def test_partial_zero_post_group_is_not_reported(tmp_path):
    """个别股票没帖子是正常的（冷门股、停牌），只有整组全空才算故障。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    monitor = _monitor_fixture(tmp_path, {**_OK_GROUPS, "pipeline-g3.log": [(8, 3)]})

    code, out = _run(root, mode="eod", monitor=monitor)

    assert code == 2, f"零帖 3/8 不该报：{out}"
    assert "整组零帖" not in out


def test_all_failed_group_reports_the_failure_not_a_duplicate(tmp_path):
    """8 只全失败时「有 8 只失败」已经说明了根因，不再叠一条「整组零帖」。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    monitor = _monitor_fixture(tmp_path, {**_OK_GROUPS, "pipeline-g4.log": [(8, 0, 8)]})

    code, out = _run(root, mode="eod", monitor=monitor)

    assert code == 0
    assert "pipeline-g4.log 有 8 只失败" in out
    assert "pipeline-g4.log 整组零帖" not in out


def test_all_groups_zero_post_is_one_aggregate_alarm(tmp_path):
    """六组全空 = 全站性故障，最该叫醒人 —— 但不能刷六条，也不能静默。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    all_zero = {name: [(stocks, 0)] for name, [(stocks, _)] in _OK_GROUPS.items()}
    monitor = _monitor_fixture(tmp_path, all_zero)

    code, out = _run(root, mode="eod", monitor=monitor)

    assert code == 0
    assert out.count("六组 pipeline 全部零帖") == 1
    assert "要么站点侧全挂，要么今天休市" in out


# ── 收官轮（peak.mjs）走同一套判定 ──
_PEAK = CRAWLER / "scripts" / "schedule-checks" / "peak.mjs"


def _usage_stub(root: Path, payload: dict) -> None:
    """在 fixture 里放一个假的 analyze_opencli_usage.py，让 peak.mjs 不读真台账。"""
    p = root / "scripts" / "analyze_opencli_usage.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"import json\nprint(json.dumps({payload!r}))\n", encoding="utf-8")


def _peak_run(root: Path, monitor: Path, final: bool = True) -> tuple[int, str]:
    proc = subprocess.run(
        ["node", str(_PEAK)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "XQ_CRAWLER_DIR": str(root),
            "XQ_MONITOR_DIR": str(monitor),
            "XQ_PEAK_FINAL": "1" if final else "0",
        },
    )
    return proc.returncode, proc.stdout


def test_peak_summary_reports_zero_post_group(tmp_path):
    """收官小结不能用「ok」把整组零帖盖过去 —— 这行是给人看下午结论的。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    _usage_stub(
        root,
        {
            "site_total": 51,
            "site_failed": 0,
            "local_total": 0,
            "site_peak": {"minute": "2026-10-28 18:03", "count": 7},
            "by_hour": {"2026-10-28 18:00": 51},
        },
    )
    monitor = _monitor_fixture(tmp_path, {**_OK_GROUPS, "pipeline-g2.log": [(9, 0)]})

    code, out = _peak_run(root, monitor)

    assert code == 0
    assert "pipeline-g2.log: 整组零帖 9 只" in out
    assert "pipeline.log: ok" in out


def test_peak_is_silent_when_every_group_is_healthy(tmp_path):
    """全绿时收官轮只出小结、不报问题（problem 列表为空才算正常）。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    _usage_stub(
        root,
        {
            "site_total": 51,
            "site_failed": 0,
            "local_total": 0,
            "site_peak": {"minute": "2026-10-28 18:03", "count": 7},
            "by_hour": {"2026-10-28 18:00": 51},
        },
    )
    monitor = _monitor_fixture(tmp_path, _OK_GROUPS)

    code, out = _peak_run(root, monitor)

    assert "整组零帖" not in out
    assert "pipeline.log: ok" in out


# ── 审查追加：日期绑定、残缺组、老日志回退、每小时都判 ──
_YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def test_hung_run_today_is_not_judged_by_yesterdays_summary(tmp_path):
    """今天起了头就崩、还没写收尾时，最后一条是昨天的 —— 不能当「今天跑完了」。

    主 job 的 pipeline.log 追加多天；只看「最后一条」而不看日期，
    今天崩掉的运行会被昨天的 healthy 记录顶替，巡检报全绿。
    """
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    monitor = _monitor_fixture(
        tmp_path, {**_OK_GROUPS, "pipeline-g2.log": [(9, 9, 0, _YESTERDAY)]}
    )
    # 只有昨天的收尾记录，但 mtime 是今天（进程刚起就崩，文件被这次运行写过）
    assert "没有 [SUMMARY]" in _run(root, mode="eod", monitor=monitor)[1]


def test_one_empty_group_with_others_not_run_is_not_called_sitewide(tmp_path):
    """五组没跑 + 一组零帖 ≠ 全站故障。说成「六组全部零帖」会把排查带偏。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    others = tuple(n for n in _OK_GROUPS if n != "pipeline-g2.log")
    monitor = _monitor_fixture(
        tmp_path, {**_OK_GROUPS, "pipeline-g2.log": [(9, 0)]}, stale=others
    )

    code, out = _run(root, mode="eod", monitor=monitor)

    assert "pipeline-g2.log 整组零帖" in out
    assert "六组 pipeline 全部零帖" not in out


def test_legacy_log_without_crawl_health_still_detects_zero_posts(tmp_path):
    """老日志没有 crawl_health 时走人读行回退，且要和**成功数**比而不是总数。

    8 只里 7 只超时（SUMMARY 的 failed 不含超时，success=1），剩下那 1 只也没帖子。
    回退分支若拿「零帖数 >= 股票总数」比，1 >= 8 永远不成立 —— 明明一条帖子都没有，
    却判成正常。
    """
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    monitor = _monitor_fixture(tmp_path, _OK_GROUPS)
    (monitor / "logs" / "pipeline-g4.log").write_text(
        f"{TODAY} 13:20:00 [INFO] __main__: 成功但零帖: 1 | 超时: 7 | 失败: 0\n"
        f"{TODAY} 13:20:00 [INFO] __main__: Pipeline完成: "
        f"{json.dumps({'crawled': 8, 'failed': 0, 'total_stocks': 8})}\n"
        f"{TODAY} 13:20:00 [INFO] __main__: [SUMMARY] stocks=8/8 success=1 "
        "failed=0 alerts=0 elapsed=100s\n",
        encoding="utf-8",
    )

    code, out = _run(root, mode="eod", monitor=monitor)

    assert "pipeline-g4.log 整组零帖" in out


def test_group_with_failures_is_not_reported_twice(tmp_path):
    """整组失败时「有 N 只失败」已经说清楚了，再报一条「整组零帖」是同一个根因报两遍。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    monitor = _monitor_fixture(tmp_path, {**_OK_GROUPS, "pipeline-g2.log": [(9, 0, 9)]})

    code, out = _run(root, mode="eod", monitor=monitor)

    assert "pipeline-g2.log 有 9 只失败" in out
    assert "pipeline-g2.log 整组零帖" not in out


def test_peak_hourly_round_also_checks_zero_posts(tmp_path):
    """15:00 出的问题不能等到 18:xx 收官轮才说 —— 每小时那一轮就要判。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    _usage_stub(
        root,
        {
            "site_total": 20,
            "site_failed": 0,
            "local_total": 0,
            "site_peak": {"minute": "2026-10-28 15:30", "count": 4},
            "by_hour": {"2026-10-28 15:00": 20},
        },
    )
    monitor = _monitor_fixture(tmp_path, {**_OK_GROUPS, "pipeline-g2.log": [(9, 0)]})

    code, out = _peak_run(root, monitor, final=False)

    assert code == 0
    assert "pipeline-g2.log 整组零帖" in out
    assert "【下午峰值小结】" not in out


def _healthy_usage(root: Path) -> None:
    _usage_stub(
        root,
        {
            "site_total": 20,
            "site_failed": 0,
            "local_total": 0,
            "site_peak": {"minute": "2026-10-28 15:30", "count": 4},
            "by_hour": {"2026-10-28 15:00": 20},
        },
    )


def test_peak_hourly_round_is_silent_when_healthy(tmp_path):
    """全绿的那一轮必须 exit 2 保持安静 —— 这是每小时都会走到的主路径。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    _healthy_usage(root)
    monitor = _monitor_fixture(tmp_path, _OK_GROUPS)

    code, out = _peak_run(root, monitor, final=False)

    assert code == 2, f"无异常应静默（exit 2），实际 {code}：{out}"
    assert "整组零帖" not in out


def test_peak_does_not_realert_the_same_incident_every_hour(tmp_path):
    """同一个没修好的故障，每小时叫一次就是告警疲劳，真正的故障会被一起淹掉。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    _healthy_usage(root)
    monitor = _monitor_fixture(tmp_path, {**_OK_GROUPS, "pipeline-g2.log": [(9, 0)]})

    code1, first = _peak_run(root, monitor, final=False)
    code2, second = _peak_run(root, monitor, final=False)

    assert code1 == 0 and "pipeline-g2.log 整组零帖" in first
    assert code2 == 2, f"同一故障的第二轮不该再叫醒（exit 2），实际 {code2}：{second}"
    assert "整组零帖" not in second


def test_peak_realerts_when_the_failure_set_changes(tmp_path):
    """故障范围变了（又多挂一组）要重新报 —— 去重只压重复，不压新信息。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    _healthy_usage(root)
    monitor = _monitor_fixture(tmp_path, {**_OK_GROUPS, "pipeline-g2.log": [(9, 0)]})
    _peak_run(root, monitor, final=False)

    (monitor / "logs" / "pipeline-g4.log").write_text(
        _log_run(8, 0, day=TODAY), encoding="utf-8"
    )
    code, out = _peak_run(root, monitor, final=False)

    assert code == 0
    assert "pipeline-g4.log 整组零帖" in out
    # 只报新增的那组，已报过的 g2 不再重复刷屏
    assert "pipeline-g2.log" not in out


def _touch_minutes_ago(path: Path, minutes: int) -> None:
    ts = time.time() - minutes * 60
    os.utime(path, (ts, ts))


def test_peak_reports_a_stalled_run_but_not_one_still_in_flight(tmp_path):
    """今天起了头却没写 [SUMMARY]：正在跑是常态，停太久才是故障。

    只报「卡死」不报「运行中」，否则每小时 :30 那一轮会把正常跑着的组误报成故障。
    """
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    _healthy_usage(root)
    monitor = _monitor_fixture(tmp_path, _OK_GROUPS)
    log = monitor / "logs" / "pipeline-g2.log"
    log.write_text(f"{TODAY} 16:00:00 [INFO] __main__: 开始\n", encoding="utf-8")
    _touch_minutes_ago(log, 5)

    code, out = _peak_run(root, monitor, final=False)
    assert code == 2, f"刚起 5 分钟不算卡死：{out}"

    _touch_minutes_ago(log, 40)
    code, out = _peak_run(root, monitor, final=False)
    assert code == 0
    assert "pipeline-g2.log 今天跑了但没写出 [SUMMARY]" in out


def test_peak_realerts_after_recovery_then_recurrence(tmp_path):
    """组恢复过就把它从「已报」里删掉；当天再坏要重新叫，别被早上的记录压住。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    _healthy_usage(root)
    monitor = _monitor_fixture(tmp_path, {**_OK_GROUPS, "pipeline-g2.log": [(9, 0)]})

    first = _peak_run(root, monitor, final=False)[1]
    assert "pipeline-g2.log 整组零帖" in first

    (monitor / "logs" / "pipeline-g2.log").write_text(
        _log_run(9, 9, day=TODAY), encoding="utf-8"
    )
    assert _peak_run(root, monitor, final=False)[0] == 2  # 恢复，安静

    (monitor / "logs" / "pipeline-g2.log").write_text(
        _log_run(9, 0, day=TODAY), encoding="utf-8"
    )
    code, out = _peak_run(root, monitor, final=False)
    assert code == 0
    assert "pipeline-g2.log 整组零帖" in out


def test_missing_1900_report_artifact_is_reported(tmp_path):
    """19:00 三件套没有别的门守着：health_check 不带 --require-daily-report 就不查。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    monitor = _monitor_fixture(tmp_path, _OK_GROUPS)
    (monitor / "data" / "daily_reports" / f"{TODAY}-summary.json").unlink()

    code, out = _run(root, mode="eod", monitor=monitor)

    assert code == 0
    assert "缺 19:00 产物 data/daily_reports/" in out
    assert "-summary.json" in out
