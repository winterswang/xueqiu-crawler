"""标题可用性判定 —— 站点栏目标题 / 列表占位符都不算「文章标题」。

为什么需要
----------
雪球详情页在**没渲染出文章标题**时，`document.title` 会退回站点栏目标题
``雪球-聪明的投资者都在这里``；列表页还会把截断提示 ``展开`` 连同图标字体字符
（``\\ue63c``）一起带出来。两者都会被当成真标题写进 ``index.json`` 与 ``.md``，
最终在日报里显示成一篇文章。

实测（2026-10-09）：``data/index.json`` 里 7 条标题是 ``雪球-聪明的投资者都在这里``，
全部来自同一个只发图不发字的账号（永庆好公司）。今天那篇还顺着
``sync_raw_articles_to_ima.py`` 上传进了 IMA 知识库。

为什么单开一个模块、不并进 `xueqiu_analyzer.waf`
------------------------------------------------
``is_error_page`` 已经拦得住「标题为空」；这里补的是**标题非空、但根本不是标题**
这一格。不并进 waf.py 是因为那个模块的契约是**风控拦截**：它的判定会连带触发
重试 / 重启浏览器，并计入 ``blocked_articles``。站点栏目标题是**渲染不全** ——
页面并没有被拦，把它算成 WAF 会让刚理顺的拦截口径重新变脏。

只做**精确匹配**，不做子串：标题里出现「雪球」两个字的正常文章多得是。
"""

from __future__ import annotations

import re

# 详情页没渲染出文章标题时 `document.title` 回落的站点栏目标题。
SITE_CHROME_TITLES = frozenset({"雪球-聪明的投资者都在这里", "雪球"})

# 列表页的截断提示被当成标题抓下来时的形态。
LIST_PLACEHOLDER_TITLES = frozenset({"展开", "全文", "阅读全文"})

# 图标字体用的私有区字符（U+E000–U+F8FF）：列表页的截断提示「展开」后面
# 会跟一个 U+E63C 图标字形。不清掉的话，`展开\ue63c` 就落不进上面那条精确匹配。
_PUA_CHARS = re.compile(r'[\ue000-\uf8ff]')


def normalize_title(title: str) -> str:
    """去掉首尾空白与图标字体字符。"""
    return _PUA_CHARS.sub('', title or '').strip()


def is_usable_title(title: str) -> bool:
    """这个字符串能不能当文章标题用。"""
    t = normalize_title(title)
    return bool(t) and t not in SITE_CHROME_TITLES and t not in LIST_PLACEHOLDER_TITLES


def resolve_title(detail_title: str, list_title: str) -> str:
    """详情标题优先，它不可用时退回列表标题；都不可用返回空串.

    不能写成 ``detail or list`` —— 空串才会走 ``or`` 的右边，而这里要拦的是
    ``雪球-聪明的投资者都在这里`` 这种**非空但没用**的值。
    """
    for candidate in (detail_title, list_title):
        if is_usable_title(candidate):
            return normalize_title(candidate)
    return ""
