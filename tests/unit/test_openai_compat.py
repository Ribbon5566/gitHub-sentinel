from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from sentinel.domain.completion import CompletionRequest
from sentinel.domain.digest import DigestInput
from sentinel.domain.enums import DigestStyle
from sentinel.domain.errors import LLMError
from sentinel.infrastructure.llm.openai_compat import OpenAICompatLLM

BASE = "https://api.deepseek.com/v1"
ENDPOINT = f"{BASE}/chat/completions"


def build(**overrides) -> OpenAICompatLLM:
    params = {
        "base_url": BASE,
        "api_key": "sk-test",
        "model": "deepseek-flash",
        "thinking": "disabled",
    }
    params.update(overrides)
    return OpenAICompatLLM(**params)


def request_payload() -> CompletionRequest:
    return CompletionRequest(
        system="你是研发负责人。",
        user="以下是进展。",
        max_tokens=800,
        temperature=0.3,
    )


def reply(content: str, **extra) -> httpx.Response:
    body = {
        "model": "deepseek-flash",
        "choices": [
            {"message": {"content": content}, "finish_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 80},
    }
    body.update(extra)
    return httpx.Response(200, json=body)


async def test_complete_sends_disabled_thinking_and_temperature() -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.post("/chat/completions").mock(return_value=reply("报告正文"))
        llm = build()
        try:
            result = await llm.complete(request_payload())
        finally:
            await llm.aclose()

    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 800
    assert body["stream"] is False
    assert result.text == "报告正文"
    assert result.prompt_tokens == 120
    assert result.finish_reason == "stop"


async def test_thinking_enabled_omits_temperature() -> None:
    """思考模式下 temperature 不生效，部分兼容服务会因此报 400，干脆不发。"""
    with respx.mock(base_url=BASE) as router:
        route = router.post("/chat/completions").mock(return_value=reply("思考结果"))
        llm = build(thinking="enabled")
        try:
            await llm.complete(request_payload())
        finally:
            await llm.aclose()

    body = json.loads(route.calls[0].request.content)
    assert body["thinking"] == {"type": "enabled"}
    assert "temperature" not in body


async def test_thinking_default_leaves_field_out() -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.post("/chat/completions").mock(return_value=reply("默认"))
        llm = build(thinking="default")
        try:
            await llm.complete(request_payload())
        finally:
            await llm.aclose()

    body = json.loads(route.calls[0].request.content)
    assert "thinking" not in body
    assert body["temperature"] == 0.3


async def test_http_error_becomes_llm_error_with_body() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(400, text='{"error":"unknown model deepseek-chat"}')
        )
        llm = build()
        try:
            with pytest.raises(LLMError) as excinfo:
                await llm.complete(request_payload())
        finally:
            await llm.aclose()

    message = str(excinfo.value)
    assert "400" in message
    assert "unknown model" in message


async def test_empty_content_is_an_error_not_an_empty_report() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/chat/completions").mock(return_value=reply("   "))
        llm = build()
        try:
            with pytest.raises(LLMError):
                await llm.complete(request_payload())
        finally:
            await llm.aclose()


async def test_missing_choices_is_an_error() -> None:
    with respx.mock(base_url=BASE) as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": []})
        )
        llm = build()
        try:
            with pytest.raises(LLMError):
                await llm.complete(request_payload())
        finally:
            await llm.aclose()


async def test_json_mode_can_be_disabled() -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.post("/chat/completions").mock(return_value=reply("正文"))
        llm = build(json_mode=False)
        try:
            await llm.complete(
                CompletionRequest(system="s", user="u", json_mode=True, max_tokens=100)
            )
        finally:
            await llm.aclose()

    assert "response_format" not in json.loads(route.calls[0].request.content)


async def test_summarize_still_requests_json_object() -> None:
    digest_input = DigestInput(
        repo_full_name="acme/widgets",
        repo_title="Widgets",
        period_start=datetime(2026, 11, 23, tzinfo=UTC),
        period_end=datetime(2026, 11, 23, 12, tzinfo=UTC),
        style=DigestStyle.BRIEF,
    )
    payload = {"headline": "标题", "overview_md": "概述", "highlights": ["要点"], "risks": []}
    with respx.mock(base_url=BASE) as router:
        route = router.post("/chat/completions").mock(
            return_value=reply("```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")
        )
        llm = build()
        try:
            summary = await llm.summarize(digest_input)
        finally:
            await llm.aclose()

    body = json.loads(route.calls[0].request.content)
    assert body["response_format"] == {"type": "json_object"}
    assert summary.headline == "标题"
    assert summary.model == "deepseek-flash"
