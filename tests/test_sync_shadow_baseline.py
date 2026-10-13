from __future__ import annotations

import json
from pathlib import Path

from scripts.sync_shadow_baseline import main, sync_baseline

TODAY = "2026-10-13"


def write_article(directory: Path, user_id: str, article_id: str) -> Path:
    path = directory / user_id / f"{article_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {article_id}\n\n正文\n", encoding="utf-8")
    return path


def write_index(directory: Path, entries: dict[str, dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.json").write_text(
        json.dumps({"articles": entries}, ensure_ascii=False), encoding="utf-8"
    )


def entry(user_id: str, article_id: str, crawl_time: str) -> dict:
    return {
        "article_id": article_id,
        "user_id": user_id,
        "title": f"标题 {article_id}",
        "author": "作者",
        "publish_time": "10-01 09:00",
        "crawl_time": crawl_time,
    }


def read_index(directory: Path) -> dict:
    return json.loads((directory / "index.json").read_text(encoding="utf-8"))


def legacy_with(tmp_path: Path, entries: dict[str, dict]) -> Path:
    """建一个 legacy 目录：每条索引条目都配一份正文。"""
    legacy = tmp_path / "data"
    for key, record in entries.items():
        write_article(legacy, record["user_id"], record["article_id"])
    write_index(legacy, entries)
    return legacy


def test_copies_pre_today_articles_and_merges_index(tmp_path):
    """今天之前的条目：正文复制过去、索引条目并过去。"""
    legacy = legacy_with(
        tmp_path,
        {
            "1_100": entry("1", "100", "2026-10-10T08:12:56"),
            "1_101": entry("1", "101", "2026-10-12T08:04:50"),
        },
    )
    shadow = tmp_path / "data-gateway"

    report = sync_baseline(legacy_dir=legacy, shadow_dir=shadow, today=TODAY)

    assert report.copied_articles == 2
    assert report.index_added == 2
    assert report.today_entries == 0
    assert (shadow / "1" / "100.md").exists()
    assert set(read_index(shadow)["articles"]) == {"1_100", "1_101"}


def test_today_articles_are_never_mirrored(tmp_path):
    """今天的文章一律不镜像 —— 两侧必须各自去发现，否则对比就是自证一致。

    这是本脚本最要紧的一条口径：`run_daily.sh` 那段「拿标准答案改卷」的教训
    （见其 shadow 分支注释）就是被这条守住的。
    """
    legacy = legacy_with(
        tmp_path,
        {
            "1_100": entry("1", "100", "2026-10-10T08:12:56"),
            "1_999": entry("1", "999", f"{TODAY}T08:05:00"),
        },
    )
    shadow = tmp_path / "data-gateway"

    report = sync_baseline(legacy_dir=legacy, shadow_dir=shadow, today=TODAY)

    assert report.today_entries == 1
    assert report.copied_articles == 1
    assert not (shadow / "1" / "999.md").exists()
    assert "1_999" not in read_index(shadow)["articles"]


def test_filter_uses_crawl_time_not_file_mtime(tmp_path):
    """判据是索引里的 `crawl_time` 日期，不是文件 mtime。

    跨零点跑时两者会分叉（23:59 抓的、00:01 落盘）；用 mtime 会把今天的新文
    也镜像过去，正好破坏上面那条口径。
    """
    legacy = legacy_with(
        tmp_path,
        {
            "1_100": entry("1", "100", "2026-10-12T23:59:00"),
            "1_101": entry("1", "101", f"{TODAY}T00:01:00"),
        },
    )
    shadow = tmp_path / "data-gateway"

    report = sync_baseline(legacy_dir=legacy, shadow_dir=shadow, today=TODAY)

    assert report.copied_articles == 1
    assert (shadow / "1" / "100.md").exists()
    assert not (shadow / "1" / "101.md").exists()


def test_second_run_is_idempotent(tmp_path):
    """幂等：第二遍不复制、不改索引。"""
    legacy = legacy_with(tmp_path, {"1_100": entry("1", "100", "2026-10-10T08:12:56")})
    shadow = tmp_path / "data-gateway"

    first = sync_baseline(legacy_dir=legacy, shadow_dir=shadow, today=TODAY)
    before = read_index(shadow)
    second = sync_baseline(legacy_dir=legacy, shadow_dir=shadow, today=TODAY)

    assert first.index_added == 1 and first.copied_articles == 1
    assert second.index_added == 0 and second.copied_articles == 0
    assert second.already_present == 1 and second.index_kept == 1
    assert read_index(shadow) == before


def test_existing_shadow_entry_is_kept_not_overwritten(tmp_path):
    """影子侧已有的索引条目保留 —— 它记的是影子自己抓到的 crawl_time。"""
    legacy = legacy_with(tmp_path, {"1_100": entry("1", "100", "2026-10-10T08:12:56")})
    shadow = tmp_path / "data-gateway"
    shadow.mkdir()
    own = entry("1", "100", "2026-10-11T09:00:00")
    own["title"] = "影子自己抓到的标题"
    write_index(shadow, {"1_100": own})

    report = sync_baseline(legacy_dir=legacy, shadow_dir=shadow, today=TODAY)

    assert report.index_kept == 1 and report.index_added == 0
    assert read_index(shadow)["articles"]["1_100"]["title"] == "影子自己抓到的标题"


def test_history_before_today_copied_today_skipped(tmp_path):
    """history 是已知集合的第三个来源：只补「今天之前」的日期文件。"""
    legacy = legacy_with(tmp_path, {"1_100": entry("1", "100", "2026-10-10T08:12:56")})
    history_dir = legacy / "history" / "1"
    history_dir.mkdir(parents=True)
    (history_dir / "2026-10-10.json").write_text("{}", encoding="utf-8")
    (history_dir / f"{TODAY}.json").write_text("{}", encoding="utf-8")
    shadow = tmp_path / "data-gateway"

    report = sync_baseline(legacy_dir=legacy, shadow_dir=shadow, today=TODAY)

    assert report.copied_history == 1
    assert (shadow / "history" / "1" / "2026-10-10.json").exists()
    assert not (shadow / "history" / "1" / f"{TODAY}.json").exists()


def test_missing_master_file_is_reported_not_faked(tmp_path):
    """索引有、正文没了（曾因 OOM 丢过）：如实报出来，不假装成功。"""
    legacy = tmp_path / "data"
    write_index(legacy, {"1_100": entry("1", "100", "2026-10-10T08:12:56")})
    shadow = tmp_path / "data-gateway"

    report = sync_baseline(legacy_dir=legacy, shadow_dir=shadow, today=TODAY)

    assert report.missing_files == ["1_100"]
    assert report.copied_articles == 0
    assert "⚠️ 索引有但正文缺失 1 篇" in report.summary_line()


def test_unparsable_crawl_time_is_skipped_not_guessed(tmp_path):
    """`crawl_time` 缺失/无法解析 → 保守跳过（宁可欠账，也不镜像今天的新文）。"""
    legacy = legacy_with(tmp_path, {"1_100": entry("1", "100", "2026-10-10T08:12:56")})
    broken = entry("1", "101", "")
    write_article(legacy, "1", "101")
    data = read_index(legacy)
    data["articles"]["1_101"] = broken
    write_index(legacy, data["articles"])
    shadow = tmp_path / "data-gateway"

    report = sync_baseline(legacy_dir=legacy, shadow_dir=shadow, today=TODAY)

    assert report.undecidable == ["1_101"]
    assert report.copied_articles == 1
    assert not (shadow / "1" / "101.md").exists()


def test_dry_run_writes_nothing(tmp_path):
    legacy = legacy_with(tmp_path, {"1_100": entry("1", "100", "2026-10-10T08:12:56")})
    shadow = tmp_path / "data-gateway"

    report = sync_baseline(
        legacy_dir=legacy, shadow_dir=shadow, today=TODAY, dry_run=True
    )

    assert report.copied_articles == 1 and report.index_added == 1
    assert not shadow.exists()  # 一个文件都不该落


def test_main_exits_2_when_legacy_index_missing(tmp_path, capsys):
    """legacy 侧没有索引时必须失败退出 —— 静默放行 = 影子带着错误基线去抓。"""
    code = main(
        [
            "--legacy-dir",
            str(tmp_path / "data"),
            "--shadow-dir",
            str(tmp_path / "data-gateway"),
            "--date",
            TODAY,
        ]
    )

    assert code == 2
    assert "索引不存在" in capsys.readouterr().err
