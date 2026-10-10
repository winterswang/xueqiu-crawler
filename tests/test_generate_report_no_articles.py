#!/usr/bin/env python3
"""回归测试：无新增文章时仍须产出日报文件。

背景（2026-10-01 事故）：
    generate_today_report 在 articles 为空时直接 return ""，不写任何文件；
    而 run_daily.sh 的后续步骤（publish_daily_report.py / push_feishu.py）
    都假定日报文件存在 -> 「今天确实没有新文章」这个**正常状态**会触发一串
    「日报文件不存在」报错，并且当天零交付。

修复后：无新增时仍复用 generate_daily_report([], []) 产出一份最小日报，
格式与正常日报一致，下游无需改动。
"""

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / 'scripts'))

from generate_report import generate_today_report


def _empty_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / 'data'
    (data_dir / 'daily_reports').mkdir(parents=True)
    (data_dir / 'index.json').write_text('{"articles": {}}', encoding='utf-8')
    return data_dir


def test_no_articles_still_writes_report_file(tmp_path):
    """核心回归：空数据也必须落盘。"""
    data_dir = _empty_data_dir(tmp_path)
    out = data_dir / 'daily_reports' / 'no-update.md'

    generate_today_report(data_dir=str(data_dir), output_path=str(out))

    assert out.exists(), '无新增时也必须产出日报文件（下游 publish/push 假定它存在）'


def test_no_articles_report_is_well_formed(tmp_path):
    """产出物必须能被下游解析：要有日报标题。"""
    data_dir = _empty_data_dir(tmp_path)
    out = data_dir / 'daily_reports' / 'no-update.md'

    generate_today_report(data_dir=str(data_dir), output_path=str(out))

    text = out.read_text(encoding='utf-8')
    assert '价值投资日报' in text
    assert '今日新增' in text


def test_existing_report_not_downgraded_to_empty(tmp_path):
    """爬2(20261009) 回归：索引被清后重跑，0 新增不得把当天已有内容的好日报
    覆盖成「无新增」空壳（叠加发布幂等还会把空版本推给 IMA）。"""
    data_dir = _empty_data_dir(tmp_path)
    out = data_dir / 'daily_reports' / 'no-update.md'
    good = '价值投资日报\n\n今日新增 3 篇：AAA、BBB、CCC\n'
    out.write_text(good, encoding='utf-8')

    text = generate_today_report(data_dir=str(data_dir), output_path=str(out))

    assert out.read_text(encoding='utf-8') == good
    assert '今日新增 3 篇' in text


def test_no_articles_does_not_call_llm(tmp_path):
    """空数据路径不应实例化分析器（避免无谓的 LLM 调用与费用）。"""
    data_dir = _empty_data_dir(tmp_path)
    out = data_dir / 'daily_reports' / 'no-update.md'

    report = generate_today_report(
        data_dir=str(data_dir), output_path=str(out), api_key='sk-should-not-be-used'
    )

    assert out.exists()
    assert report
