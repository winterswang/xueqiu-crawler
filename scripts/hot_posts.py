#!/usr/bin/env python3
"""全网热门贴抓取 + 打分 —— 早报融合分析的热贴数据源.

产出 data/hot_posts/<date>/hot_top10.json，由 generate_report.py 读入后与自选账号
文章按文章 ID 去重合并，走同一套 LLM 分析与优先级分桶（不再单独成段）。

数据源：opencli xueqiu hot（DOM 适配器，opencli-adapters#8/#9，依赖登录态）。
口径：**滚动窗口**——只收「抓取时刻往前 window_hours 小时」内发布的帖子
（默认 24h，Asia/Shanghai）。不按日历日切：几点跑窗口就自动跟到几点，
早报 8 点跑即「昨早 8 点 → 现在」。跨过零点发布的条目在报告里标「今晨」。

打分（0-100，权重见 config.yaml hot_posts.weights）：
  rank        雪球「热门」榜位次（站点 smart 排序的置信度）
  freshness   exp(-小时龄 / 半衰期)
  engagement  log1p(赞 + 2*评 + 3*转) / log1p(批次 P95)
  watchlist   正文 $股票(代码)$ 与自选股（opencli xueqiu watchlist）交集
  followed    作者在 config/accounts.yaml 关注列表
  depth       长文加分、短贴降权（以纯文本长度近似内容深度）
连续霸榜降权：命中昨日 top10 的条目总分 × repeat_decay。

失败契约：任何一步失败都落盘 data/.last_hot_stats.json（ok=false）并以非 0
退出 —— run_daily.sh 里 `|| echo` 兜底，日报降级为纯自选账号文章，绝不阻塞主流程
（同 sync_raw_articles_to_ima.py 的模式）。20:30 巡检可读该 stats 文件断言。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
# 同目录的 title_guard / opencli_extractor 也要能 import —— 被 tests 以
# `scripts.hot_posts` 形式导入时，脚本目录不会自动进 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402
from xueqiu_analyzer.opencli_call_logger import record_opencli_call  # noqa: E402
from xueqiu_analyzer.opencli_cooldown import in_cooldown  # noqa: E402
from xueqiu_analyzer.opencli_rate_limiter import acquire_opencli_slot  # noqa: E402

logger = logging.getLogger(__name__)

TZ = ZoneInfo('Asia/Shanghai')
OPENCLI_TIMEOUT_SECONDS = 120

# 让子进程（opencli / 插件）能归因调用方，同 opencli_extractor 的做法。
os.environ.setdefault('XUEQIU_CALL_SOURCE', 'xueqiu-crawler:hot_posts')

DEFAULT_CONFIG = {
    'enabled': True,
    'limit': 60,
    'scrolls': 5,
    'window_hours': 24,
    'top_n': 10,
    'freshness_half_life_hours': 36.0,
    'repeat_decay': 0.9,
    'prefetch_max': 50,
    # 权重表之外的一次性扣分（负分）
    'penalties': {'pnl_showoff': 20},
    # 抓全文的时间预算。给足 15 分钟，避免「排在后面的条目拿不到全文」
    # 导致同一批候选里有的按全文、有的按摘要评分（不可比）。
    'fulltext_budget_seconds': 900.0,
    # 相邻详情页抓取的最小间隔（秒），0 = 不额外限速。设 15 的理由见
    # HOT_FULLTEXT_INTERVAL_SECONDS 的注释（详情页令牌桶，5 篇/分必撞墙）。
    'fulltext_interval_seconds': 0.0,
    # 内容深度分档（按**全文**字数）。全文实测 504-6922、中位 1235，
    # 旧的 500/200 是按摘要（≤300 字）设计的，用在全文上会 10 条全满分。
    'depth_chars': {'deep': 3000, 'medium': 1500, 'short': 800},
    'weights': {
        'rank': 30,
        'freshness': 20,
        'engagement': 20,
        'watchlist': 15,
        'followed': 10,
        'depth': 5,
    },
}

# $中国平安(SH601318)$ / $腾讯控股(00700)$ → 括号里的代码
_SYMBOL_RE = re.compile(r'\$[^$\s][^$]*?\(([A-Z]{0,4}[0-9A-Z.]+)\)\$')

# 「晒收益 / 账户流水」特征：只认**具体金额**（万）或明确的盈亏表述。
# 产品口径（用户 2026-10-18）：这类内容对投研没用，热榜里降到最低。
# 必须避开两类误伤：
#   - 公司业绩分析（「台积电三季度营收 467.2 亿美元、同比增长 41.2%」）
#   - 纯持仓记录（「招商银行：持股 22300 股，不变，每股成本 36.075」）
_PNL_PATTERNS = (
    re.compile(r'(赚|挣|盈利)了?[^。！？\n]{0,12}?\d+(?:\.\d+)?\s*万'),
    re.compile(r'(累计|总共|一共)[^。！？\n]{0,8}?(收益|盈利|赚)[^。！？\n]{0,10}?\d'),
    re.compile(r'(回撤|亏了|赔了)[^。！？\n]{0,8}?\d+(?:\.\d+)?\s*万'),
    re.compile(r'账户[^。！？\n]{0,12}?(翻倍|翻了[一两三四五六七八九十几\d]+倍)'),
)


def looks_like_pnl_showoff(text: str) -> bool:
    """是不是「晒自己账户赚了多少/亏了多少」类帖子（用于热榜降权）。"""
    return any(pattern.search(text or '') for pattern in _PNL_PATTERNS)


def load_hot_config(config_path: Path | None = None) -> dict:
    """读 config.yaml 的 hot_posts 段，缺省项用 DEFAULT_CONFIG 补齐。"""
    path = config_path or PROJECT_ROOT / 'config' / 'config.yaml'
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝，防调用方改到默认值
    try:
        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        section = data.get('hot_posts') or {}
        if isinstance(section, dict):
            weights = dict(cfg['weights'])
            weights.update(section.get('weights') or {})
            cfg.update(section)
            cfg['weights'] = weights
    except OSError:
        logger.warning('读取 %s 失败，hot_posts 用默认配置', path)
    return cfg


def _run_opencli(args: list[str], timeout: int = OPENCLI_TIMEOUT_SECONDS) -> str:
    """带限频槽位 / 冷却检查 / 台账记录地跑一次 opencli，返回 stdout。"""
    if in_cooldown():
        raise RuntimeError('opencli 熔断冷却中，跳过热门贴抓取')
    cmd = ['opencli'] + args
    source = os.environ.get('XUEQIU_CALL_SOURCE', 'xueqiu-crawler:hot_posts')
    slot = acquire_opencli_slot(' '.join(args[:2]), cmd)
    started_at = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        record_opencli_call(
            cmd,
            started_at,
            error=exc,
            source=source,
            caller='hot_posts',
            throttle=slot,
        )
        raise
    record_opencli_call(
        cmd,
        started_at,
        result=result,
        source=source,
        caller='hot_posts',
        throttle=slot,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"opencli {' '.join(args)} 失败: {result.stderr.strip()[:200]}"
        )
    return result.stdout


def _parse_json_array(stdout: str, what: str) -> list:
    """opencli 的升级提示可能打印在 JSON 之后，取第一个 '[' 起的片段。"""
    start = stdout.find('[')
    if start == -1:
        raise RuntimeError(f'{what} 输出里没有 JSON 数组')
    data = json.loads(stdout[start:])
    if not isinstance(data, list):
        raise RuntimeError(f'{what} 输出不是 JSON 数组')
    return data


def fetch_hot_posts(cfg: dict) -> list[dict]:
    """拉热门贴原始列表。"""
    stdout = _run_opencli(
        [
            'xueqiu',
            'hot',
            '--limit',
            str(cfg['limit']),
            '--scrolls',
            str(cfg['scrolls']),
            '-f',
            'json',
        ],
    )
    return _parse_json_array(stdout, 'opencli xueqiu hot')


def fetch_watchlist_codes(data_dir: Path, now: datetime) -> list[str]:
    """自选股代码列表，按天缓存（每天至多打一次 watchlist 接口）。"""
    cache = data_dir / 'hot_posts' / 'watchlist_cache.json'
    if cache.exists():
        try:
            doc = json.loads(cache.read_text(encoding='utf-8'))
            if doc.get('date') == now.strftime('%Y-%m-%d'):
                return list(doc.get('codes') or [])
        except (json.JSONDecodeError, OSError):
            pass
    stdout = _run_opencli(['xueqiu', 'watchlist', '-f', 'json'])
    rows = _parse_json_array(stdout, 'opencli xueqiu watchlist')
    codes = sorted(
        {
            str(row.get('symbol', '')).strip()
            for row in rows
            if isinstance(row, dict) and row.get('symbol')
        }
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {'date': now.strftime('%Y-%m-%d'), 'codes': codes},
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    return codes


def load_followed_author_ids(accounts_path: Path | None = None) -> set[str]:
    """accounts.yaml 的关注账号 id 集合（作者相关性加分用）。"""
    path = accounts_path or PROJECT_ROOT / 'config' / 'accounts.yaml'
    try:
        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        return {
            str(acc.get('id'))
            for acc in (data.get('accounts') or [])
            if isinstance(acc, dict) and acc.get('id')
        }
    except OSError:
        logger.warning('读取 %s 失败，关注作者加分不可用', path)
        return set()


def parse_created_at(value) -> datetime | None:
    """适配器输出的 ISO 时间 → aware datetime（Asia/Shanghai）。失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def filter_window(items: list[dict], now: datetime, cfg: dict):
    """滚动窗口过滤 [now - window_hours, now]。

    返回 (kept, dropped_counts)；解析失败的条目直接丢弃。kept 条目带
    _created（aware datetime）与 is_overnight（跨零点发布 → 报告标「今晨」）。
    """
    window_start = now - timedelta(hours=float(cfg['window_hours']))
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    kept: list[dict] = []
    dropped = {'no_time': 0, 'out_of_window': 0}
    for item in items:
        created = parse_created_at(item.get('created_at'))
        if created is None:
            dropped['no_time'] += 1
            continue
        if not (window_start <= created <= now):
            dropped['out_of_window'] += 1
            continue
        out = dict(item)
        out['_created'] = created
        out['is_overnight'] = created >= midnight
        kept.append(out)
    return kept, dropped


def extract_symbols(text: str) -> list[str]:
    """从正文提取 $名称(代码)$ 的代码列表，去重保序。"""
    seen: set[str] = set()
    symbols: list[str] = []
    for match in _SYMBOL_RE.finditer(text or ''):
        code = match.group(1)
        if code not in seen:
            seen.add(code)
            symbols.append(code)
    return symbols


def load_prior_status_ids(data_dir: Path, today: str) -> set[str]:
    """index.json 里**今天之前**已爬过的文章 id。

    这些帖子不该进今天的候选池：`get_today_articles` 只收 crawl_time 是今天的
    条目，所以它们不会出现在 articles 里去重，只会被当成「热榜独有」重复分析
    一遍。必须在 select_top **之前**剔除，否则它们白占 top_n 名额，
    排名更靠后的新贴反而进不来。
    """
    path = data_dir / 'index.json'
    try:
        index = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return set()
    ids: set[str] = set()
    for key, info in (index.get('articles') or {}).items():
        if not isinstance(info, dict):
            continue
        if str(info.get('crawl_time') or '').startswith(today):
            continue  # 今天爬的，留给日报侧按 ID 合并（要挂 🔥 标记）
        ids.add(str(info.get('article_id') or key).strip())
    return ids


def load_prev_top_status_ids(data_dir: Path, now: datetime) -> set[str]:
    """昨日 top10 的 status_id —— 连续霸榜降权用。"""
    prev = (
        data_dir
        / 'hot_posts'
        / (now - timedelta(days=1)).strftime('%Y-%m-%d')
        / 'hot_top10.json'
    )
    try:
        doc = json.loads(prev.read_text(encoding='utf-8'))
        return {
            str(item.get('status_id'))
            for item in (doc.get('items') or [])
            if item.get('status_id')
        }
    except (OSError, json.JSONDecodeError):
        return set()


def _engagement_raw(item: dict) -> int:
    return (
        int(item.get('likes') or 0)
        + 2 * int(item.get('comments') or 0)
        + 3 * int(item.get('forwards') or 0)
    )


def _depth_score(text_len: int, thresholds: dict) -> float:
    """内容深度分档：长文加分、短贴降权（阈值见 config 的 hot_posts.depth_chars）。"""
    deep = int(thresholds.get('deep', 3000))
    medium = int(thresholds.get('medium', 1500))
    short = int(thresholds.get('short', 800))
    if text_len > deep:
        return 1.0
    if text_len > medium:
        return 0.75
    if text_len > short:
        return 0.5
    return 0.25


def score_items(
    items: list[dict],
    watchlist_codes: list[str],
    followed_ids: set[str],
    cfg: dict,
    now: datetime,
    prev_top_ids: set[str],
) -> list[dict]:
    """逐条打分，返回带 score / breakdown / 标签的新列表（保持入参顺序）。"""
    watchlist = set(watchlist_codes)
    half_life = float(cfg['freshness_half_life_hours'])
    weights = cfg['weights']
    depth_chars = cfg.get('depth_chars') or DEFAULT_CONFIG['depth_chars']
    penalties = cfg.get('penalties') or DEFAULT_CONFIG['penalties']
    total_n = max(1, len(items))

    engagement_raws = [_engagement_raw(item) for item in items]
    p95 = (
        sorted(engagement_raws)[max(0, math.ceil(0.95 * len(engagement_raws)) - 1)]
        if engagement_raws
        else 0
    )
    eng_denom = math.log1p(p95) if p95 > 0 else 0.0

    scored: list[dict] = []
    for idx, item in enumerate(items):
        created = item['_created']
        age_hours = max(0.0, (now - created).total_seconds() / 3600)
        rank_score = 1 - (idx / total_n)
        freshness = math.exp(-age_hours / half_life) if half_life > 0 else 0.0
        eng = (
            min(1.0, math.log1p(_engagement_raw(item)) / eng_denom)
            if eng_denom > 0
            else 0.0
        )
        # 有全文就用全文。摘要只有 133-179 字，自选股命中率和内容深度都测不准：
        # 实测今天 top10 里就有 2 条的自选股只出现在全文（NVDA/SKHY、SH600900）。
        body = item.get('content') or item.get('text', '')
        symbols = extract_symbols(body)
        hits = [s for s in symbols if s in watchlist]
        watchlist_score = 1.0 if hits else 0.0
        is_followed = str(item.get('author_id', '')) in followed_ids
        depth = _depth_score(len(body), depth_chars)
        # 回复贴（「回复@xxx」开头）对早报读者是残缺上下文，不管多长都按短贴降权
        if (body or '').lstrip().startswith('回复@'):
            depth = min(depth, 0.2)

        breakdown = {
            'rank': round(weights['rank'] * rank_score, 2),
            'freshness': round(weights['freshness'] * freshness, 2),
            'engagement': round(weights['engagement'] * eng, 2),
            'watchlist': round(weights['watchlist'] * watchlist_score, 2),
            'followed': round(
                weights['followed'] * (1.0 if is_followed else 0.0),
                2,
            ),
            'depth': round(weights['depth'] * depth, 2),
        }
        # 晒收益/账户流水：权重表之外直接扣分（用户明确不感兴趣）
        is_pnl = looks_like_pnl_showoff(body)
        if is_pnl:
            breakdown['pnl_showoff'] = -abs(float(penalties.get('pnl_showoff', 20)))
        score = sum(breakdown.values())
        if str(item.get('status_id', '')) in prev_top_ids:
            score *= float(cfg['repeat_decay'])
            breakdown['repeat_decay_applied'] = cfg['repeat_decay']

        out = {k: v for k, v in item.items() if k != '_created'}
        out.update(
            {
                'symbols_hit': hits,
                'followed': is_followed,
                'is_pnl_showoff': is_pnl,
                'score': round(score, 1),
                'breakdown': breakdown,
            }
        )
        scored.append(out)
    return scored


def filter_candidates(
    items: list[dict], prior_ids: set[str] | None = None
) -> list[dict]:
    """候选池过滤：去掉今天之前爬过的、以及摘要过短的帖子。

    必须在 `score_items` / `select_top` **之前**做 —— 否则这些条目会白占
    `top_n` 名额，排名更靠后的合格新贴反而进不了日报。
    """
    prior = prior_ids or set()
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        status_id = str(item.get('status_id', ''))
        if not status_id or status_id in seen or status_id in prior:
            continue
        if len(str(item.get('text') or '').strip()) < HOT_MIN_TEXT_CHARS:
            continue
        seen.add(status_id)
        out.append(item)
    return out


def select_top(scored: list[dict], top_n: int) -> list[dict]:
    return sorted(scored, key=lambda item: -item['score'])[:top_n]


def load_top_doc(
    data_dir: Path | str = 'data', report_date: str | None = None
) -> dict | None:
    """读当天已落盘的 hot_top10.json（generate_report 融合进日报用）。

    没有 / 损坏都返回 None —— 调用方按「无热榜数据」降级，不抛异常。
    """
    date_str = report_date or datetime.now(TZ).strftime('%Y-%m-%d')
    path = Path(data_dir) / 'hot_posts' / date_str / 'hot_top10.json'
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) and doc.get('items') else None


_REPLY_PREFIX_RE = re.compile(r'^回复@[^:：]+[:：]\s*')
_URL_USER_RE = re.compile(r'xueqiu\.com/(\d+)/')

# 摘要短于此长度就丢——与 analyzer.check_article_quality 对热贴/摘要的 80 字门槛
# 对齐：否则 40-79 字的帖子会白占一个 top_n 名额，然后作为「无效文章」进日报。
HOT_MIN_TEXT_CHARS = 80
HOT_TITLE_MAX_CHARS = 40


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def derive_hot_title(text: str, author: str = '') -> str:
    """从热榜摘要派生一个可读标题：折叠空白 → 去「回复@xxx:」前缀 → 截 40 字。"""
    flat = re.sub(r'\s+', ' ', text or '').strip()
    flat = _REPLY_PREFIX_RE.sub('', flat).strip()
    if len(flat) > HOT_TITLE_MAX_CHARS:
        flat = flat[:HOT_TITLE_MAX_CHARS] + '…'
    if len(flat) < 3:
        return f"{author or '雪球用户'}的热门贴"
    return flat


def _user_id_from_url(url: str) -> str:
    match = _URL_USER_RE.search(url or '')
    return match.group(1) if match else ''


def hot_item_to_article(item: dict, rank: int) -> dict | None:
    """单条热贴 → analyzer 可消费的 article dict；无效条目返回 None。

    `content` 默认用首页摘要（`content_source='excerpt'`）；若条目已被
    `enrich_items_with_fulltext` 补过全文（01:00 的 prepare job），直接用全文和
    真实标题。`rank` 是 Top N 列表里的 1 起序号。
    """
    status_id = str(item.get('status_id') or '').strip()
    text = (item.get('text') or '').strip()
    if not status_id or len(text) < HOT_MIN_TEXT_CHARS:
        return None

    author = item.get('author') or '雪球用户'
    url = item.get('url') or ''
    full_text = str(item.get('content') or '').strip()
    content = full_text or text
    return {
        'article_id': status_id,
        'user_id': str(item.get('author_id') or '').strip() or _user_id_from_url(url),
        'title': item.get('title') or derive_hot_title(text, author),
        'author': author,
        'publish_time': item.get('created_at') or '',
        'content': content,
        'filepath': '',
        'source': 'hot',
        'content_source': 'full' if full_text else 'excerpt',
        'url': url,
        'hot': {
            'rank': rank,
            'site_rank': item.get('rank'),
            'score': round(_as_float(item.get('score')), 1),
            'likes': _as_int(item.get('likes')),
            'comments': _as_int(item.get('comments')),
            'forwards': _as_int(item.get('forwards')),
            'is_overnight': bool(item.get('is_overnight')),
            'followed': bool(item.get('followed')),
            'symbols_hit': list(item.get('symbols_hit') or []),
        },
    }


def merge_hot_items(
    articles: list[dict], hot_items: list[dict]
) -> tuple[list[dict], dict]:
    """把热榜条目按 `article_id == status_id` 合并进 articles。

    重合条目只挂 `hot` 元数据（保留已爬到的全文，`source` 改 `both`），
    热榜独有条目追加在末尾。返回 `(merged, stats)`，
    `stats = {hot_total, merged, hot_only, skipped}`。
    """
    stats = {'hot_total': 0, 'merged': 0, 'hot_only': 0, 'skipped': 0}
    merged = list(articles)
    by_id: dict[str, dict] = {}
    for article in merged:
        key = str(article.get('article_id') or '').strip()
        if key:
            by_id[key] = article

    seen: set[str] = set()
    for rank, item in enumerate(hot_items or [], 1):
        stats['hot_total'] += 1
        if not isinstance(item, dict):
            stats['skipped'] += 1
            continue
        status_id = str(item.get('status_id') or '').strip()
        if not status_id or status_id in seen:
            stats['skipped'] += 1
            continue
        seen.add(status_id)

        hot_article = hot_item_to_article(item, rank)
        if hot_article is None:
            stats['skipped'] += 1
            continue

        existing = by_id.get(status_id)
        if existing is None:
            merged.append(hot_article)
            by_id[status_id] = hot_article
            stats['hot_only'] += 1
            continue

        if existing.get('hot'):
            stats['skipped'] += 1
            continue
        existing['source'] = 'both'
        existing['hot'] = hot_article['hot']
        existing.setdefault('content_source', 'full')
        existing.setdefault('url', hot_article['url'])
        # 已爬正文可能比热榜摘要还短（详情页只渲染出一部分、纯图片帖），这时
        # 用更长的摘要换掉它 —— 否则「有全文」反而让这条被判成无效文章。
        if len(hot_article['content']) > len(existing.get('content') or ''):
            existing['content'] = hot_article['content']
            # 用热榜侧的真实来源：01:00 prepare job 抓过全文就是 'full'，
            # 只有摘要时才是 'excerpt' —— 不能一律标成摘要，否则 LLM 会
            # 被「这是页面摘要，不要臆造」的提示误导，明明有全文却写「摘要未提供」。
            existing['content_source'] = hot_article['content_source']
        stats['merged'] += 1

    return merged, stats


# 全文抓取的总时间预算。限速 + 单次导航约 9-12 秒/条；给足 15 分钟。
# 预算截断会让排在后面的条目按摘要评分，与拿到全文的条目不可比 —— 所以宁可给宽。
# 注意：若调大 fulltext_interval_seconds，这里要同步够用（15s/条 × 48 条 ≈ 12 分钟）。
HOT_FULLTEXT_BUDGET_SECONDS = 900.0

# 相邻两次详情页抓取的最小间隔（秒），0 = 不额外限速（只靠插件限速器）。
# 站点风控对详情页是令牌桶：实测容量 ≈32 篇、每分钟回 1 个，即「一轮能过的
# 篇数 ≈ 32 + 本轮耗时(分钟)」。单次导航 ~9s、插件预留 6-9s 全被导航吃掉，
# 5 篇/分的连发会在第 42 篇左右撞墙（2026-10-19 01:00 与 09:19 两次实测一致）。
# 拉到 15s ≈ 4 篇/分，45 条约 11 分钟，预算内能过线。
HOT_FULLTEXT_INTERVAL_SECONDS = 0.0


def _apply_fulltext_result(item: dict, result: dict) -> bool:
    """把一次抓取结果写回 item；返回是否真的用上了全文。"""
    content = (result.get('content') or '').strip()
    if not content:
        return False
    # 详情页只渲染出一部分时，「全文」可能比首页摘要还短，
    # 换过去反而会把这条从可分析打成无效文章。
    if len(content) <= len(str(item.get('text') or '')):
        return False

    from title_guard import resolve_title

    item['content'] = content
    item['content_source'] = 'full'
    # 详情页没渲染出标题时 document.title 会回落成站点栏目名，必须先过
    # title_guard；不可用就保留热榜摘要派生的标题。
    fallback = derive_hot_title(item.get('text') or '', item.get('author') or '')
    title = resolve_title(result.get('title') or '', fallback)
    if title:
        item['title'] = title
    return True


def enrich_items_with_fulltext(
    items: list[dict],
    log=None,
    budget_seconds: float = HOT_FULLTEXT_BUDGET_SECONDS,
    fetch=None,
    interval_seconds: float = HOT_FULLTEXT_INTERVAL_SECONDS,
) -> dict:
    """给热榜条目补抓全文，写回 `item['content']` / `['title']` / `['content_source']`。

    正常由 prepare job（`hot_posts.py --fetch-fulltext`）调用，抓好的
    正文随 hot_top10.json 一起落盘，08:00 的日报生成只读文件、一个页面请求都不发。

    - 已有 `content` 的条目跳过（幂等，重跑不会重复抓）。
    - 相邻两次抓取的**发起时刻**至少拉开 `interval_seconds` 秒（0 = 不额外限速），
      等待时间计入 `budget_seconds` 预算。
    - 撞 WAF / 熔断冷却 → **立刻停手**，剩余条目保留首页摘要。绝不重试放大。
    - `fetch` 可注入（测试用），默认走 `opencli_extractor.get_article_content`。

    Returns:
        `{attempted, fetched, skipped}` 统计。
    """
    log = log or logger  # 参数名用 log，避免遮蔽模块级 logger
    stats = {'attempted': 0, 'fetched': 0, 'skipped': 0}
    targets = [
        item
        for item in items or []
        if isinstance(item, dict) and item.get('url') and not item.get('content')
    ]
    if not targets:
        return stats

    if fetch is None:
        # 局部导入：纯离线场景（无 opencli）不被拖累，测试也能直接 monkeypatch。
        from opencli_extractor import get_article_content

        def fetch(url):  # noqa: ANN001 - 局部适配，参数只有 url
            # max_retries=0：一次被拦就是一次请求。重试会把单条放大成 2-3 次导航。
            return get_article_content(url, max_retries=0)

    deadline = time.monotonic() + budget_seconds
    interval = max(0.0, float(interval_seconds or 0.0))
    last_start: float | None = None
    for idx, item in enumerate(targets, 1):
        if time.monotonic() > deadline:
            log.warning(
                '热榜正文抓取: 超出 %.0f 秒预算，剩余 %d 条用首页摘要',
                budget_seconds,
                len(targets) - idx + 1,
            )
            break
        # 节流：把相邻两次详情页的「发起时刻」拉开到 interval 秒以上（等首条
        # 不用等）。等待时间同样计入预算 —— deadline 检查在循环顶部兜底。
        if interval > 0 and last_start is not None:
            wait = interval - (time.monotonic() - last_start)
            if wait > 0:
                time.sleep(wait)
        stats['attempted'] += 1
        last_start = time.monotonic()
        try:
            result = fetch(item['url'])
        except (OSError, RuntimeError, ValueError, ImportError) as exc:
            # opencli 不在 PATH、子进程超时、适配器崩掉。后面的条目大概率同样
            # 失败，直接停手走摘要兜底。编程错误（KeyError/AttributeError）不吞。
            log.warning(
                '热榜正文抓取: %s 抓取异常（%s），剩余条目用首页摘要',
                item.get('status_id'),
                exc,
            )
            break

        if result.get('cooldown') or result.get('waf_detected'):
            reason = '熔断冷却' if result.get('cooldown') else 'WAF 拦截'
            log.warning(
                '热榜正文抓取: %s，停止后续抓取，剩余 %d 条用首页摘要',
                reason,
                len(targets) - idx,
            )
            break

        if _apply_fulltext_result(item, result):
            stats['fetched'] += 1
        else:
            log.info(
                '热榜正文抓取: %s 未用上全文（空正文或比摘要短），保留摘要',
                item.get('status_id'),
            )
            stats['skipped'] += 1

    log.info(
        '热榜正文抓取: %d/%d 条拿到全文（%d 条保留摘要）',
        stats['fetched'],
        stats['attempted'],
        stats['skipped'],
    )
    return stats


def _candidate_summary(item: dict) -> dict:
    """候选池复盘用：留下名次依据，不存正文（体积大）和原始 DOM 摘要。"""
    return {
        'status_id': item.get('status_id'),
        'author': item.get('author'),
        'title': item.get('title') or '',
        'url': item.get('url'),
        'created_at': item.get('created_at'),
        'score': item.get('score'),
        'breakdown': item.get('breakdown'),
        'content_source': item.get('content_source', 'excerpt'),
        'content_len': len(item.get('content') or item.get('text') or ''),
        'symbols_hit': item.get('symbols_hit') or [],
        'followed': item.get('followed'),
        'is_pnl_showoff': bool(item.get('is_pnl_showoff')),
    }


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding='utf-8',
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='抓取并打分雪球首页热门贴')
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--config', default=None, help='config.yaml 路径')
    parser.add_argument(
        '--fetch-fulltext',
        action='store_true',
        help='给 top_n 条目抓正文再落盘（01:00 的 prepare job 用）。'
        '不加则只存首页摘要，08:00 由日报按摘要分析。',
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
    )
    data_dir = Path(args.data_dir)
    cfg = load_hot_config(Path(args.config) if args.config else None)
    now = datetime.now(TZ)
    started = time.time()
    stats: dict = {'date': now.strftime('%Y-%m-%d'), 'ok': False}
    try:
        if not cfg['enabled']:
            stats.update(ok=True, skipped='disabled')
            logger.info('hot_posts.enabled=false，跳过热门贴抓取')
            return 0
        raw_items = fetch_hot_posts(cfg)
        stats['fetched'] = len(raw_items)
        kept, dropped = filter_window(raw_items, now, cfg)
        stats['window_kept'] = len(kept)
        stats['window_dropped'] = dropped
        # 原始数据先落盘：后面任何一步失败，都有抓取证据可查
        out_dir = data_dir / 'hot_posts' / now.strftime('%Y-%m-%d')
        _write_json(
            out_dir / 'hot_raw.json',
            {'fetched': raw_items, 'kept': kept, 'dropped': dropped},
        )

        # 只剔除「今天之前已爬过」的帖子：它们不在今日 articles 里，留着只会被
        # 当成热榜独有重复分析。今天爬过的必须保留 —— 日报侧要按 ID 合并并挂
        # 🔥 热度标记。两个过滤都放在 select_top **之前**，免得短贴/旧贴白占名额。
        prior_ids = load_prior_status_ids(data_dir, now.strftime('%Y-%m-%d'))
        prev_top_ids = load_prev_top_status_ids(data_dir, now)
        candidates = filter_candidates(kept, prior_ids)
        stats['candidates'] = len(candidates)
        if not candidates:
            raise RuntimeError('滚动窗口过滤后 0 条候选')

        # 自选股列表必须在全文抓取**之前**取：它按天缓存、至多 1 次请求，但若
        # 排在全文之后，全文途中撞墙触发的熔断冷却会让这个调用抛 RuntimeError
        # ——整轮作废，已抓到手的几十篇全文全部丢弃（2026-10-19 01:00 实测）。
        # 先取就把「最后一步撞冷却」从失败路径上摘掉；全文途中撞墙由 enrich
        # 内部停手兜底，拿到多少用多少，hot_top10.json 照常落盘。
        watchlist_codes = fetch_watchlist_codes(data_dir, now)
        followed_ids = load_followed_author_ids()

        # 全文抓取只在 prepare job 里做（--fetch-fulltext）：那一轮页面请求
        # 与 08:00 的爬取分属不同滚动小时。结果直接写进 hot_top10.json，
        # 08:00 的日报生成只读文件、零请求。
        #
        # **必须在打分之前抓**：排序里的「自选股命中」(15 分) 和「内容深度」
        # (5 分) 都要看全文。摘要只有 133-179 字，拿它排出来的名次会明显偏
        # —— 实测 2026-10-18 的 top10 里就有 2 条自选股只出现在全文里。
        if args.fetch_fulltext:
            prefetch_max = int(cfg.get('prefetch_max') or 0)
            targets = candidates[:prefetch_max] if prefetch_max > 0 else candidates
            stats['fulltext'] = enrich_items_with_fulltext(
                targets,
                budget_seconds=float(
                    cfg.get('fulltext_budget_seconds', HOT_FULLTEXT_BUDGET_SECONDS)
                ),
                interval_seconds=float(
                    cfg.get('fulltext_interval_seconds', HOT_FULLTEXT_INTERVAL_SECONDS)
                ),
            )

        scored = score_items(
            candidates,
            watchlist_codes,
            followed_ids,
            cfg,
            now,
            prev_top_ids,
        )
        top_items = select_top(scored, int(cfg['top_n']))

        window_hours = float(cfg['window_hours'])
        window_start = now - timedelta(hours=window_hours)
        doc = {
            'generated_at': now.isoformat(),
            'window': {
                'start': window_start.isoformat(),
                'end': now.isoformat(),
                'hours': int(window_hours),
                'start_label': window_start.strftime('%m-%d %H:%M'),
                'end_label': now.strftime('%m-%d %H:%M'),
            },
            'candidates': len(candidates),
            'items': top_items,
        }
        _write_json(out_dir / 'hot_top10.json', doc)
        # 全部候选的名次依据：回答「第 12 名为什么没进榜」不用重跑一遍。
        _write_json(
            out_dir / 'hot_candidates.json',
            {
                'generated_at': now.isoformat(),
                'candidates': len(scored),
                'items': [
                    _candidate_summary(item)
                    for item in sorted(scored, key=lambda x: -x['score'])
                ],
            },
        )
        stats.update(
            ok=True,
            top_n=len(top_items),
            top1_score=top_items[0]['score'] if top_items else None,
        )
        logger.info(
            '热门贴完成: 抓%d → 窗口%d → 候选%d → top%d（最高 %.1f 分）',
            len(raw_items),
            len(kept),
            len(candidates),
            len(top_items),
            top_items[0]['score'] if top_items else 0,
        )
        return 0
    except Exception as exc:
        stats['error'] = str(exc)[:300]
        logger.warning('热门贴环节失败（日报降级为纯自选账号文章）: %s', exc)
        return 1
    finally:
        stats['elapsed_ms'] = int((time.time() - started) * 1000)
        try:
            _write_json(data_dir / '.last_hot_stats.json', stats)
        except OSError:
            pass


if __name__ == '__main__':
    sys.exit(main())
