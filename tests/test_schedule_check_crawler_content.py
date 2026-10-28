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
import shutil
import subprocess
from datetime import date
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


def _log_run(total: int, with_posts: int, failed: int = 0) -> str:
    """一次运行的收尾三行，字段与线上日志同形（顺序也是线上的顺序）。"""
    empty = total - with_posts - failed
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
        f"[INFO] __main__: 成功但零帖: {empty} | 超时: 0 | 失败: {failed}\n"
        f"[INFO] __main__: Pipeline完成: {json.dumps(done)}\n"
        f"[INFO] __main__: [SUMMARY] stocks={total}/{total} success={total - failed} "
        f"failed={failed} alerts=0 p0=0 p1=0 p2=0 filtered=0 elapsed=100s\n"
    )


def _monitor_fixture(tmp_path: Path, groups: dict[str, list[tuple[int, int]]]) -> Path:
    """造一个 monitor 目录：每组一份今天更新过的 pipeline 日志。

    ``groups`` 的 value 是**历次运行**的 ``(股票数, 有帖数[, 失败数])`` 列表，
    最后一次是今天这轮。
    主 job 的 ``pipeline.log`` 是跨天**追加**的，所以这里要能写出多轮 ——
    只写一轮的话，「取第一条还是最后一条」的 bug 测不出来。
    """
    root = tmp_path / "monitor"
    (root / "logs").mkdir(parents=True)
    # 12:00 的 sync-watchlist 产物；缺了会被 eod 单独报一条，掩盖本组断言。
    (root / "logs" / "sync_watchlist.log").write_text("ok\n", encoding="utf-8")
    for name, runs in groups.items():
        (root / "logs" / name).write_text(
            "".join(_log_run(*run) for run in runs), encoding="utf-8"
        )
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


def test_timeout_without_posts_is_not_counted_as_having_posts(tmp_path):
    """超时既不是「抓到帖子」也不是零帖 —— 别拿 `零帖数 < 股票数` 当「有数据」。

    8 只里 7 只失败、1 只零帖：一组一条帖子都没有，仍应算整组空转。
    """
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY})
    monitor = _monitor_fixture(tmp_path, {**_OK_GROUPS, "pipeline-g4.log": [(8, 0, 7)]})

    code, out = _run(root, mode="eod", monitor=monitor)

    assert code == 0
    assert "pipeline-g4.log 整组零帖" in out


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


def _peak_run(root: Path, monitor: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["node", str(_PEAK)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "XQ_CRAWLER_DIR": str(root),
            "XQ_MONITOR_DIR": str(monitor),
            "XQ_PEAK_FINAL": "1",
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
