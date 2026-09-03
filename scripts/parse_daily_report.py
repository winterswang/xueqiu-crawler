#!/usr/bin/env python3
"""
雪球价值投资日报解析器 (W32 2026-07-20)

从日报 markdown 中提取"入选文章"——即分类为 🔴 必读 / 🟡 值得关注 / 📰 市场资讯 三类的文章。
🔵 参考类（短文/评论/无营养）不入选。

日报格式（参考 data/daily_reports/2026-07-20.md）：
```
### 🔴 必读
#### 1. <文章标题> $股票$ $股票$ #话题# ...

- **作者**：参一員
- **链接**：https://xueqiu.com/1425236713/400972632
- **字数**：9705 字
...

### 🟡 值得关注
（相同格式）

### 📰 市场资讯
（相同格式）

### 🔵 参考
1. [文章标题](https://xueqiu.com/{user_id}/{post_id})（作者名）
   - ⚠️ 正文为空或过短(199字符)
2. ...

## 五、今日总结
（结束）
```

API:
    from parse_daily_report import extract_selected_articles
    selected = extract_selected_articles(date="2026-07-20")
    # → [
    #     {"user_id": "1425236713", "post_id": "400972632", "title": "一场所有...", "category": "必读"},
    #     ...
    # ]
"""
from __future__ import annotations

import re
from pathlib import Path
from datetime import datetime

# 仓库根（兄弟目录部署兼容：远程 /root/code 与本地 ~/code/claude_code 同构）
PROJECT_DIR = Path(__file__).resolve().parent.parent
from typing import Optional

# 选中的分类标题（H3 级别）
SELECTED_CATEGORIES = {
    "🔴 必读": "必读",
    "🟡 值得关注": "值得关注",
    "📰 市场资讯": "市场资讯",
}

# 不入选的分类（参考类）
EXCLUDED_CATEGORIES = {
    "🔵 参考": "参考",
}

# 链接正则：xueqiu.com/{user_id}/{post_id}
XUEQIU_URL_RE = re.compile(
    r"https?://xueqiu\.com/(\d+)/(\d+)"
)

# 分类标题正则（H3：### xxx）
CATEGORY_HEADER_RE = re.compile(
    r"^###\s+(.+?)\s*$",
    re.MULTILINE,
)


def extract_selected_articles(
    date: Optional[str] = None,
    report_dir: Optional[Path] = None,
) -> list[dict]:
    """从日报 markdown 提取入选文章。

    Args:
        date: "YYYY-MM-DD" 格式，默认今天
        report_dir: 日报目录，默认 <repo>/data/daily_reports

    Returns:
        list of dict，每个含 {user_id, post_id, title, category}
        - user_id: 雪球用户 ID（str）
        - post_id: 文章 ID（str）
        - title: 文章标题（str，可能为空）
        - category: "必读" / "值得关注" / "市场资讯"

    Note:
        - 🔵 参考类文章**不**出现在返回中
        - 同一天同一篇文章只返回一次（去重）
        - 日报文件不存在 → 返回空 list
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    if report_dir is None:
        report_dir = PROJECT_DIR / "data" / "daily_reports"

    report_path = report_dir / f"{date}.md"
    if not report_path.exists():
        return []

    content = report_path.read_text(encoding="utf-8")

    # 1. 找到所有 H3 分类标题及位置
    sections = []  # [(category_name, start_pos)]
    for m in CATEGORY_HEADER_RE.finditer(content):
        title = m.group(1).strip()
        sections.append((title, m.end()))

    # 2. 给每个 section 计算 end_pos（下一个 H3 开始）
    sections_with_range = []
    for i, (title, start) in enumerate(sections):
        end = sections[i + 1][1] if i + 1 < len(sections) else len(content)
        sections_with_range.append((title, start, end))

    # 3. 提取入选 section 里的所有文章链接
    selected = []
    seen_keys = set()  # 去重 (user_id, post_id)

    for title, start, end in sections_with_range:
        # 只处理选中的分类
        category_label = None
        for prefix, label in SELECTED_CATEGORIES.items():
            if title.startswith(prefix) or prefix in title:
                category_label = label
                break

        if category_label is None:
            continue  # 跳过 🔵 参考 / 其他 section

        section_text = content[start:end]

        # 提取 section 内的所有 xueqiu.com 链接
        for m in XUEQIU_URL_RE.finditer(section_text):
            user_id, post_id = m.group(1), m.group(2)
            key = (user_id, post_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # 尝试从链接附近提取标题（链接前一行或同行的 "链接" 字段附近）
            article_title = _extract_title_near_link(section_text, m.start())

            selected.append({
                "user_id": user_id,
                "post_id": post_id,
                "title": article_title,
                "category": category_label,
            })

    return selected


def _extract_title_near_link(text: str, link_pos: int, window: int = 200) -> str:
    """尝试从链接位置附近提取文章标题。

    策略：向前找最近一行 "#### N. " 开头的标题（H4 格式，必读类用），
    或向上找 "### 🔴 必读" 之后的第一个 ####。
    """
    # 向前找 #### 行
    before = text[max(0, link_pos - window):link_pos]
    h4_match = re.search(r"####\s+\d+\.\s+(.+?)$", before, re.MULTILINE)
    if h4_match:
        return h4_match.group(1).strip()
    # 找链接前面最近的 markdown 标题
    h_match = re.search(r"#+\s+(.+?)$", before, re.MULTILINE)
    if h_match:
        return h_match.group(1).strip()
    return ""


def find_raw_article_path(
    user_id: str,
    post_id: str,
    data_dir: Optional[Path] = None,
) -> Optional[Path]:
    """根据 user_id + post_id 找 raw article 文件路径。

    Returns:
        Path if exists, else None
    """
    if data_dir is None:
        data_dir = PROJECT_DIR / "data"

    article_path = data_dir / user_id / f"{post_id}.md"
    return article_path if article_path.exists() else None


if __name__ == "__main__":
    import sys
    import json

    date = sys.argv[1] if len(sys.argv) > 1 else None
    selected = extract_selected_articles(date)
    print(f"日期: {date or 'today'}")
    print(f"入选文章数: {len(selected)}")
    for art in selected:
        path = find_raw_article_path(art["user_id"], art["post_id"])
        path_str = str(path) if path else "❌ NOT FOUND"
        print(f"  [{art['category']:6}] {art['user_id']}/{art['post_id']} → {path_str}")
