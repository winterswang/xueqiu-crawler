#!/usr/bin/env python3
"""
AI 分析总结模块 - 重构版

使用智谱 GLM-5 模型对投资文章进行深度分析
- 质量检测：内容 > 200 字符才走 GLM-5
- 优先级分类：必读/值得关注/参考
- 详细分析：核心观点、价值投资评估、相关股票
"""

import os
import sys
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

from logging_utils import get_logger, log_parse_failure, log_api_call

# 自加载 .env（不依赖 shell 环境变量传递，兼容 cron/手动调用等场景）
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / '.env'
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass

try:
    from anthropic import Anthropic
except ImportError:
    print("请安装 anthropic: pip install anthropic")
    Anthropic = None

try:
    from openai import OpenAI as OpenAIClient
except ImportError:
    print("请安装 openai: pip install openai")
    OpenAIClient = None


# ============ 市场分类 ============
def classify_stock_market(stock: str) -> str:
    """判断股票所属市场"""
    stock_clean = stock.strip()

    # 港股格式: 00883.HK, 09988.HK, 3690.HK
    if '.HK' in stock_clean.upper() or '港股' in stock_clean:
        return '港股'
    # 美股格式: AAPL, TSLA, BMBM, HOOD — 已知美股代码白名单
    us_stock_pattern = re.compile(
        r'^('
        r'AAPL|MSFT|GOOGL|GOOG|AMZN|META|TSLA|NVDA|AMD|INTC|NFLX|DIS|BA|CAT|JPM|GS|MS|V|MA|JNJ|PFE|UNH|HD|MCD|KO|PEP|WMT|COST|TGT|SBUX|NKE|UPS|FDX|CSCO|ORCL|IBM|CRM|ADBE|ACN|QCOM|TXN|AVGO|BMBM|MTCH|HOOD|COIN|MSTR|SQ|SHOP|DDOG|SNOW|CRWD|ZM|ROKU|PYPL|SNAP|UBER|LYFT|ABNB|RIVN|LCID|PLTR|SOFI|UPST|AFRM'
        r')$'
    )
    if us_stock_pattern.match(stock_clean.upper()):
        return '美股'
    # 美股宽泛检测：纯大写字母（2-5字符）但排除已知 A 股拼音缩写
    if re.match(r'^[A-Z][A-Z.]{1,4}$', stock_clean.replace('-', '')) and not re.match(r'^[0-9]', stock_clean):
        return '美股'
    # A股: 6位数字, 000xxx, 300xxx, 688xxx
    if re.match(r'^[0-9]{6}', stock_clean):
        return 'A股'
    # 日股: 4-5位数字 + .T
    if '.T' in stock_clean.upper() or '日股' in stock_clean:
        return '日股'
    return '其他'

def group_stocks_by_market(stocks: List[str]) -> dict:
    """将股票列表按市场分组"""
    groups = {'A股': [], '港股': [], '美股': [], '日股': [], '其他': []}
    for stock in stocks:
        market = classify_stock_market(stock)
        groups[market].append(stock)
    return {k: v for k, v in groups.items() if v}

# ============ 质量检测 ============

def check_article_quality(article: dict) -> Tuple[bool, List[str]]:
    """
    质量检测（综合版，合并 quality_check.py 的 4 项检测）
    
    Returns:
        (passed, issues): 是否通过，问题列表
    """
    issues = []
    
    title = article.get('title', '')
    content = article.get('content', '')
    
    # 标题检测
    if not title or len(title.strip()) < 3:
        issues.append("标题为空或过短")
    
    # 内容检测
    if not content or len(content.strip()) < 200:
        issues.append(f"正文为空或过短({len(content)}字符)")
    
    # 作者检测
    if not article.get('author'):
        issues.append("作者为空")
    
    # 发布时间检测
    if not article.get('publish_time'):
        issues.append("发布时间为空")
    
    # 关键检测：标题和正文必须合格
    critical_issues = ['标题为空或过短', '正文为空或过短']
    has_critical = any(any(ci in i for ci in critical_issues) for i in issues)
    
    return (not has_critical, issues)


def calculate_priority_score(article: dict, analysis: dict = None) -> dict:
    """
    计算优先级评分详情
    
    Returns:
        {
            'total': 总分,
            'content_depth': 内容深度分,
            'keywords': 关键词分,
            'category': 主题归类分,
            'core_points': 核心观点分,
            'title_quality': 标题质量分
        }
    """
    content = article.get('content', '')
    title = article.get('title', '')
    
    scores = {
        'content_depth': 0,
        'keywords': 0,
        'category': 0,
        'core_points': 0,
        'title_quality': 0,
        'total': 0
    }
    
    # 1. 内容深度评分（最高 30 分）
    content_len = len(content)
    if content_len > 5000:
        scores['content_depth'] = 30
    elif content_len > 3000:
        scores['content_depth'] = 25
    elif content_len > 1500:
        scores['content_depth'] = 15
    elif content_len > 500:
        scores['content_depth'] = 5
    
    # 2. 价值投资关键词评分（最高 25 分）
    deep_keywords = [
        # A股/港股传统价值投资术语
        '估值', 'PE', 'PB', 'ROE', 'ROIC', 'DCF', '自由现金流',
        '护城河', '安全边际', '商业模式', '竞争优势', '壁垒',
        '财报', '年报', '季报', '业绩',
        '内在价值', '价值投资',
        '毛利率', '净利率', '利润率', '周转率',
        '管理层', '资本配置', '股东回报',
        # 通用深度分析关键词（覆盖海外市场/行业研究）
        '深度', '剖析', '研究', '分析', '访谈', 'CEO',
        '行业', '竞争格局', '盈利', '增长', '空间', '景气度',
        '供需', '产能', '份额', '渗透率',
        '风险', '催化剂', '展望', '预测'
    ]
    keyword_hits = sum(1 for kw in deep_keywords if kw in title + content)
    scores['keywords'] = min(keyword_hits * 2, 25)
    
    # 2.1 作者权重分（最高 10 分）- 深度大V加分，媒体号不加分
    author = article.get('author', '')
    premium_authors = [
        'MZInvest', '逸修1', 'Waterzzz', '仓又加错-Leo',
        '慧博', 'Ricky', '不明真相的群众', '李想', '大道无形我有型'
    ]
    if any(pa in author for pa in premium_authors):
        scores['author_bonus'] = 10
    else:
        scores['author_bonus'] = 0
    
    # 3. GLM-5 分析结果评分
    if analysis:
        # 3.1 主题归类（最高 10 分）
        category = analysis.get('category', '')
        if category == '公司研究':
            scores['category'] = 10
        elif category == '行业分析':
            scores['category'] = 7
        elif category == '投资理念':
            scores['category'] = 5
        
        # 3.4 核心观点数量（每个 2 分，最高 10 分）
        core_points = analysis.get('core_points', [])
        scores['core_points'] = min(len(core_points) * 2, 10)
    
    # 4. 标题质量（最高 10 分）
    title_keywords = ['深度', '分析', '研究', '估值', '财报', '年报', '护城河']
    if any(kw in title for kw in title_keywords):
        scores['title_quality'] = 10
    elif len(title) > 20:
        scores['title_quality'] = 5
    
    # 计算总分
    scores['total'] = sum([
        scores['content_depth'],
        scores['keywords'],
        scores.get('author_bonus', 0),
        scores['category'],
        scores['core_points'],
        scores['title_quality']
    ])
    
    return scores


def classify_priority(article: dict, analysis: dict = None) -> str:
    """
    优先级分类 V2 - 基于内容质量与价值投资相关性综合评分
    
    评分维度：
    1. 内容深度（字数）- 最高 30 分
    2. 价值投资关键词 - 最高 25 分
    3. 主题归类 - 最高 10 分
    4. 核心观点数量 - 最高 10 分
    5. 标题质量 - 最高 10 分
    总分：最高 85 分
    
    Returns:
        'must_read' | 'worth_reading' | 'reference'
    """
    scores = calculate_priority_score(article, analysis)
    total = scores['total']
    
    # 根据总分确定优先级
    # 必读：60+ 分（高质量价值投资分析）
    # 值得关注：40-59 分（有价值的分析）
    # 市场资讯：<40分 或 标题带收评/IPO追踪/IPO前哨/年中/盘点（流水线媒体内容）
    # 参考：短状态/预告
    title = article.get('title', '')
    is_news = any(kw in title for kw in ['收评', 'IPO追踪', 'IPO前哨', '年中盘点', 'IPO', '新股'])
    if is_news and total < 60:
        return 'market_news'
    if total >= 60:
        return 'must_read'
    elif total >= 30:
        return 'worth_reading'
    else:
        return 'reference'


# ============ 文章分析器 ============

class ArticleAnalyzer:
    """文章分析器 - GLM-5 深度分析"""
    
    def __init__(self, api_key: str = None, provider: str = None, config: dict = None):
        """
        Args:
            config: 从 config.yaml 读取的配置 dict（含 analysis.models）
        """
        self.logger = get_logger()
        self.client = None
        self.stats = {"total_calls": 0, "parse_success": 0, "parse_failed": 0, "api_errors": 0,
                       "retry_count": 0, "total_latency_ms": 0, "success_calls": 0}

        # 支持 minimax / aliyun 双 provider
        self.provider = provider or os.environ.get('ANALYZER_PROVIDER', 'minimax')

        # 从 config 读取模型名和截断阈值
        analysis_cfg = (config or {}).get('analysis', {})
        models_cfg = analysis_cfg.get('models', {})
        self.model_name = models_cfg.get(self.provider, 'minimax-m3')
        self.max_content_chars = analysis_cfg.get('max_content_chars', 8000)
        
        # 重试配置
        retry_cfg = analysis_cfg.get('retry', {})
        self.retry_max = retry_cfg.get('max_retries', 3)
        self.retry_base_delay = retry_cfg.get('base_delay_ms', 2000) / 1000.0
        self.retry_max_delay = retry_cfg.get('max_delay_ms', 30000) / 1000.0
        self.retry_backoff = retry_cfg.get('backoff_multiplier', 2.0)
        self.retry_http_codes = retry_cfg.get('retry_on_http_codes', [429, 529, 502, 503, 504])
        self.request_timeout = retry_cfg.get('request_timeout_ms', 120000) / 1000.0

        # API Key 解析：优先传入的 api_key > 环境变量
        # minimax provider 已迁到字节 coding plan，优先 ARK_API_KEY，兼容旧 MINIMAX_API_KEY
        if api_key:
            self.api_key = api_key
        elif self.provider == 'minimax':
            self.api_key = os.environ.get('ARK_API_KEY', '') or os.environ.get('MINIMAX_API_KEY', '')
        else:
            self.api_key = os.environ.get('BAILIAN_API_KEY', '')

        # 兜底：provider=minimax 时也尝试 BAILIAN_API_KEY，反之亦然
        if not self.api_key and self.provider == 'minimax':
            self.api_key = os.environ.get('BAILIAN_API_KEY', '')
            if self.api_key:
                self.logger.warning("MINIMAX_API_KEY 未设置，回退使用 BAILIAN_API_KEY")
        if not self.api_key and self.provider != 'minimax':
            self.api_key = os.environ.get('MINIMAX_API_KEY', '')
            if self.api_key:
                self.logger.warning("BAILIAN_API_KEY 未设置，回退使用 MINIMAX_API_KEY")

        if self.provider == 'minimax':
            # 字节 coding plan（OpenAI 兼容接口），取代即将废弃的 MiniMax 官方 anthropic baseURL。
            # 默认 minimax-m3；api_key 优先 ARK_API_KEY。
            # 注：不再读旧的 MINIMAX_BASE_URL（.env 中可能残留官方地址会污染），改用 ARK 专属变量。
            minimax_base = os.environ.get(
                'ARK_CODING_BASE_URL',
                'https://ark.cn-beijing.volces.com/api/coding/v3',
            )
            # coding plan 用 ARK_API_KEY，未显式传入时优先取环境变量
            if not api_key:
                self.api_key = os.environ.get('ARK_API_KEY', '') or self.api_key
            if self.api_key and OpenAIClient:
                try:
                    self.client = OpenAIClient(
                        api_key=self.api_key,
                        base_url=minimax_base,
                        max_retries=0,
                        timeout=self.request_timeout,
                    )
                    self.logger.info(
                        f"MiniMax(coding plan) 客户端初始化成功: model={self.model_name}, "
                        f"timeout={self.request_timeout}s, retry_max={self.retry_max}, "
                        f"base_url={minimax_base}"
                    )
                except Exception as e:
                    self.logger.error(f"MiniMax(coding plan) 客户端初始化失败: {e}")
                    self.client = None
            else:
                self.logger.warning(
                    f"MiniMax(coding plan) 客户端未初始化: api_key={'有' if self.api_key else '无'}, "
                    f"OpenAIClient={'可用' if OpenAIClient else '不可用'}"
                )
        elif self.provider == 'aliyun' and self.api_key:
            self.client = OpenAIClient(
                api_key=self.api_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
        elif self.api_key and OpenAIClient:
            self.client = OpenAIClient(
                api_key=self.api_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )
    
    def _call_minimax_with_retry(self, prompt: str, title: str) -> Optional[str]:
        """
        调用 MiniMax API，带指数退避重试和详细日志
        
        重试策略:
        - 529 (overloaded): 指数退避 2s → 4s → 8s → ... → max 30s
        - 429/502/503/504: 同上
        - NetworkError / Timeout: 同上
        - 其他错误 (4xx): 不重试
        
        Returns:
            成功返回 response text，失败返回 None
        """
        last_error = None
        
        for attempt in range(self.retry_max + 1):  # 首次 + 重试
            t0 = time.time()
            
            log_api_call(
                "start",
                title=title[:60],
                model=self.model_name,
                attempt=attempt,
                max_attempts=self.retry_max + 1,
                prompt_chars=len(prompt),
            )
            
            try:
                resp = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=8192,
                )
                latency_ms = int((time.time() - t0) * 1000)

                # 提取文本（OpenAI 兼容格式）
                usage = getattr(resp, 'usage', None)
                input_tokens = getattr(usage, 'prompt_tokens', 0) if usage else 0
                output_tokens = getattr(usage, 'completion_tokens', 0) if usage else 0
                choice = resp.choices[0] if getattr(resp, 'choices', None) else None
                stop_reason = getattr(choice, 'finish_reason', 'unknown') if choice else 'unknown'
                response = (choice.message.content if choice and choice.message else "") or ""

                # 部分模型会将推理放在 reasoning_content，正文为空时尝试回退提取
                if not response and choice and choice.message:
                    reasoning = getattr(choice.message, 'reasoning_content', None)
                    if reasoning:
                        self.logger.warning(
                            f"MiniMax 正文为空，从 reasoning_content 提取 (长度={len(reasoning)})"
                        )
                        json_start = reasoning.rfind('{')
                        response = reasoning[json_start:] if json_start != -1 else reasoning
                
                log_api_call(
                    "success",
                    title=title[:60],
                    model=self.model_name,
                    attempt=attempt,
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    response_chars=len(response),
                    stop_reason=str(stop_reason),
                )
                
                self.stats["total_latency_ms"] += latency_ms
                self.stats["success_calls"] += 1
                return response
                
            except Exception as e:
                latency_ms = int((time.time() - t0) * 1000)
                error_type = type(e).__name__
                error_str = str(e)[:200]
                
                # 判断是否可重试
                retryable = False
                error_code = None
                
                # Anthropic SDK 异常分类
                if hasattr(e, 'status_code'):
                    error_code = e.status_code
                    retryable = error_code in self.retry_http_codes
                elif hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                    error_code = e.response.status_code
                    retryable = error_code in self.retry_http_codes
                elif isinstance(error_str, str):
                    # 从错误消息中提取 HTTP 状态码
                    for code in self.retry_http_codes:
                        if str(code) in error_str:
                            error_code = code
                            retryable = True
                            break
                    # Network/timeout 错误也可重试
                    if any(kw in error_type.lower() for kw in ('timeout', 'connection', 'network', 'apierror')):
                        retryable = True
                    if any(kw in error_str.lower() for kw in ('timeout', 'connection refused', 'reset by peer')):
                        retryable = True
                
                log_api_call(
                    "failure",
                    title=title[:60],
                    model=self.model_name,
                    attempt=attempt,
                    latency_ms=latency_ms,
                    error_type=error_type,
                    error_code=error_code,
                    error_message=error_str,
                    retryable=retryable,
                    is_last_attempt=(attempt >= self.retry_max),
                )
                
                last_error = e
                
                if not retryable or attempt >= self.retry_max:
                    break
                
                # 计算退避延迟
                delay = min(
                    self.retry_base_delay * (self.retry_backoff ** attempt),
                    self.retry_max_delay
                )
                self.stats["retry_count"] += 1
                
                log_api_call(
                    "retry",
                    title=title[:60],
                    attempt=attempt,
                    next_attempt=attempt + 1,
                    delay_ms=int(delay * 1000),
                    reason=error_type,
                    error_code=error_code,
                )
                
                self.logger.warning(
                    f"MiniMax 调用失败 (attempt={attempt}, retryable={retryable}): "
                    f"{title[:40]}, {error_type}"
                    + (f"(HTTP {error_code})" if error_code else "")
                    + f", {delay:.1f}s 后重试"
                )
                time.sleep(delay)
        
        # 所有重试耗尽
        self.logger.error(
            f"MiniMax 调用最终失败 (共 {self.retry_max + 1} 次尝试): "
            f"{title[:40]}, last_error={type(last_error).__name__}: {str(last_error)[:100]}"
        )
        raise last_error
    
    def analyze_article(self, article: dict) -> dict:
        """分析单篇文章"""
        # 质量检测
        passed, issues = check_article_quality(article)
        
        if not passed:
            return {
                'quality_passed': False,
                'issues': issues,
                'priority': 'reference',
                'analysis': None
            }
        
        # GLM-5 分析
        if not self.client:
            return self._mock_analysis(article)
        
        prompt = self._build_prompt(article)
        title = article.get('title', 'N/A')
        
        try:
            if self.provider == 'minimax':
                response = self._call_minimax_with_retry(prompt, title)
            else:
                completion = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                )
                response = completion.choices[0].message.content
            self.stats["total_calls"] += 1
            self.logger.debug(f"LLM 调用成功: {article.get('title', 'N/A')[:40]}, provider={self.provider}, model={self.model_name}")
            analysis = self._parse_response(response, article)

            # 校验 LLM 响应完整性
            if not self._validate_analysis(analysis):
                missing = self._missing_fields(analysis)
                self.logger.warning(f"LLM 响应字段不完整, 缺失={missing}, title={article.get('title', 'N/A')[:40]}")
                analysis = self._repair_analysis(analysis, article)
            
            # 计算优先级和评分详情
            scores = calculate_priority_score(article, analysis)
            priority = classify_priority(article, analysis)
            
            return {
                'quality_passed': True,
                'issues': [],
                'priority': priority,
                'scores': scores,
                'analysis': analysis
            }
        except Exception as e:
            self.stats["api_errors"] += 1
            error_type = type(e).__name__
            error_str = str(e)[:200]
            self.logger.error(
                f"LLM 调用最终失败: {article.get('title', 'N/A')[:40]}, "
                f"type={error_type}, message={error_str}, "
                f"总重试={self.stats['retry_count']}次",
                exc_info=False
            )
            # 仅对未知异常输出完整 traceback
            if 'Overloaded' not in error_type and 'RateLimit' not in error_type and 'Timeout' not in error_type:
                self.logger.debug(f"异常详情: {e}", exc_info=True)
            return {
                'quality_passed': True,
                'issues': [f"分析失败: {e}"],
                'priority': 'reference',
                'scores': calculate_priority_score(article),
                'analysis': None
            }
    
    def _build_prompt(self, article: dict) -> str:
        """构建深度分析 Prompt"""
        title = article.get('title', '无标题')
        author = article.get('author', '未知')
        content = article.get('content', '')
        word_count = len(content)
        
        # 截取内容（从 config 读取最大字符数）
        truncated = len(content) > self.max_content_chars
        content_text = content[:self.max_content_chars]
        if truncated:
            content_text += "\n\n[注：原文较长，已截断至前 {} 字符]".format(self.max_content_chars)
        
        return f"""你是一位专业的价值投资研究专家，专注于从年报、季报、行业分析中挖掘被低估的信息。

## 分析任务

对以下文章进行深度分析，结构分为两部分：【信息提炼】和【深度评价】。

## 文章信息
- **标题**：{title}
- **作者**：{author}
- **字数**：{word_count} 字

## 正文内容
{content_text}

---

## 输出格式（严格 JSON）

```json
{{
    "summary": "一句话总结这篇文章的核心结论（30字以内）",
    "category": "行业分析 | 公司研究 | 投资理念 | 宏观经济 | 其他",
    "topic_category": "科技/AI/互联网 | 新能源/制造业 | 医药/医疗 | 消费/零售 | 金融/地产/宏观 | 海外市场 | IPO/新股 | 市场策略/投资理念 | 商业航天 | 其他",
    "sentiment": "bullish | bearish | neutral",
    "related_stocks": ["股票名称(code)", "..."],
    "core_points": [
        "核心观点1：内容要饱满，不少于50字，完整呈现论点",
        "核心观点2：内容要饱满，不少于50字，完整呈现论点",
        "核心观点3：内容要饱满，不少于50字，完整呈现论点"
    ],
    "deep_analysis": {{
        "business_quality": "商业模式判断：从价值投资角度评价公司靠什么赚钱、护城河强弱、是否容易产生自由现金流（100字以上）",
        "management": "管理层评估：管理层是否诚信、资本配置能力如何、过往承诺是否兑现（80字以上）",
        "key_risks": "关键风险：列出1-3个最大不确定性，说明为什么重要（80字以上）",
        "competitive_position": "竞争格局：与同行相比，竞争优势还是劣势，差距在拉开还是缩小（80字以上）",
        "outlook": "后续关注点：下一个需要观察的时间窗口或指标是什么（60字以上）"
    }}
}}
```

## 写作要求
- core_points 要有观点、有数据支撑，不是复述原文
- deep_analysis 的五个维度要充分利用文章中已有的信息，不要泛泛而谈
- 语言简洁专业，直接切入要点
- JSON 字符串内部如需使用英文双引号，必须转义为 \"
- 禁止在字段值中直接出现未转义的英文双引号
- 输出必须能被 Python json.loads 直接解析
- 不要输出 JSON 以外的任何解释文本
- 请确保输出是有效的 JSON"""

    def _extract_json_candidate(self, response: str) -> str:
        """Extract the most likely JSON object text from an LLM response."""
        if not response:
            return ''
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL | re.IGNORECASE)
        if json_match:
            return json_match.group(1).strip()
        fenced_match = re.search(r'```\s*(\{.*?\})\s*```', response, re.DOTALL)
        if fenced_match:
            return fenced_match.group(1).strip()
        start = response.find('{')
        end = response.rfind('}')
        if start != -1 and end != -1 and end > start:
            return response[start:end + 1].strip()
        return response.strip()

    def _repair_unescaped_quotes_in_json_strings(self, text: str) -> str:
        """Repair common MiniMax JSON issue: bare quotes inside string values.

        Conservative state-machine repair:
        - Quotes followed by JSON structural delimiters (: , } ]) close strings.
        - Other unescaped quotes encountered while inside a string are treated as
          literal content and escaped as \".
        """
        if not text:
            return text

        out = []
        in_string = False
        escape = False
        n = len(text)

        for i, ch in enumerate(text):
            if not in_string:
                out.append(ch)
                if ch == '"':
                    in_string = True
                continue

            if escape:
                out.append(ch)
                escape = False
                continue

            if ch == '\\':
                out.append(ch)
                escape = True
                continue

            if ch == '"':
                j = i + 1
                while j < n and text[j].isspace():
                    j += 1
                next_ch = text[j] if j < n else ''
                if next_ch in {':', ',', '}', ']'} or next_ch == '':
                    out.append(ch)
                    in_string = False
                else:
                    out.append('\\"')
                continue

            out.append(ch)

        return ''.join(out)

    def _parse_json_with_local_repair(self, response: str, title: str, strategy_name: str) -> Optional[dict]:
        """Parse JSON after deterministic local repair for unescaped quotes."""
        candidate = self._extract_json_candidate(response)
        if not candidate:
            return None
        repaired = self._repair_unescaped_quotes_in_json_strings(candidate)
        result = json.loads(repaired)
        self.stats["parse_success"] += 1
        self.logger.debug(f"JSON解析成功(strategy={strategy_name}): {title[:40]}")
        return result

    def _extract_string_field_from_raw(self, response: str, field: str, limit: int = None) -> str:
        """Best-effort extraction of a JSON string field from malformed raw text."""
        m = re.search(rf'"{re.escape(field)}"\s*:\s*"', response)
        if not m:
            return ''
        start = m.end()
        chars = []
        escape = False
        for i in range(start, len(response)):
            ch = response[i]
            if escape:
                chars.append(ch)
                escape = False
                continue
            if ch == '\\':
                chars.append(ch)
                escape = True
                continue
            if ch == '"':
                j = i + 1
                while j < len(response) and response[j].isspace():
                    j += 1
                if j >= len(response) or response[j] in {',', '}', ']'}:
                    break
                chars.append(ch)
                continue
            chars.append(ch)
        value = ''.join(chars).strip()
        return value[:limit] if limit else value

    def _extract_string_array_from_raw(self, response: str, field: str, max_items: int = None) -> list:
        """Best-effort extraction of a JSON string array from malformed raw text."""
        m = re.search(rf'"{re.escape(field)}"\s*:\s*\[', response)
        if not m:
            return []
        i = m.end()
        items = []
        n = len(response)
        while i < n:
            while i < n and (response[i].isspace() or response[i] == ','):
                i += 1
            if i >= n or response[i] == ']':
                break
            if response[i] != '"':
                i += 1
                continue
            i += 1
            chars = []
            escape = False
            while i < n:
                ch = response[i]
                if escape:
                    chars.append(ch)
                    escape = False
                    i += 1
                    continue
                if ch == '\\':
                    chars.append(ch)
                    escape = True
                    i += 1
                    continue
                if ch == '"':
                    j = i + 1
                    while j < n and response[j].isspace():
                        j += 1
                    if j >= n or response[j] in {',', ']'}:
                        i = j
                        break
                    chars.append(ch)
                    i += 1
                    continue
                chars.append(ch)
                i += 1
            item = ''.join(chars).strip()
            if item:
                items.append(item)
                if max_items and len(items) >= max_items:
                    break
            if i < n and response[i] == ']':
                break
            if i < n and response[i] == ',':
                i += 1
        return items

    def _partial_analysis_from_raw(self, response: str) -> dict:
        """Build a useful partial analysis from malformed JSON instead of empty [解析异常]."""
        summary = self._extract_string_field_from_raw(response, 'summary', 100) or self._extract_summary_from_raw(response)
        category = self._extract_string_field_from_raw(response, 'category', 30) or '其他'
        topic_category = self._extract_string_field_from_raw(response, 'topic_category', 30) or '其他'
        # 标准化主题分类
        valid_topics = {'科技/AI/互联网', '新能源/制造业', '医药/医疗', '消费/零售', '金融/地产/宏观',
                       '海外市场', 'IPO/新股', '市场策略/投资理念', '商业航天', '其他'}
        if topic_category not in valid_topics:
            topic_category = '其他'
        core_points = self._extract_string_array_from_raw(response, 'core_points', max_items=3)
        if not core_points:
            core_points = ['[部分解析] JSON 结构异常，已保留可提取摘要；请查看原文获取完整分析']

        deep = {
            'business_quality': self._extract_string_field_from_raw(response, 'business_quality') or '[部分解析] 未能提取商业模式字段',
            'management': self._extract_string_field_from_raw(response, 'management') or '[部分解析] 未能提取管理层字段',
            'key_risks': self._extract_string_field_from_raw(response, 'key_risks') or '[部分解析] 未能提取关键风险字段',
            'competitive_position': self._extract_string_field_from_raw(response, 'competitive_position') or '[部分解析] 未能提取竞争格局字段',
            'outlook': self._extract_string_field_from_raw(response, 'outlook') or '[部分解析] 未能提取后续关注字段',
        }
        return {
            'category': category,
            'topic_category': topic_category,
            'related_stocks': self._extract_stocks_from_raw(response),
            'core_points': core_points,
            'summary': summary,
            'deep_analysis': deep,
        }
    
    def _parse_response(self, response: str, article: dict = None) -> dict:
        """解析 LLM 响应 - 精确提取 JSON 对象（5 层 fallback）"""
        title = (article or {}).get('title', '未知文章')
        strategies_failed = []
        
        try:
            # Strategy 1: fenced code block with json
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group(1))
                self.stats["parse_success"] += 1
                self.logger.debug(f"JSON解析成功(strategy=code_block): {title[:40]}")
                return result
        except Exception as e:
            strategies_failed.append(f"code_block: {e}")

        try:
            # Strategy 2: raw_decode — first complete JSON object
            # Handles truncated/malformed JSON gracefully
            decoder = json.JSONDecoder()
            start = response.find('{')
            if start != -1:
                obj, end = decoder.raw_decode(response[start:])
                self.stats["parse_success"] += 1
                self.logger.debug(f"JSON解析成功(strategy=raw_decode): {title[:40]}")
                return obj
        except Exception as e:
            strategies_failed.append(f"raw_decode: {e}")

        try:
            # Strategy 3: strip markdown artifacts and retry
            cleaned = re.sub(r'^[\s\S]*?```json\s*', '', response)
            cleaned = re.sub(r'\s*```[\s\S]*$', '', cleaned)
            result = json.loads(cleaned)
            self.stats["parse_success"] += 1
            self.logger.debug(f"JSON解析成功(strategy=cleaned): {title[:40]}")
            return result
        except Exception as e:
            strategies_failed.append(f"cleaned: {e}")

        try:
            # Strategy 4: deterministic local repair for common MiniMax malformed JSON
            result = self._parse_json_with_local_repair(response, title, "local_repair")
            if result is not None:
                return result
            strategies_failed.append("local_repair: no JSON candidate found")
        except Exception as e:
            strategies_failed.append(f"local_repair: {e}")

        # Strategy 5: retry — ask LLM to fix its own broken JSON
        if strategies_failed:
            try:
                fixed_response = self._retry_json_fix(response, article)
                if fixed_response:
                    # Re-run strategies 1-3 on corrected response
                    try:
                        json_match = re.search(r'```json\s*(\{.*?\})\s*```', fixed_response, re.DOTALL)
                        if json_match:
                            result = json.loads(json_match.group(1))
                            self.stats["parse_success"] += 1
                            self.logger.debug(f"JSON解析成功(strategy=retry+code_block): {title[:40]}")
                            return result
                    except Exception:
                        pass
                    try:
                        decoder = json.JSONDecoder()
                        start = fixed_response.find('{')
                        if start != -1:
                            obj, _ = decoder.raw_decode(fixed_response[start:])
                            self.stats["parse_success"] += 1
                            self.logger.debug(f"JSON解析成功(strategy=retry+raw_decode): {title[:40]}")
                            return obj
                    except Exception:
                        pass
                    try:
                        cleaned = re.sub(r'^[\s\S]*?```json\s*', '', fixed_response)
                        cleaned = re.sub(r'\s*```[\s\S]*$', '', cleaned)
                        result = json.loads(cleaned)
                        self.stats["parse_success"] += 1
                        self.logger.debug(f"JSON解析成功(strategy=retry+cleaned): {title[:40]}")
                        return result
                    except Exception:
                        pass
                    try:
                        return self._parse_json_with_local_repair(fixed_response, title, "retry+local_repair")
                    except Exception as e:
                        strategies_failed.append(f"retry: {e}")
            except Exception as e:
                strategies_failed.append(f"retry: {e}")

        # All strategies failed — log full diagnostics and return graceful fallback
        self.stats["parse_failed"] += 1
        strategy_detail = "; ".join(strategies_failed)
        log_parse_failure(title, response, strategy_detail)
        
        # Final fallback: preserve useful fields when full JSON parsing is impossible.
        return self._partial_analysis_from_raw(response)
    
    def _extract_summary_from_raw(self, response: str) -> str:
        """从原始响应中尝试提取 summary 字段"""
        m = re.search(r'"summary"\s*:\s*"([^"]+)"', response)
        if m:
            return m.group(1)[:100]
        lines = [l.strip() for l in response.split('\n') if l.strip() and not l.strip().startswith('```')]
        for line in lines[:5]:
            if len(line) > 10 and ('{' not in line or '}' not in line):
                return line[:80]
        return response[:80].replace('\n', ' ') if response else '响应为空'
    
    def _extract_stocks_from_raw(self, response: str) -> list:
        """从原始响应中尝试提取 related_stocks"""
        stocks = []
        m = re.search(r'"related_stocks"\s*:\s*\[([^\]]+)\]', response)
        if m:
            items = re.findall(r'"([^"]+)"', m.group(1))
            stocks.extend(items[:5])
        return stocks

    def _retry_json_fix(self, broken_response: str, article: dict) -> Optional[str]:
        """Strategy 4: ask LLM to fix its own broken JSON.

        Sends a short correction prompt with the original malformed response,
        then returns the corrected text. Returns None if retry fails or times out.
        """
        title = (article or {}).get('title', '未知文章')
        retry_prompt = (
            "The following JSON has formatting errors that prevent parsing. "
            "Fix the JSON and return ONLY the corrected JSON object, "
            "no markdown fences or other text:\n\n"
            + broken_response
        )
        try:
            if self.provider == 'minimax':
                fixed = self._call_minimax_with_retry(retry_prompt, f"fix:{title[:30]}")
            elif self.client:
                completion = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": retry_prompt}],
                    max_tokens=8192,
                )
                fixed = completion.choices[0].message.content
            else:
                return None
            self.stats["retry_count"] += 1
            self.logger.info(f"JSON解析retry: {title[:40]}")
            return fixed
        except Exception as e:
            self.logger.warning(f"JSON retry 失败: {title[:40]}: {e}")
            return None

    def get_stats(self) -> dict:
        """获取分析统计"""
        return dict(self.stats)
    
    REQUIRED_ANALYSIS_FIELDS = ['summary', 'category', 'core_points', 'deep_analysis']

    def _validate_analysis(self, analysis: dict) -> bool:
        """校验 LLM 分析响应是否包含所有必需字段"""
        if not analysis:
            return False
        for field in self.REQUIRED_ANALYSIS_FIELDS:
            if field not in analysis or analysis[field] is None:
                return False
        # core_points 必须是非空列表
        if not isinstance(analysis.get('core_points'), list) or len(analysis['core_points']) == 0:
            return False
        # deep_analysis 必须是非空 dict
        if not isinstance(analysis.get('deep_analysis'), dict) or len(analysis['deep_analysis']) == 0:
            return False
        return True

    def _missing_fields(self, analysis: dict) -> list:
        """返回缺失的字段列表"""
        missing = []
        if not analysis:
            return self.REQUIRED_ANALYSIS_FIELDS
        for field in self.REQUIRED_ANALYSIS_FIELDS:
            if field not in analysis or analysis[field] is None:
                missing.append(field)
        if not isinstance(analysis.get('core_points'), list) or len(analysis.get('core_points', [])) == 0:
            missing.append('core_points(非空)')
        if not isinstance(analysis.get('deep_analysis'), dict) or len(analysis.get('deep_analysis', {})) == 0:
            missing.append('deep_analysis(非空)')
        return missing

    def _repair_analysis(self, analysis: dict, article: dict) -> dict:
        """修复不完整的分析响应，填充缺失字段"""
        if not analysis:
            analysis = {}
        defaults = {
            'summary': article.get('title', '')[:30],
            'category': '其他',
            'topic_category': '其他',
            'related_stocks': [],
            'core_points': ['[解析异常] LLM 返回不完整，请查看原文'],
            'deep_analysis': {
                'business_quality': 'LLM 返回不完整，无法生成深度分析',
                'management': 'LLM 返回不完整，无法生成深度分析',
                'key_risks': 'LLM 返回不完整，无法生成深度分析',
                'competitive_position': 'LLM 返回不完整，无法生成深度分析',
                'outlook': 'LLM 返回不完整，无法生成深度分析',
            },
        }
        for key, default in defaults.items():
            if key not in analysis or not analysis[key]:
                analysis[key] = default
        return analysis
    
    def _mock_analysis(self, article: dict) -> dict:
        """模拟分析（无 API Key 时）"""
        content = article.get('content', '')
        
        category = '其他'
        if any(kw in content for kw in ['估值', 'PE', 'PB', 'ROE', '净利润', '现金流']):
            category = '公司研究'
        elif any(kw in content for kw in ['行业', '赛道', '竞争', '格局']):
            category = '行业分析'
        elif any(kw in content for kw in ['巴菲特', '价值投资', '安全边际', '护城河']):
            category = '投资理念'
        
        scores = calculate_priority_score(article)
        
        return {
            'quality_passed': True,
            'mock': True,  # 标记为非完整分析
            'issues': ['未配置 API Key'],
            'priority': classify_priority(article),
            'scores': scores,
            'analysis': {
                'category': category,
                'topic_category': '其他',
                'related_stocks': [],
                'core_points': ['需配置 API Key 进行完整分析'],
                'summary': article.get('title', '')[:30],
                'deep_analysis': {
                    'business_quality': '请配置 API Key 后获取完整分析',
                    'management': '请配置 API Key 后获取完整分析',
                    'key_risks': '请配置 API Key 后获取完整分析',
                    'competitive_position': '请配置 API Key 后获取完整分析',
                    'outlook': '请配置 API Key 后获取完整分析'
                }
            }
        }


# ============ 报告生成器 ============

def generate_daily_report(articles: List[dict], results: List[dict], output_path: str = None,
                          crawl_stats: dict = None) -> str:
    """
    生成每日投研分析报告 v2 - 市场分组 + 操作参考
    
    Args:
        crawl_stats: 可选，爬取统计（含覆盖率信息）
    """
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 统计
    total = len(articles)
    passed = sum(1 for r in results if r.get('quality_passed'))
    failed = total - passed
    
    must_read = sum(1 for r in results if r.get('priority') == 'must_read')
    worth_reading = sum(1 for r in results if r.get('priority') == 'worth_reading')
    market_news = sum(1 for r in results if r.get('priority') == 'market_news')
    reference = sum(1 for r in results if r.get('priority') == 'reference')
    
    # 收集所有股票并按市场分组
    # fix: 同一股票多次提及去重，一只股票只列出一次，多个文章链接合并
    stock_mentions = {}
    for article, result in zip(articles, results):
        if result.get('analysis'):
            for stock in result['analysis'].get('related_stocks', []):
                stock = stock.strip()
                if not stock:
                    continue
                if stock not in stock_mentions:
                    stock_mentions[stock] = []
                stock_mentions[stock].append({
                    'title': article.get('title', ''),
                    'url': f"https://xueqiu.com/{article.get('user_id', '')}/{article.get('article_id', '')}"
                })
    
    market_groups = group_stocks_by_market(list(stock_mentions.keys()))
    
    # 今日覆盖的市场
    markets_covered = [m for m in market_groups.keys() if m != '其他']
    market_desc = '、'.join(markets_covered) if markets_covered else '暂无股票覆盖'
    
    # 检查是否有 mock 分析
    has_mock = any(r.get('mock') for r in results)

    # 构建报告
    lines = [
        f"# 📊 价值投资日报",
        "",
        f"**日期**：{today}",
        "",
    ]

    # Mock 分析警告（仅当检测到时显示）
    if has_mock:
        lines.extend([
            "## ⚠️ 分析警告",
            "",
            "❗ **未配置 API Key**，当前分析为模拟结果，不反映真实文章质量。",
            "   请在 `.env` 中配置 `BAILIAN_API_KEY` 或 `MINIMAX_API_KEY` 后重试。",
            "",
        ])

    lines.extend([
        "---",
        "",
        "## 一、概览",
        "",
        f"- **今日新增**：{total} 篇",
        f"- **有效分析**：{passed} 篇（内容 > 200 字）",
        f"- **无效文章**：{failed} 篇（短状态/评论）",
        "",
        f"- **市场覆盖**：{market_desc}",
        "",
        "---",
        "",
        "## 二、优先级分类",
        "",
        f"| 优先级 | 数量 | 说明 |",
        f"|--------|------|------|",
        f"| 🔴 必读 | {must_read} | 高质量长文，深度分析 |",
        f"| 🟡 值得关注 | {worth_reading} | 有价值的观点和分析 |",
        f"| 📰 市场资讯 | {market_news} | 收评、IPO新闻、盘面资讯 |",
        f"| 🔵 参考 | {reference} | 短状态/预告类 |",
        "",
        "---",
        "",
        "## 三、📌 今日核心观点速览",
        "",
        "### 🔴 核心观点",
        ""
    ])
    lines.append("")
    
    has_core = False
    all_risks = []
    # 先处理必读文章的核心观点
    for article, result in zip(articles, results):
        if not result.get('quality_passed') or not result.get('analysis'):
            continue
        priority = result.get('priority', 'reference')
        if priority not in ['must_read', 'worth_reading']:
            continue
            
        analysis = result['analysis']
        author = article.get('author', '匿名')
        summary = analysis.get('summary', '')
        stocks = analysis.get('related_stocks', [])
        stock_str = '、'.join(stocks[:2]) if stocks else ''
        
        if summary:
            if stock_str:
                lines.append(f"- **{author}**「{stock_str}」：{summary}")
            else:
                lines.append(f"- **{author}**：{summary}")
            has_core = True
        
        # 收集关键风险
        da = analysis.get('deep_analysis', {})
        key_risk = da.get('key_risks', '')
        if key_risk and len(key_risk) > 10:
            all_risks.append(key_risk[:120])
    
    if not has_core:
        lines.append("暂无核心观点提炼")
    
    # 风险提示
    lines.append("")
    lines.append("### ⚠️ 风险与机会")
    lines.append("")
    if all_risks:
        for risk in list(set(all_risks))[:5]:
            lines.append(f"- {risk}{'...' if len(risk)>=120 else ''}")
    else:
        lines.append("今日无集中风险提示")
    
    # 新增标的提示（这里暂时简单实现，后续可以比对自选股）
    lines.append("")
    lines.append("### 🆕 关注方向")
    lines.append("")
    hot_topics = {}
    for article, result in zip(articles, results):
        if not result.get('quality_passed') or not result.get('analysis'):
            continue
        analysis = result['analysis']
        category = analysis.get('category', '')
        if category and category not in ['其他', '未分类']:
            hot_topics[category] = hot_topics.get(category, 0) + 1
    if hot_topics:
        top_topics = sorted(hot_topics.items(), key=lambda x: -x[1])[:5]
        for topic, cnt in top_topics:
            lines.append(f"- {topic}（{cnt}篇文章讨论）")
    else:
        lines.append("今日无集中热门话题")
    
    # 大V今日动态
    lines.append("")
    lines.append("### 👤 大V今日动态")
    lines.append("")
    author_stats = {}
    for article, result in zip(articles, results):
        if not result.get('quality_passed'):
            continue
        author = article.get('author', '匿名')
        if author not in author_stats:
            author_stats[author] = {'count': 0, 'topics': set(), 'is_media': False}
        author_stats[author]['count'] += 1
        topic = result.get('analysis', {}).get('topic_category', '')
        if topic and topic != '其他':
            author_stats[author]['topics'].add(topic)
        # 媒体号识别
        if author in ['港股解码', '海豚研究君', '腾讯新闻', '新浪财经', '格隆汇']:
            author_stats[author]['is_media'] = True
    
    # 按发文数排序
    sorted_authors = sorted(author_stats.items(), key=lambda x: -x[1]['count'])
    for author, stat in sorted_authors:
        media_tag = ' [资讯]' if stat['is_media'] else ''
        topics_str = '、'.join(list(stat['topics'])[:3]) if stat['topics'] else '综合资讯'
        lines.append(f"- **{author}**{media_tag}（{stat['count']}篇）：{topics_str}")
    
    lines.append("")
    
    # 文章集合（按优先级排序）
    lines.append("---")
    lines.append("")
    lines.append("## 四、文章详情")
    lines.append("")
    
    priorities = {'must_read': [], 'worth_reading': [], 'market_news': [], 'reference': []}
    for article, result in zip(articles, results):
        priority = result.get('priority', 'reference')
        priorities[priority].append((article, result))
    
    if priorities['must_read']:
        lines.append("### 🔴 必读")
        lines.append("")
        for i, (article, result) in enumerate(priorities['must_read'], 1):
            lines.extend(_format_article(i, article, result))
    
    if priorities['worth_reading']:
        lines.append("### 🟡 值得关注")
        lines.append("")
        
        # 按主题分组
        TOPIC_EMOJI = {
            '科技/AI/互联网': '🚀',
            '新能源/制造业': '🏭',
            '医药/医疗': '🏥',
            '消费/零售': '🛒',
            '金融/地产/宏观': '🏦',
            '海外市场': '🌏',
            'IPO/新股': '📝',
            '市场策略/投资理念': '📊',
            '商业航天': '🛰️',
            '其他': '📌',
        }
        
        groups = {}
        for article, result in priorities['worth_reading']:
            topic = result.get('analysis', {}).get('topic_category', '其他')
            if topic not in groups:
                groups[topic] = []
            groups[topic].append((article, result))
        
        # 按组内文章总分从高到低排序
        def group_score(group_items):
            return sum(r['scores']['total'] for _, r in group_items)
        sorted_topics = sorted(groups.keys(), key=lambda t: -group_score(groups[t]))
        
        article_idx = 1
        for topic in sorted_topics:
            group_items = groups[topic]
            emoji = TOPIC_EMOJI.get(topic, '📌')
            # 组内文章按单篇分数从高到低
            group_items_sorted = sorted(group_items, key=lambda x: -x[1]['scores']['total'])
            lines.append(f"#### {emoji} {topic}（{len(group_items_sorted)}篇）")
            lines.append("")
            for article, result in group_items_sorted:
                lines.extend(_format_article(article_idx, article, result))
                article_idx += 1
            lines.append("")
    
    if priorities['market_news']:
        lines.append("### 📰 市场资讯")
        lines.append("")
        for i, (article, result) in enumerate(priorities['market_news'], 1):
            lines.extend(_format_article_brief(i, article, result))
    
    if priorities['reference']:
        lines.append("### 🔵 参考")
        lines.append("")
        for i, (article, result) in enumerate(priorities['reference'], 1):
            lines.extend(_format_article_brief(i, article, result))
    
    # 总结
    categories = set(r['analysis']['category'] for r in results if r.get('analysis'))
    hot_stocks = list(stock_mentions.keys())[:5]
    lines.extend([
        "",
        "---",
        "",
        "## 五、今日总结",
        "",
        f"- **主要话题**：{', '.join(categories) if categories else '暂无'}",
        f"- **热门股票**：{', '.join(hot_stocks) if hot_stocks else '无'}",
        "",
        "---",
        "",
        f"*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        "*分析模型：MiniMax M3*",
        "",
    ])

    # 爬取覆盖率（当 crawl_stats 可用时）
    if crawl_stats:
        total = crawl_stats.get('total_users', 0)
        ok = crawl_stats.get('successful', 0)
        fail = crawl_stats.get('failed', 0)
        if fail:
            status = f"⚠️ {ok}/{total} 账号成功，{fail} 个失败"
        else:
            status = f"✅ {ok}/{total} 账号全部成功"
        lines.extend([
            f"*爬取状态：{status}*",
            f"*新增文章：{crawl_stats.get('new_articles', 0)} 篇*",
        ])
    
    report = '\n'.join(lines)
    
    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"报告已保存: {output_path}")
    
    return report


def _format_article(index: int, article: dict, result: dict) -> List[str]:
    """格式化文章详情"""
    lines = []
    
    title = article.get('title', '无标题')
    author = article.get('author', '未知')
    article_id = article.get('article_id', '')
    user_id = article.get('user_id', '')
    url = f"https://xueqiu.com/{user_id}/{article_id}"
    word_count = len(article.get('content', ''))
    
    lines.append(f"#### {index}. {title}")
    lines.append("")
    lines.append(f"- **作者**：{author}")
    lines.append(f"- **链接**：{url}")
    lines.append(f"- **字数**：{word_count} 字")
    
    # 显示评分信息
    scores = result.get('scores')
    if scores:
        lines.append(f"- **评分**：{scores.get('total', 0)} 分（内容{scores.get('content_depth', 0)}+关键词{scores.get('keywords', 0)}+作者{scores.get('author_bonus', 0)}+归类{scores.get('category', 0)}+观点{scores.get('core_points', 0)}+标题{scores.get('title_quality', 0)}）")
    
    analysis = result.get('analysis')
    if analysis:
        # 相关股票
        stocks = analysis.get('related_stocks', [])
        if stocks:
            lines.append(f"- **相关股票**：{', '.join(stocks)}")
        
        lines.append("")
        lines.append("**MiniMax M3 分析：**")
        lines.append("")
        
        # 主题归类
        lines.append(f"- **主题归类**：{analysis.get('category', '其他')}")
        
        # 核心观点
        core_points = analysis.get('core_points', [])
        if core_points:
            lines.append("")
            lines.append("**核心观点：**")
            for j, point in enumerate(core_points[:5], 1):
                lines.append(f"  {j}. {point}")
        
        # 深度分析
        da = analysis.get('deep_analysis', {})
        if da:
            lines.append("")
            lines.append("**深度评价（价值投资视角）：**")
            
            bq = da.get('business_quality', '')
            if bq:
                lines.append("")
                lines.append("📍 **商业模式**")
                lines.append(f"  {bq}")
            
            mgmt = da.get('management', '')
            if mgmt:
                lines.append("")
                lines.append("👔 **管理层**")
                lines.append(f"  {mgmt}")
            
            risks = da.get('key_risks', '')
            if risks:
                lines.append("")
                lines.append("⚠️ **关键风险**")
                lines.append(f"  {risks}")
            
            cp = da.get('competitive_position', '')
            if cp:
                lines.append("")
                lines.append("🏔️ **竞争格局**")
                lines.append(f"  {cp}")
            
            outlook = da.get('outlook', '')
            if outlook:
                lines.append("")
                lines.append("🔭 **后续关注**")
                lines.append(f"  {outlook}")
        
        # 一句话总结
        summary = analysis.get('summary', '')
        if summary:
            lines.append("")
            lines.append(f"**总结**：{summary}")
    
    lines.append("")
    lines.append("---")
    lines.append("")
    
    return lines


def _format_article_brief(index: int, article: dict, result: dict) -> List[str]:
    """格式化文章简要信息"""
    lines = []
    
    title = article.get('title', '无标题')
    author = article.get('author', '未知')
    article_id = article.get('article_id', '')
    user_id = article.get('user_id', '')
    url = f"https://xueqiu.com/{user_id}/{article_id}"
    
    lines.append(f"{index}. [{title}]({url})（{author}）")
    
    if not result.get('quality_passed'):
        lines.append(f"   - ⚠️ {', '.join(result.get('issues', []))}")
    
    return lines


if __name__ == '__main__':
    # 测试
    analyzer = ArticleAnalyzer()
    
    test_article = {
        'title': '中海油估值分析',
        'author': 'czy710',
        'user_id': '6308001210',
        'article_id': '123456',
        'content': '2024年布油均价80美元下，中海油估值分析...' * 100
    }
    
    result = analyzer.analyze_article(test_article)
    print(json.dumps(result, ensure_ascii=False, indent=2))