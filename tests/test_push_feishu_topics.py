"""飞书摘要的「今日热点」提炼 —— 不能把空白或模型的推脱串发出去.

2026-10-05 实测把模型回的「未收到文章标题。请重新提供内容。」当成热点推送了；
10-08 / 10-09 又出现摘要区整个为空。根因有两条，都在这里钉住：

1. 标题只从「必读 / 值得关注」两段取。当天全是「参考」级时列表为空，
   prompt 里就放了个空列表 → 模型回推脱串。
2. 模型返回空串或推脱串时没有任何兜底。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.push_feishu as pf


REPORT_WITH_MUST_READ = """# 日报

## 二、优先级分类

| 🔴 必读 | 1 |

## 四、文章详情

### 🔴 必读

#### 1. Marvell：远期指引狂飙

正文略

### 🟡 值得关注

#### 1. 拼多多资本配置现状及展望

正文略

### 🔵 参考

1. [某条短帖](https://xueqiu.com/a)（某人）
"""

REPORT_ONLY_REFERENCE = """# 日报

## 四、文章详情

### 🔴 核心观点


暂无核心观点提炼

### ⚠️ 风险与机会

今日无集中风险提示

### 🔵 参考

1. [这么糊，那算了吧](https://xueqiu.com/a)（永庆好公司）
2. [把他们在亚马逊和 Temu 上的运营费用进行比较](https://xueqiu.com/b)（吉吉Queen）
"""


def _fake_client(monkeypatch, reply: str | None, *, raises: bool = False):
    calls: list[dict] = []

    class _Choice:
        def __init__(self, text):
            self.message = type("M", (), {"content": text})()

    class _Resp:
        def __init__(self, text):
            self.choices = [_Choice(text)]

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if raises:
                raise RuntimeError("boom")
            return _Resp(reply)

    class _Client:
        def __init__(self):
            self.chat = type("C", (), {"completions": _Completions()})()

    monkeypatch.setattr(pf, "_client", _Client())
    return calls


def test_module_imports_without_credentials():
    """回归（2026-10-09 CI）：导入这个模块不该要求 API 凭证.

    原来客户端在模块级构造，没有凭证的环境里 `OpenAI(...)` 直接抛
    `OpenAIError: Missing credentials` —— 测试文件在**收集阶段**就报错，
    而本地因为有 .env 一直没暴露。
    """
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in ("ARK_API_KEY", "MINIMAX_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY")
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0,'scripts'); import push_feishu",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr[-400:]


# ── 标题提取 ────────────────────────────────────────────────────────────


def test_titles_prefer_must_read_and_watch_sections():
    titles = pf._extract_article_titles(REPORT_WITH_MUST_READ)

    assert titles[0] == "1. Marvell：远期指引狂飙"
    assert any("拼多多" in t for t in titles)


def test_titles_fall_back_to_reference_when_no_priority_sections():
    """回归：当天全是参考级时，原来会拿到空列表 → 模型回推脱串."""
    titles = pf._extract_article_titles(REPORT_ONLY_REFERENCE)

    assert titles, "「参考」段必须能兜底，否则 prompt 里是空列表"
    assert any("这么糊" in t for t in titles)


# ── 可用性判定 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["", "太短", "未收到文章标题。请重新提供内容。", "抱歉，我无法完成这个请求。"],
)
def test_refusals_and_empty_are_not_usable(text):
    assert pf._looks_usable(text) is False


def test_real_summary_is_usable():
    assert (
        pf._looks_usable("段永平谈腾讯：下半年利润或个位数下滑，AI 产出是关键。")
        is True
    )


# ── 端到端：拿不到可用输出时一定走兜底 ──────────────────────────────────


def test_empty_title_list_skips_the_llm_entirely(monkeypatch):
    calls = _fake_client(monkeypatch, "不该被调用")

    result = pf.generate_hot_topics_summary("# 空报告\n\n没有任何段落\n")

    assert calls == [], "没有标题就不该调 LLM"
    assert result and "未收到" not in result


def test_llm_refusal_falls_back_instead_of_publishing_it(monkeypatch):
    """回归（2026-10-05）：模型推脱串被原样当成热点推了出去."""
    _fake_client(monkeypatch, "未收到文章标题。请重新提供内容。")

    result = pf.generate_hot_topics_summary(REPORT_ONLY_REFERENCE)

    assert "未收到" not in result
    assert "请重新提供" not in result
    assert result.strip()


def test_empty_llm_output_falls_back(monkeypatch):
    """回归（2026-10-08/09）：模型返回空串 → 摘要区整个为空."""
    _fake_client(monkeypatch, "   ")

    result = pf.generate_hot_topics_summary(REPORT_WITH_MUST_READ)

    assert result.strip(), "不能是空串"


def test_llm_exception_falls_back(monkeypatch):
    _fake_client(monkeypatch, None, raises=True)

    result = pf.generate_hot_topics_summary(REPORT_WITH_MUST_READ)

    assert result.strip()


def test_good_llm_output_is_used_verbatim(monkeypatch):
    reply = "段永平谈腾讯：下半年利润或个位数下滑，AI 产出是关键。"
    _fake_client(monkeypatch, reply)

    assert pf.generate_hot_topics_summary(REPORT_WITH_MUST_READ) == reply


# ── 兜底内容本身 ────────────────────────────────────────────────────────


def test_fallback_uses_core_viewpoints_when_present():
    report = (
        "# 日报\n\n### 🔴 核心观点\n\n- **某大V**：腾讯下半年利润或个位数下滑。\n\n"
        "### ⚠️ 风险与机会\n\n- 风险略\n"
    )

    assert "腾讯下半年利润" in pf._fallback_topics(report)


def test_fallback_skips_placeholders_and_says_so():
    """报告全是「暂无…」占位时，宁可说一句实话，也不能留空."""
    result = pf._fallback_topics(REPORT_ONLY_REFERENCE)

    assert result.strip()
    assert "暂无" not in result


def test_fallback_is_never_empty_even_for_an_empty_report():
    assert pf._fallback_topics("").strip()


# ── 真实报告上跑一遍（不调 LLM） ────────────────────────────────────────


def test_real_reports_always_produce_a_usable_fallback():
    reports = sorted(Path("data/daily_reports").glob("2026-10-0*.md"))
    if not reports:
        pytest.skip("no daily reports on disk")
    for path in reports:
        text = path.read_text(encoding="utf-8")
        assert pf._fallback_topics(text).strip(), path.name
        # 每一份真实报告的标题提取都不该炸
        assert isinstance(pf._extract_article_titles(text), list), path.name
