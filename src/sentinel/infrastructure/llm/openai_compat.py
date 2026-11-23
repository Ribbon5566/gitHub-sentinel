"""OpenAI 兼容协议适配器（DeepSeek / Qwen / Ollama / OpenAI 均可）。

不用官方 SDK：只有一个 POST，引 SDK 反而多一层版本耦合。
Stage 3 用 respx 补齐了测试，现在两类调用（结构化摘要 / 自由长文）都走同一个 `_chat`。

DeepSeek 的两个坑写在这里，避免以后又被踩：
  1. 当前模型名是 `deepseek-flash`，`deepseek-chat` / `deepseek-reasoner` 已进入下线计划。
  2. thinking 模式（默认开启）下 `temperature` 不生效，且显式关闭时要用
     `"thinking": {"type": "disabled"}`；不传则跟随服务端默认，可能莫名变慢变贵。
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from ...domain.completion import CompletionRequest, CompletionResult
from ...domain.digest import DigestInput, DigestSummary
from ...domain.errors import LLMError
from .parsing import parse_summary
from .precompress import render_compact
from .prompts import SYSTEM_PROMPT, build_user_prompt

ThinkingMode = Literal["default", "enabled", "disabled"]


class OpenAICompatLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: float = 60.0,
        max_input_chars: int = 12000,
        temperature: float = 0.2,
        thinking: ThinkingMode = "disabled",
        json_mode: bool = True,
        report_timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise LLMError("openai_compat 需要 SENTINEL_LLM_API_KEY，缺失时请改用 mock")
        self.name = f"openai_compat:{model}"
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_input_chars = max_input_chars
        self._temperature = temperature
        self._thinking = thinking
        self._json_mode = json_mode
        self._report_timeout = report_timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def summarize(self, request: DigestInput) -> DigestSummary:
        compact = render_compact(request, max_chars=self._max_input_chars)
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(request, compact)},
            ],
        }
        payload.update(self._tuning(json_mode=True, temperature=self._temperature))
        data = await self._chat(payload, call_timeout=self._timeout)
        content, _ = self._extract(data)
        return parse_summary(content, model=self._model)

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        payload.update(
            self._tuning(
                json_mode=request.json_mode and self._json_mode,
                temperature=request.temperature,
            )
        )
        data = await self._chat(payload, call_timeout=self._report_timeout)
        content, meta = self._extract(data)
        return CompletionResult(text=content, **meta)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ 内部
    def _tuning(self, *, json_mode: bool, temperature: float) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self._thinking == "enabled":
            # 思考模式下 temperature 不生效，部分兼容服务会直接报 400，干脆不发
            extra["thinking"] = {"type": "enabled"}
        elif self._thinking == "disabled":
            extra["thinking"] = {"type": "disabled"}
            extra["temperature"] = temperature
        else:
            extra["temperature"] = temperature
        if json_mode:
            extra["response_format"] = {"type": "json_object"}
        return extra

    async def _chat(self, payload: dict[str, Any], *, call_timeout: float) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=call_timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 调用失败（网络）：{exc}") from exc

        if response.status_code >= 400:
            body = " ".join(response.text[:300].split())
            raise LLMError(f"LLM 调用失败：HTTP {response.status_code} {body}")
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("LLM 返回非 JSON 响应") from exc
        if not isinstance(data, dict):
            raise LLMError("LLM 响应不是 JSON 对象")
        return data

    def _extract(self, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应缺少 choices[0].message.content：{str(data)[:300]}") from exc
        if not isinstance(content, str) or not content.strip():
            # 空内容当失败处理，否则上层会落一份空报告
            raise LLMError("LLM 返回空内容")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        meta = {
            "model": str(data.get("model") or self._model),
            "finish_reason": str(choice.get("finish_reason") or ""),
            "prompt_tokens": _int(usage.get("prompt_tokens")),
            "completion_tokens": _int(usage.get("completion_tokens")),
        }
        return content, meta


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
