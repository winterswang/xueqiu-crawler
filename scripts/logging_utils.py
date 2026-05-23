#!/usr/bin/env python3
"""
统一日志模块

提供两个日志通道:
- main_log:   主流程日志 (info/debug/error) → logs/cron_daily.log
- parse_log:  JSON 解析失败诊断日志 → logs/parse_failures.log
- stats_log:  执行统计日志 → logs/execution_stats.log
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 主流程 logger
_main_logger = None

def _init_main_logger():
    global _main_logger
    if _main_logger:
        return _main_logger
    _main_logger = logging.getLogger("xueqiu.main")
    _main_logger.setLevel(logging.DEBUG)
    # 不传播到 root logger
    _main_logger.propagate = False
    # 文件 handler（自动轮转，最大 5MB × 3 个备份）
    fh = RotatingFileHandler(
        LOG_DIR / "cron_daily.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(module)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    _main_logger.addHandler(fh)
    # 控制台 handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    _main_logger.addHandler(ch)
    return _main_logger

def get_logger(name: str = "xueqiu.main"):
    return _init_main_logger()

# 解析失败专用 logger (记录完整 LLM 响应)
_parse_logger = None

def _init_parse_logger():
    global _parse_logger
    if _parse_logger:
        return _parse_logger
    _parse_logger = logging.getLogger("xueqiu.parse_failures")
    _parse_logger.setLevel(logging.DEBUG)
    _parse_logger.propagate = False
    fh = RotatingFileHandler(
        LOG_DIR / "parse_failures.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    _parse_logger.addHandler(fh)
    return _parse_logger

def get_parse_logger():
    return _init_parse_logger()

# 执行统计 logger
_stats_logger = None

def _init_stats_logger():
    global _stats_logger
    if _stats_logger:
        return _stats_logger
    _stats_logger = logging.getLogger("xueqiu.stats")
    _stats_logger.setLevel(logging.INFO)
    _stats_logger.propagate = False
    fh = logging.FileHandler(LOG_DIR / "execution_stats.log", encoding="utf-8")
    fh.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    _stats_logger.addHandler(fh)
    return _stats_logger

def get_stats_logger():
    return _init_stats_logger()


def log_parse_failure(article_title: str, response: str, strategy: str = "all_failed"):
    """记录 JSON 解析失败的完整诊断信息"""
    plog = get_parse_logger()
    plog.info("=" * 60)
    plog.info(f"文章标题: {article_title}")
    plog.info(f"失败策略: {strategy}")
    plog.info(f"响应长度: {len(response)} 字符")
    plog.info(f"响应预览 (前200字): {response[:200]}")
    plog.info(f"响应预览 (后200字): {response[-200:]}")
    # 检查 JSON 特征
    has_brace = "{" in response
    has_bracket = "[" in response
    has_json_block = "```json" in response
    has_md_block = "```" in response
    plog.info(f"JSON 特征: 花括号={'✅' if has_brace else '❌'}, 方括号={'✅' if has_bracket else '❌'}, json块={'✅' if has_json_block else '❌'}, md块={'✅' if has_md_block else '❌'}")
    plog.info(f"完整响应:\n{response}")
    plog.info("=" * 60)
    # 同时记录到主日志
    logger = get_logger()
    logger.warning(f"JSON解析失败: {article_title}, 响应{len(response)}字符, has_json_block={has_json_block}")


def log_execution_stage(stage: str, status: str, detail: str = ""):
    """记录执行阶段统计"""
    slog = get_stats_logger()
    slog.info(f"stage={stage} status={status} {detail}")


def log_execution_summary(summary: dict):
    """记录执行总结"""
    slog = get_stats_logger()
    slog.info(f"EXECUTION_SUMMARY: {summary}")


# MiniMax API 调用专用 logger（结构化 JSON 日志）
_api_logger = None

def _init_api_logger():
    global _api_logger
    if _api_logger:
        return _api_logger
    _api_logger = logging.getLogger("xueqiu.api")
    _api_logger.setLevel(logging.DEBUG)
    _api_logger.propagate = False
    fh = RotatingFileHandler(
        LOG_DIR / "minimax_api.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    _api_logger.addHandler(fh)
    return _api_logger


def log_api_call(event: str, **kwargs):
    """
    记录 MiniMax API 调用事件（结构化 JSON）
    
    Args:
        event: start / success / retry / failure
        **kwargs: 调用上下文（model, attempt, latency_ms, error_type, etc.）
    """
    alog = _init_api_logger()
    import json as _json
    payload = {"event": event}
    payload.update(kwargs)
    alog.info(_json.dumps(payload, ensure_ascii=False, default=str))
