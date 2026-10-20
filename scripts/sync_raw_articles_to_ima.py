#!/usr/bin/env python3
"""
每日同步雪球「日报入选文章」到 IMA「雪球内容数据」知识库。

W32 2026-07-20 改造：只同步日报中分类为 🔴必读/🟡值得关注/📰市场资讯 的文章，
🔵参考（短文/无营养）不入选，未出现在日报里的也不同步。

热榜帖补齐（2026-10-20）：日报入选的文章里，热榜独有帖的作者不是自选账号，
正文只存在 `data/hot_posts/<date>/hot_top10.json`，没有 `data/<uid>/<post_id>.md`，
按 raw 路径找会**静默跳过**——当天 9 篇入选只同步了 3 篇。这里在 raw 找不到时
回落到热榜文件，按 crawler raw 的同一形状落成 .md 再走原上传链路。

- 增量同步：记录已上传文件的路径+大小+mtime，不重复上传
- 断点续传：中断后下次继续
- 非阻塞：上传失败不影响主日报流程，只打印日志
- 凌晨空跑保护：08:00 之前日报可能未生成 → 返回 0
"""

import sys
import re
import json
import shutil
import hashlib
import tempfile
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add xueqiu-analyzer-skill to path for ima_kb_uploader
# 兄弟目录推导（远程 /root/code 与本地 ~/code/claude_code 同构）；
# 目录不存在时无害——回退到 venv 里 pip install -e 的包
sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parent.parent.parent / 'xueqiu-analyzer-skill' / 'src'
    ),
)
from xueqiu_analyzer.ima_kb_uploader import upload_file

# Add scripts to path for parse_daily_report
sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_daily_report import extract_selected_articles, find_raw_article_path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
SYNC_STATE_FILE = DATA_DIR / ".ima_raw_sync_state.json"
# 热榜帖落盘的子目录（相对 data/ 的 hot_posts/<date>/ 下）：避免污染 data/<uid>/
# （那里是自选账号的爬取产物，crawler 的索引/历史/日报都按目录口径读）。
HOT_RAW_SUBDIR = "raw_md"
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
    """[DEPRECATED W32] 查找所有文章md文件（按用户ID目录存储）—— 保留兼容但不再使用"""
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


def load_hot_top_items(date_str: str, hot_dir: Path) -> dict[str, dict]:
    """读当天热榜 top10，返回 {status_id: item}。

    热榜文件是 06:00 prepare job 的产物；缺失或损坏时返回空 dict —— 同步是日报流程
    的非关键步骤，不能因为它没备好就把整步打挂。
    """
    path = hot_dir / date_str / "hot_top10.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(f"读取热榜文件失败，本轮不做热榜补位: {path} ({exc})")
        return {}
    if not isinstance(doc, dict):
        # 合法 JSON 但不是对象（`[]`/`null` 等）—— 与损坏同论
        logger.warning(f"热榜文件格式不对（顶层不是对象），本轮不做热榜补位: {path}")
        return {}
    items = doc.get("items") or []
    return {
        str(item["status_id"]): item
        for item in items
        if isinstance(item, dict) and item.get("status_id")
    }


def render_hot_post_markdown(item: dict) -> str:
    """把热榜条目渲染成与 crawler raw 同形的 .md。

    形状对齐是为了让下游 `extract_article_title` / 上传逻辑**不用分支**。
    刻意不写入任何「本轮运行时间」的时间戳：内容必须只由条目本身决定，
    否则每次运行都算「内容变了」，mtime 一变就会把同一篇重复上传一遍。
    """
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "").strip()
    return (
        f"# {title}\n"
        "\n"
        f"> 作者：{item.get('author') or ''} | 发布时间：{item.get('created_at') or ''}\n"
        f"> 点赞：{item.get('likes') or 0} | 评论：{item.get('comments') or 0}\n"
        f"> 原文链接：{item.get('url') or ''}\n"
        "> 来源：雪球热榜（全文）\n"
        "\n"
        "---\n"
        "\n"
        f"{content}\n"
    )


def hot_item_author_id(item: dict) -> str:
    """热榜条目里的作者 ID；`author_id` 可能是空的（2026-10-20 的 412809907 就是），
    这时从 url 的 `/xueqiu.com/<uid>/<pid>` 里取 —— 两者本就该一致。"""
    author_id = str(item.get("author_id") or "").strip()
    if author_id:
        return author_id
    match = re.search(r"xueqiu\.com/(\d+)/", str(item.get("url") or ""))
    return match.group(1) if match else ""


def materialize_hot_post_raw(
    date_str: str,
    user_id: str,
    post_id: str,
    hot_items: dict[str, dict],
    hot_dir: Path,
) -> Path | None:
    """把这篇热榜帖落成 raw .md 并返回路径；补不出来就返回 None（照旧算 missing）。

    只在**拿到全文**时才落盘：`content_source != 'full'` 的条目正文是首页摘要，
    直接当 raw 上传等于往知识库里塞半篇。
    """
    item = hot_items.get(str(post_id))
    if item is None:
        return None
    item_author = hot_item_author_id(item)
    if item_author and item_author != str(user_id):
        # 同一 status_id 的作者 ID 对不上，说明热榜侧口径有问题，别硬塞
        logger.warning(
            "  ⚠️ 热榜条目作者 ID 对不上，跳过: %s/%s (热榜记的是 %s)",
            user_id,
            post_id,
            item_author,
        )
        return None
    content = str(item.get("content") or "").strip()
    if item.get("content_source") != "full" or not content:
        logger.warning(
            "  ⚠️ 热榜帖只有摘要没有全文，跳过（不拿摘要冒充 raw）: %s/%s",
            user_id,
            post_id,
        )
        return None

    path = hot_dir / date_str / HOT_RAW_SUBDIR / str(user_id) / f"{post_id}.md"
    markdown = render_hot_post_markdown(item)
    try:
        # 内容没变就不重写：保持 mtime，让下文的「mtime+size 未变 → 已上传」判断生效
        if path.exists() and path.read_text(encoding="utf-8") == markdown:
            return path
    except (OSError, UnicodeDecodeError):
        # 旧文件读不出来（同日一轮被中途打断留下的半截多字节字符等）：
        # 当作「内容不一致」走下面的重写自愈，而不是让整个同步步骤崩掉
        logger.warning(f"  ⚠️ 已有热榜 raw 读不出来，覆盖重写: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        logger.warning(f"  ⚠️ 热榜帖落盘失败: {path} ({exc})")
        return None
    return path


def find_selected_article_files(
    date_str: str,
    data_dir: Path | None = None,
    report_dir: Path | None = None,
) -> tuple[list[Path], int, int, int]:
    """[W32 新增] 查找当天日报入选文章的 raw 文件路径。

    自选账号的文章在 `data/<uid>/<post_id>.md`；热榜独有帖没有这个文件，回落到
    当天的 `hot_posts/<date>/hot_top10.json` 补齐（见 materialize_hot_post_raw）。

    Returns:
        (selected_paths, total_selected, missing_files, hot_filled)
        - selected_paths: 找到（含热榜补齐）的 raw 文件路径列表
        - total_selected: 日报入选总数
        - missing_files: 入选但两条路径都拿不到的数量
        - hot_filled: 其中由热榜补齐的篇数
    """
    data_dir = DATA_DIR if data_dir is None else data_dir
    hot_dir = data_dir / "hot_posts"
    if report_dir is None:
        report_dir = data_dir / "daily_reports"
    selected = extract_selected_articles(date_str, report_dir)
    total = len(selected)
    paths = []
    missing = 0
    hot_filled = 0
    hot_items: dict[str, dict] | None = None

    for art in selected:
        path = find_raw_article_path(art["user_id"], art["post_id"], data_dir)
        if path is not None:
            paths.append(path)
            continue

        # 懒加载：只有真遇到 missing 才去读热榜文件
        if hot_items is None:
            hot_items = load_hot_top_items(date_str, hot_dir)
        hot_path = materialize_hot_post_raw(
            date_str, art["user_id"], art["post_id"], hot_items, hot_dir
        )
        if hot_path is not None:
            paths.append(hot_path)
            hot_filled += 1
            logger.info(
                "  🔥 热榜补齐 raw: %s/%s (%s)",
                art["user_id"],
                art["post_id"],
                art.get("title", "")[:50],
            )
        else:
            missing += 1
            logger.warning(
                "  ⚠️ 入选文章 raw 找不到: %s/%s (%s)",
                art["user_id"],
                art["post_id"],
                art.get("title", "")[:50],
            )
    return sorted(paths), total, missing, hot_filled


def make_state_key(date_str: str, file_path: Path, data_dir: Path | None = None) -> str:
    """[W32 新增] 生成 state key: date:user_id:post_id

    热榜补齐路径（hot_posts/<date>/raw_md/<uid>/<pid>.md）落不进 uid/pid 短式，
    用相对路径兜底 —— 只需两次生成一致（test_state_key_is_stable_for_hot_paths 守着）。
    """
    data_dir = DATA_DIR if data_dir is None else data_dir
    try:
        rel = file_path.relative_to(data_dir)
    except ValueError:
        # 不在 data_dir 下（理论上不该发生）：退回绝对路径，保证可生成、可复现
        return f"{date_str}:abs:{file_path}"
    parts = rel.parts  # ('1425236713', '400972632.md')
    if len(parts) == 2 and parts[0].isdigit():
        post_id = parts[1].replace('.md', '')
        return f"{date_str}:{parts[0]}:{post_id}"
    return f"{date_str}:{rel}"


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
    # W32 2026-07-20: 接受可选 --date 参数，默认今天
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="日报日期 YYYY-MM-DD（默认今天）")
    args = parser.parse_args()

    target_date = args.date or datetime.now().strftime("%Y-%m-%d")

    logger.info("=" * 60)
    logger.info(f"开始同步雪球日报入选文章到 IMA「{KB_NAME}」 (date={target_date})")

    # 加载已上传状态
    state = load_sync_state()
    logger.info(f"已上传记录: {len(state)} 篇")

    # 凌晨空跑保护：日报不存在 → 0 上传 0 错误返回
    daily_path = DATA_DIR / "daily_reports" / f"{target_date}.md"
    if not daily_path.exists():
        logger.info(f"⏰ 日报文件不存在 ({daily_path}) —— 凌晨空跑保护，返回 0。")
        return 0

    # W32: 改用入选文件列表（不再 find_article_files 全部）
    all_files, total_selected, missing, hot_filled = find_selected_article_files(
        target_date
    )
    logger.info(
        f"日报入选: {total_selected} 篇，找到 raw: {len(all_files)} 篇"
        f"（其中热榜补齐 {hot_filled} 篇），missing: {missing} 篇"
    )

    if not all_files:
        logger.info("✅ 没有入选文章需要同步（可能日报为空或全部 missing）")
        return 0

    # 找出待上传的：state key 不在 OR mtime/size 变了
    pending = []
    for f in all_files:
        key = make_state_key(target_date, f)
        try:
            stat = f.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except OSError as exc:
            # 静默 continue 会让这篇永远进不了知识库还没人知道（2026-10-20 收口）
            logger.warning("  ⚠️ 文件 stat 失败，本轮跳过: %s (%s)", f, exc)
            continue

        if size < 100:  # 跳过太小的空文件/短状态
            logger.warning("  ⚠️ 入选文件过小（%d 字节），不会上传到知识库: %s", size, f)
            continue

        if key in state:
            old = state[key]
            # 如果 mtime 和 size 都没变：已成功上传、或已达重试上限（永久失败）才跳过；
            # 曾失败（无 media_id）且 fail_count < MAX_RETRIES → 重新入队，跨运行重试
            # （修复：原逻辑无条件跳过，导致下游 MAX_RETRIES 重试永远不可达）
            if old.get("size") == size and abs(old.get("mtime", 0) - mtime) < 1:
                if old.get("media_id") or old.get("fail_count", 0) >= 5:
                    continue
            # 内容变了才需要重新上传

        pending.append((key, f))

    logger.info(
        f"待上传: {len(pending)} 篇（已上传: {len(all_files) - len(pending)} 篇）"
    )

    if not pending:
        logger.info("✅ 没有新文章需要同步（全部已上传）")
        return 0

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
            logger.info(
                f"[{i}/{len(pending)}] 上传: {fpath.name} → {upload_title[:50]}..."
            )
            media_id = upload_file(str(tmp_path), KB_ID, title=upload_title)
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
                "last_fail_time": None,
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
                "last_fail_time": datetime.now(
                    timezone(timedelta(hours=8))
                ).isoformat(),
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
    permanent_fail = sum(
        1 for entry in state.values() if entry.get('fail_count', 0) >= MAX_RETRIES
    )

    logger.info("=" * 60)
    logger.info(f"同步完成！成功: {success}, 失败: {fail}, 跳过(永久失败): {skip}")
    logger.info(
        f"累计已上传成功: {len([e for e in state.values() if e.get('media_id')])} 篇"
    )
    if permanent_fail > 0:
        logger.info(
            f"永久失败（已重试{MAX_RETRIES}次）: {permanent_fail} 篇，不会再自动重试"
        )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
