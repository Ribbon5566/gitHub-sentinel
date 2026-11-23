#!/usr/bin/env python
"""分层守护：用 AST 扫 import，检查依赖方向是否被破坏。

规则（只列「允许」的边，其余一律算违规）：

    domain          → （无内部依赖）
    ports           → domain
    application     → domain, ports (+ 顶层 config)
    infrastructure  → domain, ports
    api             → domain, ports, application (+ 顶层 config)
    顶层组合根      → 任意

为什么不引 import-linter：多一个依赖、多一份配置，而这里的规则只有 6 行。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

STDLIB = set(sys.stdlib_module_names) | {"__future__"}

LAYERS = ("domain", "ports", "application", "infrastructure", "api")
ROOT = "root"

ALLOWED: dict[str, set[str]] = {
    "domain": set(),
    "ports": {"domain"},
    "application": {"domain", "ports"},
    "infrastructure": {"domain", "ports"},
    "api": {"domain", "ports", "application"},
    ROOT: set(LAYERS) | {ROOT},
}

# 顶层模块里允许被下层引用的白名单。
#   config  —— 纯值对象（只依赖 domain），谁都可以读配置；
#   container —— 仅 api 用作依赖注入的容器类型（Depends 需要具体类型），不直接调实现。
ALLOWED_ROOT_MODULES: dict[str, set[str]] = {
    "domain": set(),
    "ports": set(),
    "application": {"config"},
    "infrastructure": {"config"},
    "api": {"config", "container"},
}

ALLOWED_THIRD_PARTY = {
    "domain": {"pydantic"},
    "ports": set(),
}


def _module_layer(parts: tuple[str, ...]) -> str:
    return parts[0] if parts and parts[0] in LAYERS else ROOT


def _resolve(package: tuple[str, ...], level: int, module: str | None) -> tuple[str, ...]:
    """把相对 import 解析成 sentinel 包内的绝对路径（返回值不含包名 sentinel）。

    package 是「当前文件所在包」：pkg/mod.py 与 pkg/__init__.py 都是 ("pkg",)。
    """
    if level == 0:
        if not module:
            return ()
        chunks = tuple(module.split("."))
        return chunks[1:] if chunks and chunks[0] == "sentinel" else ()

    base = list(package)
    for _ in range(level - 1):
        if base:
            base.pop()
    if module:
        base.extend(module.split("."))
    return tuple(base)


def _imports(path: Path, root: Path) -> list[tuple[tuple[str, ...], bool]]:
    """返回 [(模块路径, 是否外部包)]。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[tuple[str, ...], bool]] = []
    file_parts = path.relative_to(root).with_suffix("").parts
    package = tuple(file_parts[:-1])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sentinel" or alias.name.startswith("sentinel."):
                    found.append((tuple(alias.name.split("."))[1:], False))
                else:
                    found.append(((alias.name.split(".")[0],), True))
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                found.append((_resolve(package, node.level, node.module), False))
            elif node.module and (node.module == "sentinel" or node.module.startswith("sentinel.")):
                found.append((tuple(node.module.split("."))[1:], False))
            elif node.module:
                found.append(((node.module.split(".")[0],), True))
    return found


def check(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        layer = _module_layer(rel.parts)
        for target, external in _imports(path, root):
            if external:
                top = target[0] if target else ""
                if top in STDLIB:
                    continue
                allowed = ALLOWED_THIRD_PARTY.get(layer)
                if allowed is not None and top not in allowed:
                    violations.append(
                        f"{rel}: {layer} 层不允许依赖第三方包 {top}"
                    )
                continue

            target_layer = _module_layer(target)
            if target_layer == layer:
                continue
            if target_layer == ROOT:
                if target and target[0] in ALLOWED_ROOT_MODULES.get(layer, set()):
                    continue
                violations.append(
                    f"{rel}: {layer} 层禁止 import 顶层模块 {'/'.join(target)}"
                )
                continue
            if target_layer not in ALLOWED[layer]:
                violations.append(
                    f"{rel}: {layer} 层禁止 import {target_layer} 层"
                    f"（{'/'.join(target) or 'sentinel'}）"
                )
    return violations


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "src" / "sentinel"
    violations = check(root)
    if not violations:
        print(f"OK: 分层检查通过（{root}）")
        return 0
    print(f"FAIL: 发现 {len(violations)} 处分层违规")
    for item in violations:
        print(f"  - {item}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
