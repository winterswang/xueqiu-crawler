#!/usr/bin/env python3
"""Regression tests for MiniMax M3 malformed JSON repair."""

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "scripts"))

from analyzer import ArticleAnalyzer


BROKEN_IPO_WEEKLY = r'''```json
{
    "summary": "港股IPO周报：17家递表，11家过聆讯，4家招股。",
    "category": "行业分析",
    "related_stocks": [
        "丰宜科技",
        "三星电气(601567.SH)",
        "晶合集成(688249.SH)",
        "安克创新(300866.SZ)",
        "琻捷电子(06675.HK)",
        "海清智元(01392.HK)",
        "星源材质(06067.HK)",
        "华健未来(06132.HK)",
        "新易盛(300502.SZ)",
        "麦格米特(002851.SZ)",
        "百洋医药(301015.SZ)",
        "沐曦股份(688802.SH)",
        "瑞达期货(002961.SZ)"
    ],
    "core_points": [
        "核心观点1：港股IPO市场活跃度维持高位，过去一周（2026.6.8-6.14）共有17家公司递交上市申请，同时11家公司通过港交所聆讯，4家启动招股，递表-聆讯-招股各环节均呈现密集推进状态，显示港交所作为中资企业重要融资平台的吸引力持续增强。",
        "核心观点2：A股公司赴港二次上市或双重主要上市成为显著趋势，三星电气（601567.SH）、晶合集成（688249.SH）、安克创新（300866.SZ）、新易盛（300502.SZ）、麦格米特（002851.SZ）、沐曦股份（688802.SH）、瑞达期货（002961.SZ）等多家A股上市公司密集宣布筹划H股发行，涵盖电气设备、半导体、消费电子、期货等多个行业，反映港股估值修复和流动性优势对A股龙头的吸引力。",
        "核心观点3：AI算力与机器人产业链成为本轮港股IPO的重要标签，沐曦股份（国产GPU）、安克创新（消费电子出海）、晶合集成（半导体制造）、海清智元（01392.HK）等公司的递表或招股，显示港股市场正在系统性承接中国硬科技资产的估值重估机会，赛道选择与产业政策导向高度一致。"
    ],
    "deep_analysis": {
        "business_quality": "从递表和招股公司构成看，本批次港股IPO标的整体质量偏向硬科技与高端制造。沐曦股份属于国产GPU赛道，晶合集成为半导体晶圆代工企业，新易盛为光模块龙头，麦格米特为电力电子设备厂商，星源材质为锂电隔膜龙头，这些公司均处于具有真实产业需求和国产替代逻辑的赛道，其商业模式以技术壁垒和规模效应为核心，理论上具备产生自由现金流的潜力。但需注意，硬科技公司普遍处于资本开支高峰期，短期自由现金流可能为负，价值评估更应聚焦于远期市占率和盈利能力，而非当期PE。",
        "management": "文章未披露具体管理层信息，但可从公司质地做侧面判断。安克创新、晶合集成、麦格米特等均为A股细分行业龙头，管理层经过A股市场多年检验，资本配置和公司治理相对规范；沐曦股份等新兴GPU公司则尚处上市前夜，其管理层执行力有待通过上市后的信息披露和资本运作来验证。整体而言，本批次公司多为成熟产业玩家，管理层诚信风险相对可控。",
        "key_risks": "关键风险有三：一是港股流动性风险，4家公司集中招股但当周无公司挂牌，认购反应和上市后表现仍待观察，若市场情绪转弱可能出现破发；二是估值风险，A股公司赴港上市后通常存在AH折价，但当前港股市场对硬科技资产给予的估值溢价能否持续存疑；三是行业β风险，半导体、AI算力、锂电等赛道本身周期性强，相关公司业绩波动可能放大股价波动。",
        "competitive_position": "从竞争格局看，本批次递表公司多为各自细分赛道的国产替代龙头或出海品牌，竞争位置相对靠前。安克创新在全球消费电子充电配件领域已建立品牌护城河；晶合集成在DDIC晶圆代工领域与联电、世界先进形成竞争；沐曦股份在国产GPU中与寒武纪、海光信息形成第二梯队竞争。赴港上市有助于这些公司获取国际资本、提升海外品牌认知，从而在中美科技博弈背景下强化供应链多元化和客户多元化优势。",
        "outlook": "后续核心观察窗口为：（1）当周4家招股公司（琻捷电子、海清智元、星源材质、华健未来）的配售结果和首日表现，将直接反映港股市场对中小市值硬科技标的的承接力度；（2）已递表的17家公司中，预计2-3个月内将密集通过聆讯，需重点关注沐曦股份、新易盛、麦格米特等明星项目的聆讯进度和基石投资者质量；（3）A股公司赴港上市潮的持续性，将是判断港股市场是否进入新一轮"中概回流+A股出海"双轮驱动行情的关键信号。"
    }
}
```'''


BROKEN_LIULIUMEI = r'''```json
{
    "summary": "溜溜梅港股上市首日暴涨175%，但毛利率下滑、上市前大额派息流向控股股东存隐忧。",
    "category": "公司研究",
    "related_stocks": [
        "溜溜梅(06658.HK)",
        "首钢朗泽(02553)",
        "大金重工(01081)",
        "分众传媒(002027.SZ)"
    ],
    "core_points": [
        "核心观点1：溜溜梅首日挂牌表现亮眼，发行价43.58港元开盘暴涨175%至120港元，香港公开发售超额认购6586.73倍，国际配售亦获2.64倍认购，反映散户打新热度极高，但首日暴涨更多反映港股小盘股流通性稀缺及情绪溢价，并非基本面驱动，94.57亿港元市值已隐含较高增长预期。",
        "核心观点2：公司业绩呈现\"增收放缓、盈利改善\"背离特征，2023-2025年营收从13.22亿元增至17.11亿元，但2025年增速骤降至5.86%，同期净利润却从9923万元增至1.82亿元，主因销售费用率优化（2.72亿元，费用率15.9%）而非毛利率提升，盈利质量改善可持续性存疑。",
        "核心观点3：毛利率从40.1%连续下滑至35.6%，反映零食赛道竞争白热化、品类同质化严重，公司护城河薄弱；同时IPO前向控股股东杨帆夫妇（合计持股87.77%）派息6730万元，分红高度集中于实控人，引发市场对上市后小股东回报机制的担忧。"
    ],
    "deep_analysis": {
        "business_quality": "溜溜梅本质是一家"品类品牌"型零食公司，依赖青梅这一细分赛道的认知占位赚钱，2024年以4.9%份额位列中国果类零食第一，看似龙头但行业极度分散（CR1不足5%），护城河很浅。商业模式靠"营销驱动+渠道渗透"，销售费用率高达15.9%，叠加毛利率持续下滑，说明定价权弱、难以将成本压力转嫁给消费者，自由现金流质量不高。募资61%用于扩产能，但行业增速放缓背景下，产能消化是隐忧，资本支出回报率（ROIC）大概率承压。",
        "management": "杨帆夫妇持股87.77%，控制权高度集中，决策效率高但小股东话语权缺失。最值得警惕的是IPO前夕向老股东派息6730万元，且绝大部分流入实控人口袋——这是在上市前用公司自有资金"补贴"大股东，相当于老股东以更低成本参与上市，损害新股东利益。资本配置上，将61%募资用于扩产、21%用于营销，但未设定明确分红或回购政策，管理层的股东回报意识有待观察。",
        "key_risks": "第一，行业竞争加剧风险——零食量贩渠道（如零食很忙、赵一鸣）兴起后品牌方议价权被压缩，毛利率从40.1%降至35.6%已是预警，若CR1始终低于5%，品类龙头地位随时可被颠覆。第二，单品类天花板风险——公司过度依赖梅类产品，2025年营收增速骤降至5.86%说明品类红利接近尾声，新增长曲线尚未验证。第三，上市后流动性陷阱——首日暴涨175%后94亿港元市值对应约52倍PE，远超零食行业可比公司均值，一旦情绪退潮可能面临估值大幅回归。",
        "competitive_position": "与三只松鼠、良品铺子、来伊份等综合零食品牌相比，溜溜梅走"窄而深"路线，青梅品类心智占位是其核心优势，但与洽洽食品（瓜子品类龙头）的稳固护城河相比，青梅作为可选零食的消费粘性远低于瓜子，品类壁垒不高。与同行差距方面，公司在中国果冻行业仅排第六（2.9%份额），梅冻产品增长是否可持续存疑。整体看，竞争格局是"龙头不龙头、护城河不深"，并未形成可持续的竞争优势。",
        "outlook": "短期关注2026年中报数据，核心看三点：一是营收增速能否企稳回升至10%以上（若继续低于10%则IPO估值难以消化）；二是销售费用率走势（若维持15%以上说明营销依赖症未解）；三是梅冻及西梅新品类的收入贡献占比。长期需观察2027年解禁期前后大股东减持动作，以及公司能否将IPO募资的扩产能投入转化为实际ROE提升。"
    }
}
```'''


def _analyzer():
    # Disable LLM retry so these regression tests prove local parsing/repair works.
    analyzer = ArticleAnalyzer(api_key="", provider="minimax")
    analyzer._retry_json_fix = lambda *_args, **_kwargs: None
    return analyzer


def _assert_full_analysis(analysis):
    assert analysis["summary"]
    assert "[解析异常]" not in analysis["summary"]
    assert len(analysis["core_points"]) >= 3
    assert all("[解析异常]" not in point for point in analysis["core_points"])
    deep = analysis["deep_analysis"]
    for key in ["business_quality", "management", "key_risks", "competitive_position", "outlook"]:
        assert key in deep
        assert "[解析异常]" not in deep[key]
        assert len(deep[key]) > 30


def test_parse_minimax_m3_json_with_unescaped_quotes_in_outlook():
    analysis = _analyzer()._parse_response(
        BROKEN_IPO_WEEKLY,
        {"title": "一图解码：港股IPO一周回顾 17家公司递表 沐曦股份筹划港股上市"},
    )
    _assert_full_analysis(analysis)
    assert "中概回流+A股出海" in analysis["deep_analysis"]["outlook"]


def test_parse_minimax_m3_json_with_multiple_unescaped_quotes_in_deep_analysis():
    analysis = _analyzer()._parse_response(
        BROKEN_LIULIUMEI,
        {"title": "【IPO前哨】溜溜梅（06658.HK）登陆港股大涨175%"},
    )
    _assert_full_analysis(analysis)
    assert "品类品牌" in analysis["deep_analysis"]["business_quality"]
    assert "补贴" in analysis["deep_analysis"]["management"]
    assert "窄而深" in analysis["deep_analysis"]["competitive_position"]


def test_partial_fallback_preserves_useful_fields_when_repair_cannot_parse():
    response = r'''```json
{
  "summary": "部分解析也要保留摘要",
  "category": "公司研究",
  "related_stocks": ["测试公司(01234.HK)"],
  "core_points": [
    "核心观点1：即使 JSON 尾部损坏，也应该保留第一条核心观点，避免日报只显示解析异常。",
    "核心观点2：字段级提取比空 fallback 更有价值。"
  ],
  "deep_analysis": {
    "business_quality": "这是一段超过三十字的商业模式字段，用于验证 partial fallback 能保留有价值内容。",
    "management": "这是一段超过三十字的管理层字段，用于验证 partial fallback 能保留有价值内容。",
    "key_risks": "这是一段超过三十字的关键风险字段，用于验证 partial fallback 能保留有价值内容。",
    "competitive_position": "这是一段超过三十字的竞争格局字段，用于验证 partial fallback 能保留有价值内容。",
    "outlook": "这是一段超过三十字的后续关注字段，用于验证 partial fallback 能保留有价值内容。"
  }
BROKEN_TAIL
```'''
    analysis = _analyzer()._parse_response(response, {"title": "partial fallback"})
    assert analysis["summary"] == "部分解析也要保留摘要"
    assert analysis["category"] == "公司研究"
    assert analysis["related_stocks"] == ["测试公司(01234.HK)"]
    assert len(analysis["core_points"]) == 2
    assert "商业模式字段" in analysis["deep_analysis"]["business_quality"]
    assert "[解析异常]" not in analysis["deep_analysis"]["key_risks"]
