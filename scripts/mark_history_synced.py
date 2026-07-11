#!/usr/bin/env python3
"""把所有本地历史文章标记为已同步，之后只上传新增的"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
state_file = DATA_DIR / ".ima_raw_sync_state.json"

try:
    state = json.load(open(state_file, 'r', encoding='utf-8'))
except:
    state = {}

count = 0
for user_dir in DATA_DIR.iterdir():
    if not user_dir.is_dir() or not user_dir.name.isdigit():
        continue
    for md_file in user_dir.glob("*.md"):
        key = str(md_file.relative_to(DATA_DIR))
        if key not in state:
            stat = md_file.stat()
            state[key] = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "media_id": "history_marked",
                "upload_time": "2026-07-08T00:00:00+08:00",
                "note": "历史文章批量标记，从今日起只增量同步"
            }
            count += 1

json.dump(state, open(state_file, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f"✅ 完成：新增标记 {count} 篇历史文章，累计记录 {len(state)} 篇")
print("从现在开始，只会上传今天之后新爬取的文章")
