"""单测 for parse_daily_report (W32 2026-07-20)"""
import pytest
from pathlib import Path
import sys

# 把 scripts 加到 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from parse_daily_report import (
    extract_selected_articles,
    find_raw_article_path,
    SELECTED_CATEGORIES,
    EXCLUDED_CATEGORIES,
)


SAMPLE_DAILY = """# 📊 价值投资日报

**日期**：2026-07-20

---

## 一、概览

- 今日新增：6 篇

## 四、文章详情

### 🔴 必读

#### 1. 一场所有人都在赢的游戏——AI繁荣与侵吞 $NVDA$ $MSFT$

- **作者**：参一員
- **链接**：https://xueqiu.com/1425236713/400972632
- **字数**：9705 字

**核心观点：**
  1. 核心观点一

---

### 🟡 值得关注

#### 1. 港股IPO分析 $00700.HK$

- **作者**：逸修1
- **链接**：https://xueqiu.com/1936609590/401030197
- **字数**：3000 字

---

### 📰 市场资讯

#### 1. 今日盘面综述

- **作者**：永庆好公司
- **链接**：https://xueqiu.com/6865675576/401017359

---

### 🔵 参考

1. [短文1](https://xueqiu.com/1936609590/401030198)（逸修1）
2. [短文2](https://xueqiu.com/8790885129/400968431)（超级鹿鼎公）

## 五、今日总结
"""


@pytest.fixture
def tmp_daily_dir(tmp_path):
    """创建临时日报目录 + 写 sample"""
    daily_dir = tmp_path / "daily_reports"
    daily_dir.mkdir()
    (daily_dir / "2026-07-20.md").write_text(SAMPLE_DAILY, encoding="utf-8")
    return daily_dir


def test_extract_selected_includes_required_yellow_news(tmp_daily_dir):
    """入选 = 🔴必读 + 🟡值得关注 + 📰市场资讯（共 3 篇）"""
    selected = extract_selected_articles("2026-07-20", report_dir=tmp_daily_dir)
    assert len(selected) == 3, f"期望 3 篇入选，实际 {len(selected)}: {selected}"
    categories = {art["category"] for art in selected}
    assert categories == {"必读", "值得关注", "市场资讯"}


def test_extract_selected_excludes_blue_reference(tmp_daily_dir):
    """🔵 参考类（短文）不入选"""
    selected = extract_selected_articles("2026-07-20", report_dir=tmp_daily_dir)
    post_ids = {art["post_id"] for art in selected}
    assert "401030198" not in post_ids  # 短文 1
    assert "400968431" not in post_ids  # 短文 2


def test_extract_selected_returns_correct_fields(tmp_daily_dir):
    """每个入选 dict 含 user_id / post_id / title / category"""
    selected = extract_selected_articles("2026-07-20", report_dir=tmp_daily_dir)
    required_keys = {"user_id", "post_id", "title", "category"}
    for art in selected:
        assert set(art.keys()) == required_keys, f"缺字段: {art}"
        assert art["user_id"].isdigit()
        assert art["post_id"].isdigit()


def test_extract_selected_no_daily_returns_empty(tmp_path):
    """日报文件不存在 → 返回空 list"""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert extract_selected_articles("2099-01-01", report_dir=empty_dir) == []


def test_extract_selected_dedupes_duplicate_links(tmp_daily_dir):
    """同一篇文章在多个分类出现 → 只返回一次"""
    # 写一个日报，里面同一个链接在 🔴 和 🟡 都出现
    dup = SAMPLE_DAILY.replace(
        "https://xueqiu.com/1425236713/400972632",
        "https://xueqiu.com/1425236713/400972632",  # 已经在必读
    )
    # 在 🟡 section 里也加同一个链接
    dup = dup.replace(
        "https://xueqiu.com/1936609590/401030197",
        "https://xueqiu.com/1936609590/401030197\n- **链接**：https://xueqiu.com/1425236713/400972632",  # 重复
    )
    (tmp_daily_dir / "2026-07-21.md").write_text(dup, encoding="utf-8")
    selected = extract_selected_articles("2026-07-21", report_dir=tmp_daily_dir)
    user_ids = [art["user_id"] for art in selected]
    # 1425236713 应该只出现一次
    assert user_ids.count("1425236713") == 1, f"1425236713 出现 {user_ids.count('1425236713')} 次"


def test_find_raw_article_path_existing():
    """find_raw_article_path: 找真实存在的 raw"""
    data_dir = Path(__file__).resolve().parent.parent / "data"
    path = find_raw_article_path("1425236713", "400972632", data_dir=data_dir)
    assert path is not None
    assert path.exists()
    assert path.name == "400972632.md"


def test_find_raw_article_path_missing():
    """find_raw_article_path: 找不到返回 None"""
    data_dir = Path(__file__).resolve().parent.parent / "data"
    path = find_raw_article_path("9999999999", "999999999999", data_dir=data_dir)
    assert path is None


def test_categories_definitions():
    """确认 SELECTED_CATEGORIES / EXCLUDED_CATEGORIES 定义"""
    assert "🔴 必读" in SELECTED_CATEGORIES
    assert "🟡 值得关注" in SELECTED_CATEGORIES
    assert "📰 市场资讯" in SELECTED_CATEGORIES
    assert "🔵 参考" in EXCLUDED_CATEGORIES
    assert SELECTED_CATEGORIES["🔴 必读"] == "必读"


def test_real_daily_2026_07_20_excludes_blue():
    """真实日报 2026-07-20：1 🔴 + 5 🔵，只入选 1 篇"""
    real_dir = Path(__file__).resolve().parent.parent / "data" / "daily_reports"
    if not (real_dir / "2026-07-20.md").exists():
        pytest.skip("真实日报 2026-07-20.md 不存在")
    selected = extract_selected_articles("2026-07-20", report_dir=real_dir)
    # 期望：1 篇 🔴 必读（NVDA 侵吞文），0 🟡，0 📰
    assert len(selected) == 1, f"期望 1 篇入选，实际 {len(selected)}"
    assert selected[0]["category"] == "必读"
    assert selected[0]["user_id"] == "1425236713"
    assert selected[0]["post_id"] == "400972632"
