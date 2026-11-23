from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_checker():
    path = ROOT / "scripts" / "check_layers.py"
    spec = importlib.util.spec_from_file_location("check_layers", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_layer_boundaries_are_respected() -> None:
    """domain 不许依赖任何上层；ports 只许依赖 domain；infrastructure 不许碰 application/api。"""
    checker = _load_checker()
    violations = checker.check(ROOT / "src" / "sentinel")
    assert violations == [], "分层违规:\n" + "\n".join(violations)


def test_domain_is_framework_free() -> None:
    """domain 里出现 sqlalchemy / fastapi / httpx 就说明边界破了。"""
    forbidden = {"sqlalchemy", "fastapi", "httpx", "jinja2", "apscheduler", "aiosmtplib"}
    offenders: list[str] = []
    for path in (ROOT / "src" / "sentinel" / "domain").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            if f"import {name}" in text or f"from {name}" in text:
                offenders.append(f"{path.name}: {name}")
    assert offenders == []
