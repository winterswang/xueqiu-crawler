#!/usr/bin/env python3
"""
每日爬取完成后，将原始文章Markdown同步到IMA「雪球内容数据」知识库。
- 增量同步：记录已上传文件的路径+大小+mtime，不重复上传
- 断点续传：中断后下次继续
- 非阻塞：上传失败不影响主日报流程，只打印日志
"""
import os
import sys
import json
import shutil
import hashlib
import tempfile
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add xueqiu-analyzer-skill to path for ima_kb_uploader
sys.path.insert(0, '/root/code/xueqiu-analyzer-skill/src')
from xueqiu_analyzer.ima_kb_uploader import upload_file

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
SYNC_STATE_FILE = DATA_DIR / ".ima_raw_sync_state.json"
KB_ID = "_gi4FCt1TSGAGMETEKONaWB4jBog1i3aukvR7usNDYQ="
KB_NAME = "雪球内容数据"


def load_sync_state() -> dict:
    """加载已上传状态：{file_path: {mtime, size, media_id, upload_time, fail_count, last_error, last_fail_time}}"""
    if SYNC_STATE_FILE.exists():
        try:
            with open(SYNC_STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
                # 兼容旧格式：只有成功记录，补充失败字段
                for key in state:
                    if 'fail_count' not in state[key]:
                        state[key]['fail_count'] = 0
                return state
        except Exception as e:
            logger.warning(f"加载同步状态失败，重新开始: {e}")
    return {}


def save_sync_state(state: dict):
    """保存同步状态"""
    with open(SYNC_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_file_md5(path: Path) -> str:
    """计算文件MD5，用于判断内容是否变化"""
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def find_article_files() -> list[Path]:
    """查找所有文章md文件（按用户ID目录存储）"""
    files = []
    for user_dir in DATA_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        # 用户ID是纯数字目录名
        if not user_dir.name.isdigit():
            continue
        for md_file in user_dir.glob("*.md"):
            files.append(md_file)
    return sorted(files)


def extract_article_title(md_path: Path) -> str:
    """从md文件提取标题（第一行# 开头的标题）"""
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('# '):
                    title = line[2:].strip()
                    # 标题不能太长，截断
                    if len(title) > 80:
                        title = title[:77] + "..."
                    return title
    except Exception:
        pass
    # 提取失败用文件名
    return md_path.stem


def main():
    logger.info("=" * 60)
    logger.info(f"开始同步雪球原始文章到IMA「{KB_NAME}」")

    # 加载已上传状态
    state = load_sync_state()
    logger.info(f"已上传记录: {len(state)} 篇")

    # 查找所有文章文件
    all_files = find_article_files()
    logger.info(f"本地文章总数: {len(all_files)} 篇")

    # 找出待上传的：不在state里，或者大小/mtime/MD5变了
    pending = []
    for f in all_files:
        key = str(f.relative_to(DATA_DIR))
        try:
            stat = f.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except Exception:
            continue

        if size < 100:  # 跳过太小的空文件/短状态
            continue

        if key in state:
            old = state[key]
            # 如果mtime和size都没变，跳过
            if old.get("size") == size and abs(old.get("mtime", 0) - mtime) < 1:
                continue
            # 内容变了才需要重新上传
            # 这里简单处理，mtime变了就上传

        pending.append((key, f))

    logger.info(f"待上传: {len(pending)} 篇")

    if not pending:
        logger.info("✅ 没有新文章需要同步")
        return

    success = 0
    fail = 0
    skip = 0
    MAX_RETRIES = 5  # 最多重试5次，超过标记为永久失败
    RATE_LIMIT_DELAY = 60  # 遇到403/限频时等待60秒

    for i, (key, fpath) in enumerate(pending, 1):
        # 跳过已经重试超过最大次数的永久失败文件
        old_entry = state.get(key, {})
        if old_entry.get('fail_count', 0) >= MAX_RETRIES:
            logger.debug(f"跳过永久失败文件（已重试{MAX_RETRIES}次）: {key}")
            skip += 1
            continue

        # 提取标题：加上作者ID作为前缀方便分类
        user_id = fpath.parent.name
        title = extract_article_title(fpath)
        upload_title = f"[{user_id}] {title}"

        # 复制到临时目录（IMA需要.txt后缀识别为纯文本）
        tmp_name = fpath.name.replace('.md', '.txt')
        tmp_path = Path(tempfile.gettempdir()) / f"xueqiu_raw_{tmp_name}"
        shutil.copy2(fpath, tmp_path)

        try:
            logger.info(f"[{i}/{len(pending)}] 上传: {fpath.name} → {upload_title[:50]}...")
            media_id = upload_file(
                str(tmp_path),
                KB_ID,
                title=upload_title
            )
            logger.info(f"✅ ({media_id[:20]}...)")

            # 记录成功状态，清除失败计数
            stat = fpath.stat()
            state[key] = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "media_id": media_id,
                "upload_time": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                "fail_count": 0,
                "last_error": None,
                "last_fail_time": None
            }
            success += 1

            # 每成功1篇保存一次状态（限频容易中断，每篇都存更安全）
            save_sync_state(state)

        except Exception as e:
            err = str(e)
            err_short = err[:200]
            fail_count = old_entry.get('fail_count', 0) + 1
            logger.warning(f"❌ 失败({fail_count}/{MAX_RETRIES}): {err_short}")

            # 记录失败状态
            stat = fpath.stat()
            state[key] = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "media_id": None,
                "upload_time": None,
                "fail_count": fail_count,
                "last_error": err_short,
                "last_fail_time": datetime.now(timezone(timedelta(hours=8))).isoformat()
            }
            fail += 1
            save_sync_state(state)

            # 检测到限频/403错误，主动等待一段时间再继续，避免继续触发封禁
            if '403' in err or '限频' in err or 'rate' in err.lower() or '429' in err:
                logger.warning(f"检测到限频，等待 {RATE_LIMIT_DELAY} 秒后继续...")
                import time
                time.sleep(RATE_LIMIT_DELAY)

        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    # 保存最终状态
    save_sync_state(state)

    # 统计永久失败数量
    permanent_fail = sum(1 for entry in state.values() if entry.get('fail_count', 0) >= MAX_RETRIES)

    logger.info("=" * 60)
    logger.info(f"同步完成！成功: {success}, 失败: {fail}, 跳过(永久失败): {skip}")
    logger.info(f"累计已上传成功: {len([e for e in state.values() if e.get('media_id')])} 篇")
    if permanent_fail > 0:
        logger.info(f"永久失败（已重试{MAX_RETRIES}次）: {permanent_fail} 篇，不会再自动重试")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
