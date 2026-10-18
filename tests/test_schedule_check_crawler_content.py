"""巡检钩子的内容断言：stats 说成功 ≠ 真抓到了东西。

`scripts/schedule-checks/check.mjs` 的 morning / eod 模式此前只看
`.last_crawl_stats.json` 与日报文件是否存在。而本项目反复发作的一类静默失败是
**正文抓成空**：stats 仍报 `successful=18, failed=0`、日报照样产出（只剩标题），
下游全绿 —— 「错误的结果和正常的结果长得一样」。

这里钉住新加的那条断言（正文 < 100 字符即报警）。阈值依据：2026-10-08–15
全量样本里正常正文最短 150 字符，空抽取约 0–35 字符，100 落在两者中间。
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


def _run(root: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["node", str(CHECK)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "XQ_CRAWLER_DIR": str(root),
            "XQ_PRECHECK_MODE": "morning",
        },
    )
    return proc.returncode, proc.stdout


def test_normal_bodies_do_not_trigger_the_assertion(tmp_path):
    """正常正文（≥100 字符）不该被报 —— 误报会天天白叫醒一个 agent。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY, "a2": _NORMAL_BODY})

    code, out = _run(root)

    assert code == 2, f"应判为一切正常（exit 2），实际 {code}：{out}"
    assert "正文是空的" not in out


def test_empty_body_is_reported(tmp_path):
    """正文空掉但 stats 全绿时，必须报出来 —— 这正是这条断言存在的理由。"""
    root = _fixture(tmp_path, {"a1": _NORMAL_BODY, "a2": _EMPTY_BODY})

    code, out = _run(root)

    assert code == 0, "有问题应放行 agent（exit 0）"
    assert "今天有 1 篇正文是空的" in out
    assert "a2" in out
    assert "a1" not in out.split("正文是空的")[1]


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

    assert "正文是空的" not in out


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
    assert "正文是空的" not in out


def test_title_line_only_strips_the_first_heading(tmp_path):
    """只丢首行的 `# 标题`，正文自带的 `#` 小标题要留着。"""
    body = "\n".join(["# 小标题 " + "x" * 30] * 4)
    root = _fixture(
        tmp_path, {"a1": f"# 标题\n\n> 作者：张三 | 发布时间：3分钟前\n\n{body}\n"}
    )

    code, out = _run(root)

    assert code == 2, f"小标题算正文，不该被剥成空：{out}"
