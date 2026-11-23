"""结构化输出解析。

模型返回 JSON 是「大概率」而不是「一定」：可能带 ```json 围栏、可能前面加一句
「好的，以下是摘要」。这里做容错提取，最终仍用 pydantic 校验，解析失败就抛
LLMError，交给上层降级——绝不让半个 json 流进报告。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ...domain.digest import DigestSummary
from ...domain.errors import LLMError

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_object(text: str) -> dict[str, Any]:
    fenced = _FENCE.search(text)
    candidate = fenced.group(1) if fenced else text
    candidate = candidate.strip()

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("模型输出里找不到 JSON 对象") from None
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型输出不是合法 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise LLMError("模型输出的 JSON 顶层不是对象")
    return data


def parse_summary(text: str, *, model: str = "") -> DigestSummary:
    data = _extract_json_object(text)

    def as_list(value: Any, limit: int = 8) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value.strip(),) if value.strip() else ()
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            return tuple(items[:limit])
        return ()

    summary = DigestSummary(
        headline=str(data.get("headline", "")).strip()[:400],
        overview_md=str(data.get("overview_md", "")).strip(),
        highlights=as_list(data.get("highlights")),
        risks=as_list(data.get("risks")),
        model=model,
    )
    if not summary.is_empty:
        return summary
    raise LLMError("模型返回的摘要为空")
