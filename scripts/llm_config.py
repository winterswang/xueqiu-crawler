#!/usr/bin/env python3
"""LLM 模型 id 的唯一解析入口。

为什么要单独一个模块
--------------------
模型 id 曾经散在 5 处：`config/config.yaml`、`analyzer.py` 的两处默认值、
`push_feishu.py` 的默认值、以及不入库的 `.env`。换模型时改一处漏一处。

2026-10-07 收口到本模块（PROJECT_LOG.md D-009）：`analyzer` 与 `push_feishu`
**都必须走这里**。否则会出现「同一轮里分析用一个模型、飞书摘要用另一个」的
口径分裂 —— 而且因为两边都不报错，没人会发现。

唯一来源
--------
`config/config.yaml` 的 `analysis.models[provider]`（provider 取
`analysis.provider`）。**不读环境变量**：曾经用 `ANALYZE_LLM_MODEL` 覆盖，
但那会制造「服务器 .env 里留着旧值 → 模型静默退回旧版、且两个消费者一起退回、
没人发现」的陷阱。要让模型跟着仓库走，就不能再有一个不入库的第二来源。

注意：这里的值是**方舟接口认的 wire model id**（形如
`deepseek-v4-1-flash-260910`，全小写带日期戳），不是展示名。
填展示名（如 `DeepSeek-V4.1-Flash`）会直接把 `model=` 调用打挂。
查当前可用 id：`GET $ARK_CODING_BASE_URL/models`。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "config.yaml"
DEFAULT_PROVIDER = "minimax"


@lru_cache(maxsize=None)
def load_config(config_path: str | None = None) -> dict:
    """读 config.yaml。带缓存——generate_daily_report 会逐篇调用解析。"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def resolve_provider(provider: str | None = None, *, config: dict | None = None) -> str:
    """解析 provider：显式入参 > ANALYZER_PROVIDER > config.yaml > 默认。"""
    if provider:
        return provider
    env_provider = os.environ.get("ANALYZER_PROVIDER")
    if env_provider:
        return env_provider
    cfg = config if config is not None else load_config()
    return (cfg.get("analysis") or {}).get("provider") or DEFAULT_PROVIDER


def resolve_model(
    provider: str | None = None,
    *,
    config: dict | None = None,
    config_path: str | None = None,
) -> str:
    """解析模型 id。找不到时抛错而不是给一个兜底名字。

    兜底模型名本身就是隐藏的第二来源：它会在 config 出问题时静默启用一个
    没人维护的模型，调用失败还查不出原因。宁可响亮地失败。
    """
    cfg = config if config is not None else load_config(config_path)
    resolved_provider = resolve_provider(provider, config=cfg)
    models = (cfg.get("analysis") or {}).get("models") or {}
    model = models.get(resolved_provider)
    if not model:
        raise RuntimeError(
            f"无法解析模型 id：{config_path or DEFAULT_CONFIG_PATH} 里没有 "
            f"analysis.models.{resolved_provider}"
        )
    return str(model)
