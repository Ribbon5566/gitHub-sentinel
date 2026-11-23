"""LLM 自由文本生成的领域契约。

放在 domain 而不是 ports，是因为 scripts/check_layers.py 规定 ports 层
不许依赖任何第三方包，而这两个结构需要 pydantic 校验。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

SYSTEM_PROMPT_FIELD_LIMIT = 8000


class CompletionRequest(BaseModel):
    """一次纯文本生成请求。

    不带 `thinking` / `response_format` 这类供应商参数：它们是适配器的事，
    由 Settings 统一控制，领域层不该知道 DeepSeek 和 OpenAI 的参数差异。
    """

    model_config = ConfigDict(frozen=True)

    system: str
    user: str
    max_tokens: int = 1600
    temperature: float = 0.2
    json_mode: bool = False


class CompletionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()
