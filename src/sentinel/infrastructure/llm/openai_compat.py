"""OpenAI 兼容协议适配器（DeepSeek / Qwen / Ollama / OpenAI 均可）。

不用官方 SDK：只有一个 POST，引 SDK 反而多一层版本耦合。
Stage 1 未纳入自动化测试覆盖，Stage 3 用 respx 补齐。
"""

from __future__ import annotations

import httpx

from ...domain.digest import DigestInput, DigestSummary
from ...domain.errors import LLMError
from .parsing import parse_summary
from .precompress import render_compact
from .prompts import SYSTEM_PROMPT, build_user_prompt


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
        self._client = httpx.AsyncClient(timeout=timeout)

    async def summarize(self, request: DigestInput) -> DigestSummary:
        compact = render_compact(request, max_chars=self._max_input_chars)
        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(request, compact)},
            ],
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc

        return parse_summary(content, model=self._model)

    async def aclose(self) -> None:
        await self._client.aclose()
