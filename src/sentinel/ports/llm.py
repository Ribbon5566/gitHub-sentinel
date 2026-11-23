from __future__ import annotations

from typing import Protocol

from ..domain.completion import CompletionRequest, CompletionResult
from ..domain.digest import DigestInput, DigestSummary


class LLMProvider(Protocol):
    """语言能力的抽象，两种形态：

    * summarize —— 结构化摘要（JSON 输出，字段固定），供周期简报使用；
    * complete  —— 自由文本长文，供每日报告使用。

    契约要点：实现必须返回可用结果；无法完成时应抛 LLMError，
    由 application 层决定是否降级，端口实现自己不要偷偷返回空对象。
    """

    name: str

    async def summarize(self, request: DigestInput) -> DigestSummary: ...

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """生成自由文本。失败必须抛 LLMError，不得返回空字符串冒充成功。"""
        ...

    async def aclose(self) -> None: ...
