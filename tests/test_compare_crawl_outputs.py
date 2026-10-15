from __future__ import annotations

import json
from pathlib import Path

from scripts.compare_crawl_outputs import build_report, main

DAY = "2026-10-07"


def write_stats(data_dir: Path, **overrides) -> None:
    payload = {
        "date": DAY,
        "total_users": 3,
        "successful": 3,
        "failed": 0,
        "new_articles": 5,
    }
    payload.update(overrides)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".last_crawl_stats.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def write_index(data_dir: Path, records: dict[str, dict]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "index.json").write_text(
        json.dumps({"articles": records, "last_update": None, "history": {}}),
        encoding="utf-8",
    )


def write_history(
    data_dir: Path, user_id: str, article_ids: list[str], day: str = DAY
) -> None:
    history_dir = data_dir / "history" / user_id
    history_dir.mkdir(parents=True, exist_ok=True)
    (history_dir / f"{day}.json").write_text(
        json.dumps(
            {
                "date": day,
                "user_id": user_id,
                "article_count": len(article_ids),
                "articles": [{"article_id": aid} for aid in article_ids],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def record(user_id: str, article_id: str, **overrides) -> tuple[str, dict]:
    payload = {
        "article_id": article_id,
        "user_id": user_id,
        "title": f"标题-{article_id}",
        "author": "张三",
        "publish_time": "2026-10-07 08:00",
        "crawl_time": "2026-10-07T09:00:00",
        "filepath": f"/abs/{user_id}/{article_id}.md",
    }
    payload.update(overrides)
    return f"{user_id}_{article_id}", payload


def aligned_pair(tmp_path: Path) -> tuple[Path, Path]:
    """构造两侧完全一致的产物（含一篇历史文章做基线）。"""
    legacy = tmp_path / "data"
    gateway = tmp_path / "data-gateway"
    for data_dir in (legacy, gateway):
        write_stats(data_dir)
        write_index(
            data_dir,
            dict([record("u1", "old1"), record("u1", "new1")]),
        )
        write_history(data_dir, "u1", ["new1"])
    return legacy, gateway


def test_identical_dirs_report_no_difference(tmp_path):
    legacy, gateway = aligned_pair(tmp_path)

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["ok"] is True
    assert report["differences"] == []
    assert report["articles"]["only_in_legacy"] == []
    assert report["articles"]["only_in_gateway"] == []


def test_article_only_on_one_side_is_difference(tmp_path):
    legacy, gateway = aligned_pair(tmp_path)
    write_index(
        gateway,
        dict([record("u1", "old1"), record("u1", "new1"), record("u1", "new2")]),
    )
    write_history(gateway, "u1", ["new1", "new2"])

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["ok"] is False
    assert report["articles"]["only_in_gateway"] == [
        {"user_id": "u1", "article_id": "new2"}
    ]
    assert any("仅 gateway" in line for line in report["differences"])


def test_stats_field_mismatch_is_difference(tmp_path):
    legacy, gateway = aligned_pair(tmp_path)
    write_stats(gateway, successful=2, failed=1)

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["ok"] is False
    assert report["stats"]["diff"]["successful"] == [3, 2]
    assert report["stats"]["diff"]["failed"] == [0, 1]
    assert report["failures"]["legacy_failed"] == 0
    assert report["failures"]["gateway_failed"] == 1


def test_field_mismatch_on_shared_article_is_reported(tmp_path):
    legacy, gateway = aligned_pair(tmp_path)
    write_index(
        gateway,
        dict([record("u1", "old1"), record("u1", "new1", title="标题变了")]),
    )

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["ok"] is False
    assert report["articles"]["field_mismatches"] == [
        {
            "user_id": "u1",
            "article_id": "new1",
            "field": "title",
            "legacy": "标题-new1",
            "gateway": "标题变了",
        }
    ]


def test_crawl_time_and_filepath_differences_are_ignored(tmp_path):
    """crawl_time / filepath 两侧必然不同，不能算差异。"""
    legacy, gateway = aligned_pair(tmp_path)
    write_index(
        gateway,
        dict(
            [
                record(
                    "u1",
                    "old1",
                    crawl_time="2026-10-07T09:05:00",
                    filepath="/other/u1/old1.md",
                ),
                record(
                    "u1",
                    "new1",
                    crawl_time="2026-10-07T09:07:00",
                    filepath="/other/u1/new1.md",
                ),
            ]
        ),
    )

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["ok"] is True
    assert report["articles"]["field_mismatches"] == []


def test_unaligned_shadow_dir_is_reported(tmp_path):
    """影子目录没和 data/ 对齐时，「新文章」口径不可比，必须显式报出来。"""
    legacy, gateway = aligned_pair(tmp_path)
    # gateway 侧缺少历史基线文章 old1（模拟影子目录未初始化）
    write_index(gateway, dict([record("u1", "new1")]))

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["ok"] is False
    assert report["articles"]["baseline_only_in_legacy"] == ["u1_old1"]
    assert any("基线不一致" in line for line in report["differences"])


def test_missing_today_history_does_not_report_baseline_misalignment(tmp_path):
    """一侧漏记当天新文章时，index 里那条不能被误判成基线不一致。

    基线扣减必须用两侧当天 id 的并集；各用各的集合会把这一条算进基线，
    报出与事实不符的「影子目录未与 data/ 对齐」。
    """
    legacy, gateway = aligned_pair(tmp_path)
    # gateway 的 index 仍有这篇文章（确实保存了），只是没写当天 history 快照
    (gateway / "history" / "u1" / f"{DAY}.json").unlink()

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["ok"] is False
    assert report["articles"]["only_in_legacy"] == [
        {"user_id": "u1", "article_id": "new1"}
    ]
    assert report["articles"]["baseline_only_in_legacy"] == []
    assert report["articles"]["baseline_only_in_gateway"] == []
    assert not any("基线不一致" in line for line in report["differences"])


def test_vacuous_comparison_is_warned(tmp_path):
    """两侧当天都没新增时，无差异是空话，必须显式警告。

    2026-10-08 实测教训：影子目录的种子顺序反了（legacy 先跑、种子后做），
    gateway 因此拿到被拷进去的「标准答案」，文章层显示完美一致 —— 而那
    根本没验证任何东西。这条警告就是为了让这种空转不至于伪装成一次有效验证。
    """
    legacy = tmp_path / "data"
    gateway = tmp_path / "data-gateway"
    for data_dir in (legacy, gateway):
        write_stats(data_dir, total_users=3, successful=3, new_articles=0)
        write_index(data_dir, dict([record("u1", "old1")]))
        write_history(data_dir, "u1", [])  # 当天无新增

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["ok"] is True  # 空转不是「差异」，不判失败
    assert len(report["warnings"]) == 1
    assert "未覆盖" in report["warnings"][0]


def test_no_warning_when_articles_were_actually_compared(tmp_path):
    legacy, gateway = aligned_pair(tmp_path)

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["articles"]["legacy_new_total"] == 1
    assert report["warnings"] == []


def test_main_exit_codes_and_report_file(tmp_path, capsys):
    legacy, gateway = aligned_pair(tmp_path)
    out = tmp_path / "logs" / f"shadow_compare_{DAY}.json"

    exit_code = main(
        [
            "--legacy-dir",
            str(legacy),
            "--gateway-dir",
            str(gateway),
            "--date",
            DAY,
            "--out",
            str(out),
        ]
    )

    assert exit_code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is True
    capsys.readouterr()

    write_stats(gateway, failed=1)
    exit_code = main(
        [
            "--legacy-dir",
            str(legacy),
            "--gateway-dir",
            str(gateway),
            "--date",
            DAY,
            "--out",
            str(out),
        ]
    )

    assert exit_code == 1
    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is False


def test_missing_index_on_both_sides_exits_2(tmp_path, capsys):
    empty = tmp_path / "nothing"
    empty.mkdir(parents=True)

    exit_code = main(
        [
            "--legacy-dir",
            str(empty),
            "--gateway-dir",
            str(empty / "also-missing"),
            "--date",
            DAY,
            "--out",
            str(tmp_path / "out.json"),
        ]
    )

    assert exit_code == 2
    assert "影子目录没有接上" in capsys.readouterr().err


def test_gateway_attempt_statuses_are_scoped_to_the_day(tmp_path):
    """只统计当天的 job —— 历史运行不能累加进来.

    回归（2026-10-09 线上）：原来不过滤日期，把影子库里**所有**历史尝试加在一起。
    今早那轮 21 次被报成 57 次，还混进前一天的 8 条 `circuit_open`，直接读出了
    「今早熔断跳了 8 个账号」这种不存在的因果关系。
    """
    import datetime
    import sqlite3

    legacy, gateway = aligned_pair(tmp_path)
    db_path = tmp_path / "gateway.sqlite3"

    def at(day: str) -> float:
        return datetime.datetime.fromisoformat(f"{day}T08:00:00").timestamp()

    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, started_at REAL);
            CREATE TABLE tasks (id INTEGER PRIMARY KEY, job_id INTEGER);
            CREATE TABLE attempts (task_id INTEGER, status TEXT);
            """
        )
        connection.execute("INSERT INTO jobs VALUES (1, ?)", (at("2026-10-06"),))
        connection.execute("INSERT INTO jobs VALUES (2, ?)", (at(DAY),))
        connection.execute("INSERT INTO tasks VALUES (10, 1), (20, 2)")
        connection.executemany(
            "INSERT INTO attempts VALUES (?, ?)",
            [
                (10, "circuit_open"),  # 昨天的 job —— 不该计入
                (20, "success"),
                (20, "success"),
                (20, "blocked_waf"),
            ],
        )

    report = build_report(
        legacy_dir=legacy, gateway_dir=gateway, day=DAY, gateway_db=db_path
    )

    assert report["failures"]["gateway_attempt_statuses"] == {
        "success": 2,
        "blocked_waf": 1,
    }


# ---------- 正文比对（2026-10-15 补：此前**根本没比正文**）----------
#
# `COMPARED_FIELDS` 只看 index 里的元数据，抓到的正文一个字都没验 —— 而正文才是
# 抓取的产出。这两天 20 篇正文其实逐字一致，但那是**人工比出来的**；不补这一层，
# 将来某篇抓成空/截断，影子会照样报绿。


def write_body(data_dir: Path, user_id: str, article_id: str, text: str) -> None:
    path = data_dir / user_id
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{article_id}.md").write_text(text, encoding="utf-8")


BODY = """# 标题

> 作者：张三 | 发布时间：3分钟前
> 原文链接：https://xueqiu.com/u1/new1

正文第一段。

> 昨天 14:09 · 转发 4 · 讨论 57 · 赞 116

*爬取时间：2026-10-07 08:13:54*
"""


def test_identical_bodies_report_no_difference(tmp_path):
    """两侧正文里的**必然不同**的三处（相对发布时间、爬取时刻、实时计数）不该算差异。"""
    legacy, gateway = aligned_pair(tmp_path)
    write_body(legacy, "u1", "new1", BODY)
    write_body(
        gateway,
        "u1",
        "new1",
        BODY.replace("发布时间：3分钟前", "发布时间：2小时前")
        .replace("讨论 57", "讨论 56")
        .replace("赞 116", "赞 118")
        .replace("2026-10-07 08:13:54", "2026-10-07 09:43:05"),
    )

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["articles"]["bodies_compared"] == 1
    assert report["articles"]["body_mismatches"] == []
    assert report["differences"] == []
    assert report["ok"] is True


def test_differing_body_content_is_reported(tmp_path):
    """正文真的不一样时必须报出来 —— 这是这一层存在的唯一理由。"""
    legacy, gateway = aligned_pair(tmp_path)
    write_body(legacy, "u1", "new1", BODY)
    write_body(gateway, "u1", "new1", BODY.replace("正文第一段。", ""))

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    mismatches = report["articles"]["body_mismatches"]
    assert len(mismatches) == 1
    assert mismatches[0]["article_id"] == "new1"
    assert mismatches[0]["legacy_chars"] > mismatches[0]["gateway_chars"]
    assert any("正文不一致" in d for d in report["differences"])
    assert report["ok"] is False


def test_body_present_on_only_one_side_is_reported(tmp_path):
    legacy, gateway = aligned_pair(tmp_path)
    write_body(legacy, "u1", "new1", BODY)

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["articles"]["body_mismatches"] == [
        {
            "user_id": "u1",
            "article_id": "new1",
            "reason": "一侧没有正文文件",
            "legacy_exists": True,
            "gateway_exists": False,
        }
    ]
    assert report["ok"] is False


def test_bodies_absent_on_both_sides_are_not_a_difference(tmp_path):
    """两侧都没正文文件 = 在这一点上一致（既有夹具就是这种形态，不能因此报差异）。"""
    legacy, gateway = aligned_pair(tmp_path)

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["articles"]["bodies_compared"] == 1
    assert report["articles"]["body_mismatches"] == []
    assert report["ok"] is True


def test_relative_publish_time_is_no_longer_compared_in_index(tmp_path):
    """`publish_time` 是相对时间字符串，两侧相隔 1.5h 跑必然不同 —— 已移出比较字段。"""
    from scripts.compare_crawl_outputs import COMPARED_FIELDS

    assert "publish_time" not in COMPARED_FIELDS

    legacy, gateway = aligned_pair(tmp_path)
    write_index(
        gateway,
        dict(
            [
                record("u1", "old1"),
                record("u1", "new1", publish_time="2026-10-07 09:30"),
            ]
        ),
    )

    report = build_report(legacy_dir=legacy, gateway_dir=gateway, day=DAY)

    assert report["articles"]["field_mismatches"] == []
    assert report["ok"] is True
