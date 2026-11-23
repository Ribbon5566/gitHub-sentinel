"""端口层：进程外部世界的唯一抽象边界。

这里只有 Protocol 和跨层传输用的数据类，没有实现。
依赖方向永远是 infrastructure → ports ← application，端口本身只认识 domain。
"""

from .clock import Clock
from .github import FetchQuery, GitHubPort, Page, RateStatus
from .llm import LLMProvider
from .notifier import Notifier, NotifyMessage, NotifyResult
from .renderer import EmailContent, RenderedReport, ReportContext, ReportRendererPort
from .repositories import (
    CursorRepo,
    EventRepo,
    InsertResult,
    NotifyLogRepo,
    ReportRepo,
    RunRepo,
    SubscriptionRepo,
)

__all__ = [
    "Clock",
    "CursorRepo",
    "EmailContent",
    "EventRepo",
    "FetchQuery",
    "GitHubPort",
    "InsertResult",
    "LLMProvider",
    "NotifyLogRepo",
    "NotifyMessage",
    "NotifyResult",
    "Notifier",
    "Page",
    "RateStatus",
    "RenderedReport",
    "ReportContext",
    "ReportRepo",
    "ReportRendererPort",
    "RunRepo",
    "SubscriptionRepo",
]
