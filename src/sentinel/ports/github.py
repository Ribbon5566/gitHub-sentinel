from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..domain.models import RepoEvent, RepoMeta


@dataclass(frozen=True, slots=True)
class FetchQuery:
    """一次资源拉取请求。

    since 是「已见过的水位减去安全回退量」，etag 上次响应的 ETag。
    两者同时给：since 保证不漏，etag 保证命中 304 时不消耗配额。
    """

    repo_full_name: str
    resource: str
    since: datetime | None = None
    etag: str | None = None
    subscription_id: int | None = None
    per_page: int = 50


@dataclass(slots=True)
class Page:
    resource: str
    events: list[RepoEvent] = field(default_factory=list)
    etag: str | None = None
    not_modified: bool = False
    truncated: bool = False
    next_page: int | None = None


@dataclass(frozen=True, slots=True)
class RateStatus:
    remaining: int | None = None
    limit: int | None = None
    reset_at: datetime | None = None

    @property
    def exhausted(self) -> bool:
        return self.remaining is not None and self.remaining <= 0


class GitHubPort(Protocol):
    async def get_repo_meta(self, repo_full_name: str) -> RepoMeta: ...

    async def fetch(self, query: FetchQuery) -> Page: ...

    async def rate_status(self) -> RateStatus: ...

    async def aclose(self) -> None: ...
