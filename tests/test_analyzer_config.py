#!/usr/bin/env python3
"""配置一致性回归测试。"""

import sys
from pathlib import Path

import yaml

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))

from analyzer import ArticleAnalyzer


def test_minimax_config_uses_ark_coding_model(monkeypatch):
    """生产 config 不应把 analyzer 覆盖回旧 MiniMax 模型名。"""
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("BAILIAN_API_KEY", raising=False)

    cfg = yaml.safe_load((_project_root / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert cfg["analysis"]["models"]["minimax"] == "deepseek-v4-flash-ga-260731"

    analyzer = ArticleAnalyzer(api_key="", config=cfg)
    assert analyzer.provider == "minimax"
    assert analyzer.model_name == "deepseek-v4-flash-ga-260731"
