#!/usr/bin/env python3
"""配置一致性回归测试。

核心不变量（PROJECT_LOG D-009）：模型 id 只有 `config/config.yaml` 一个来源，
`analyzer` 与 `push_feishu` 必须解析出**同一个**值。历史上它有 5 份副本，
换模型时改一处漏一处，而且两边都不报错，没人会发现。
"""

import sys
from pathlib import Path

import pytest
import yaml

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))

from analyzer import ArticleAnalyzer
from llm_config import resolve_model, resolve_provider

_EXPECTED_MODEL = "deepseek-v4-1-flash-260910"
_CREDENTIAL_VARS = (
    "ARK_API_KEY",
    "MINIMAX_API_KEY",
    "BAILIAN_API_KEY",
    "ANALYZE_LLM_MODEL",
    "ANALYZER_PROVIDER",
)


@pytest.fixture
def clean_env(monkeypatch):
    """清掉凭证与模型相关环境变量，保证走 config.yaml 这条路径。"""
    for name in _CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


def _config() -> dict:
    return yaml.safe_load(
        (_project_root / "config" / "config.yaml").read_text(encoding="utf-8")
    )


def test_minimax_config_uses_ark_coding_model(clean_env):
    """生产 config 不应把 analyzer 覆盖回旧 MiniMax 模型名。"""
    cfg = _config()

    assert cfg["analysis"]["models"]["minimax"] == _EXPECTED_MODEL

    analyzer = ArticleAnalyzer(api_key="", config=cfg)
    assert analyzer.provider == "minimax"
    assert analyzer.model_name == _EXPECTED_MODEL


def test_analyzer_and_push_feishu_resolve_the_same_model(clean_env, monkeypatch):
    """D-009 的守卫：两个消费者必须同源。

    push_feishu 在模块级构造 OpenAI 客户端，没有 key 时导入就抛错，
    所以先塞一个假 key——本测试不发起任何请求。
    """
    monkeypatch.setenv("ARK_API_KEY", "test-key-not-used")

    import push_feishu

    analyzer = ArticleAnalyzer(api_key="", config=_config())

    assert push_feishu.MODEL == analyzer.model_name
    assert push_feishu.MODEL == _EXPECTED_MODEL


def test_resolve_model_follows_config_file(clean_env):
    """不传 config 时应自己读 config.yaml，而不是靠调用方传对。"""
    assert resolve_model() == _EXPECTED_MODEL


def test_resolve_model_raises_instead_of_falling_back(clean_env, tmp_path):
    """config 里查不到就报错——兜底模型名是隐藏的第二来源，宁可响亮失败。"""
    empty = tmp_path / "config.yaml"
    empty.write_text("analysis:\n  models:\n    aliyun: glm-5\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="无法解析模型 id"):
        resolve_model("minimax", config_path=str(empty))


def test_provider_prefers_explicit_then_env_then_config(clean_env, monkeypatch):
    cfg = _config()

    assert resolve_provider("aliyun", config=cfg) == "aliyun"
    assert resolve_provider(config=cfg) == "minimax"

    monkeypatch.setenv("ANALYZER_PROVIDER", "aliyun")
    assert resolve_provider(config=cfg) == "aliyun"


def test_analyzer_does_not_read_a_model_env_override(clean_env, monkeypatch):
    """曾经有个 ANALYZE_LLM_MODEL 覆盖，已去掉。

    留一个不入库的第二来源，会让「服务器 .env 里是旧值」静默压过仓库配置，
    两个消费者一起退回旧模型且没人发现。这条测试锁住它不会回来。
    """
    monkeypatch.setenv("ANALYZE_LLM_MODEL", "some-old-model")

    analyzer = ArticleAnalyzer(api_key="", config=_config())

    assert analyzer.model_name == _EXPECTED_MODEL
    assert resolve_model() == _EXPECTED_MODEL
