"""插件完整性守卫：让「限速整层静默消失」在 CI 就红掉。

为什么需要这个文件：opencli 的 discovery 是**平铺急加载**（`dist/src/discovery.js:26,233-247`），
它按正则 `\\b(?:cli|onStartup|onBeforeExecute|onAfterExecute)\\s*\\(` 判断顶层 `.js` 是不是
插件模块，而 **import 失败只打一条 warn 就被静默跳过** —— 也就是说，`rate-limit.js` 里
一个导出名写错，整套限速与请求日志会在没有任何告警的情况下失效，直到有人翻 `opencli list`。

这不是假想：2026-10-12 把 `rate-limit.js` 拆成 `lib/` 时，实际连续踩了两次 ——
`Duplicate export of 'hostSiteFor'`（既在声明处 export、又列进 `export {}`）与
`Export 'asNumber' is not defined in module`（删了 import 却留着再导出）。
两次都让限速器在生产里失效，靠手工 grep `opencli list` 才发现。

本文件只做**静态**检查（不需要 node、不需要 `@jackwener/opencli` 依赖，CI 跑得动）：
  · 每个 `export {}` 里的名字都能解析到「声明」或「import」，且不与声明处的 export 重复；
  · 每个相对 import 的目标文件存在；
  · 钩子只在一个顶层文件里注册，且该文件仍带 `onBeforeExecute(` 字样；
  · `lib/` 下的模块必须被入口引用（子目录不被 discovery 扫，漏 import 就是死代码）。
"""

from __future__ import annotations

import re
from pathlib import Path

from plugin_source import resolve_plugin_dir  # noqa: E402

PLUGIN_DIR = resolve_plugin_dir()
ENTRY = PLUGIN_DIR / "rate-limit.js"

_DECL = re.compile(
    r"(?:export\s+)?(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)"
)
_IMPORT_NAMED = re.compile(r"import\s*\{([^}]*)\}\s*from")
_IMPORT_DEFAULT = re.compile(r"import\s+([A-Za-z_$][\w$]*)\s*(?:,|from)")
_EXPORT_BLOCK = re.compile(r"^export\s*\{([^}]*)\}", re.M)
_EXPORT_INLINE = re.compile(
    r"export\s+(?:async\s+)?(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)"
)
_IMPORT_FROM = re.compile(r"""from\s+['"](\.[^'"]+)['"]""")
_HOOK_REGISTRATION = re.compile(r"\bonBeforeExecute\s*\(")
_HOOK_REGISTRATION_OTHER = re.compile(r"\bonAfterExecute\s*\(")


def _plugin_files() -> list[Path]:
    return sorted(PLUGIN_DIR.glob("*.js")) + sorted((PLUGIN_DIR / "lib").glob("*.js"))


def test_every_exported_name_resolves():
    """`export {}` 里的名字必须能解析，且不与声明处 export 重复.

    重复导出会让整个模块加载失败 —— 而 discovery 只会静默跳过它。
    """
    problems: list[str] = []
    for path in _plugin_files():
        src = path.read_text(encoding="utf-8")
        imported: set[str] = set()
        for match in _IMPORT_NAMED.finditer(src):
            for raw in match.group(1).split(","):
                name = raw.strip().split(" as ")[-1].strip()
                if name:
                    imported.add(name)
        for match in _IMPORT_DEFAULT.finditer(src):
            imported.add(match.group(1))
        declared = set(_DECL.findall(src))
        inline = set(_EXPORT_INLINE.findall(src))

        for match in _EXPORT_BLOCK.finditer(src):
            for raw in match.group(1).split(","):
                name = raw.strip()
                if not name:
                    continue
                if name in inline:
                    problems.append(
                        f"{path.name}: `{name}` 已在声明处 export，又出现在 export{{}} → "
                        "Duplicate export（模块会加载失败、限速静默失效）"
                    )
                elif name not in declared and name not in imported:
                    problems.append(
                        f"{path.name}: `{name}` 既未声明也未 import → "
                        "Export not defined（模块会加载失败、限速静默失效）"
                    )

    assert not problems, "\n".join(problems)


def test_relative_imports_point_at_existing_files():
    """相对 import 的目标必须存在 —— 拼错路径同样是静默失效。"""
    problems: list[str] = []
    for path in _plugin_files():
        src = path.read_text(encoding="utf-8")
        for spec in _IMPORT_FROM.findall(src):
            target = (path.parent / spec).resolve()
            if not target.is_file():
                problems.append(
                    f"{path.name}: import '{spec}' 指向不存在的文件 {target}"
                )
    assert not problems, "\n".join(problems)


def test_only_the_entry_registers_hooks_and_keeps_discovery_marker():
    """钩子只在入口注册一次，且入口仍带 discovery 认的 `onBeforeExecute(` 字样.

    两处注册 → 每次调用被限速两遍（等待翻倍、可能顶破调用方超时）；
    入口丢了那个字样 → discovery 不加载它 → 限速整层消失。
    """
    registrars = [
        p.name
        for p in _plugin_files()
        if _HOOK_REGISTRATION.search(p.read_text(encoding="utf-8"))
    ]
    assert registrars == [ENTRY.name], (
        f"注册 onBeforeExecute 的文件应只有入口一个，实际: {registrars}"
    )
    assert ENTRY.exists(), "入口 rate-limit.js 不见了"

    # onAfterExecute（并发槽释放 / 请求日志收尾用）同样只允许入口注册；
    # 目前还没接，所以「一个都没有」也是合法的 —— 但绝不能在别处偷偷接上。
    after_registrars = [
        p.name
        for p in _plugin_files()
        if _HOOK_REGISTRATION_OTHER.search(p.read_text(encoding="utf-8"))
    ]
    assert after_registrars in ([], [ENTRY.name]), (
        f"注册 onAfterExecute 的文件只能是入口，实际: {after_registrars}"
    )


def test_lib_modules_are_referenced_by_the_entry():
    """`lib/` 不被 discovery 扫（它只平铺读顶层 .js），漏 import 就是死代码."""
    lib_dir = PLUGIN_DIR / "lib"
    if not lib_dir.is_dir():
        return
    entry_src = ENTRY.read_text(encoding="utf-8")
    # 入口（含其 lib 依赖）里的 import 图——这里只要求直接被入口或某个 lib 引用到即可
    all_src = "\n".join(p.read_text(encoding="utf-8") for p in _plugin_files())
    orphans = [
        p.name
        for p in sorted(lib_dir.glob("*.js"))
        if f"./{p.name}" not in all_src and f"lib/{p.name}" not in all_src
    ]
    assert not orphans, f"lib/ 下这些模块没有任何地方 import：{orphans}"
    assert "lib/" in entry_src or "./lib" in entry_src, "入口没有引用任何 lib/ 模块"
