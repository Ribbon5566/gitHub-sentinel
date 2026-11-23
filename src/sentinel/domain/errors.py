"""领域异常。

全部继承 SentinelError，便于 API 层统一映射成 HTTP 状态码；
领域层不感知 HTTP，所以这里不带 status_code。
"""

from __future__ import annotations


class SentinelError(Exception):
    """本项目所有可预期异常的基类。"""


class ValidationError(SentinelError):
    """输入不合法（仓库名、cron 表达式、时区等）。"""


class NotFoundError(SentinelError):
    """目标对象不存在（订阅、报告、运行记录）。"""


class ConflictError(SentinelError):
    """对象已存在（重复订阅、重复报告窗口）。"""


class CronError(ValidationError):
    """cron 表达式无法解析。"""


class GitHubError(SentinelError):
    def __init__(self, message: str, *, status: int | None = None, resource: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.resource = resource


class RateLimitError(GitHubError):
    def __init__(self, message: str, *, reset_at: int | None = None, retry_after: int | None = None) -> None:
        super().__init__(message, status=403)
        self.reset_at = reset_at
        self.retry_after = retry_after


class LLMError(SentinelError):
    """LLM 调用或结构化输出解析失败。"""


class NotifyError(SentinelError):
    """通知发送失败。"""


class SchedulerError(SentinelError):
    """调度器装配或抢占失败。"""
