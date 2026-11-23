from __future__ import annotations

from typing import Protocol

from ..domain.digest import DigestInput, DigestSummary


class LLMProvider(Protocol):
    """摘要能力的抽象。

    契约要点：实现必须返回结构化 DigestSummary；无法完成时应抛 LLMError，
    由 application 层决定是否降级，端口实现自己不要偷偷返回空对象。
    """

    name: str

    async def summarize(self, request: DigestInput) -> DigestSummary: ...

    async def aclose(self) -> None: ...
