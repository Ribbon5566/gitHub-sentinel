from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from ..domain.errors import ValidationError
from ..domain.models import RepoEvent, RepoMeta
from ..domain.progress import (
    PROGRESS_RESOURCES,
    CommitRecord,
    IssueRecord,
    PullRecord,
    ReleaseRecord,
)


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
class ProgressQuery:
    """某仓库某个时间窗口的快照查询。

    和 FetchQuery 的关键差别：这里要的是「窗口内的完整清单」，不是增量水位。
    因此不带 etag（每个窗口都不同，命不中 304），但需要 window_end 把
    服务端不支持的资源（pulls/releases）在客户端截断。

    issue_axis / pull_axis 决定「按哪个时间轴判断属于本期」：
      * "updated" —— 区间内有活动（v2.0 单日口径）
      * "closed" / "merged" —— 区间内关闭 / 合并（研发周期交付口径）

    这不是简单的过滤开关，它会同时改变两件事（实现时必须一起改）：
      1. 请求的 state：完成口径下发 state=closed，先让服务端把范围缩小；
      2. 是否做右端裁剪：完成口径下**不能**按 updated 裁右端，
         否则「区间内合并、之后又被评论」的对象会被漏掉。
    """

    repo_full_name: str
    window_start: datetime
    window_end: datetime
    resources: tuple[str, ...] = ("issues", "pulls")
    per_page: int = 50
    max_pages: int = 5
    body_excerpt_chars: int = 160
    issue_axis: Literal["updated", "closed"] = "updated"
    pull_axis: Literal["updated", "merged"] = "updated"

    def __post_init__(self) -> None:
        unknown = [name for name in self.resources if name not in PROGRESS_RESOURCES]
        if unknown:
            raise ValidationError(f"未知的每日进展资源：{', '.join(unknown)}")
        if self.window_end <= self.window_start:
            raise ValidationError("每日进展窗口为空：请检查日期与时区配置")
        # 翻页预算是配额，不能由调用方随手传个 500 把 rate limit 打穿
        if not 1 <= self.max_pages <= 20:
            raise ValidationError(f"max_pages 需在 1..20 之间，收到 {self.max_pages}")


@dataclass(slots=True)
class ProgressResult:
    repo_full_name: str
    window_start: datetime
    window_end: datetime
    issues: list[IssueRecord] = field(default_factory=list)
    pulls: list[PullRecord] = field(default_factory=list)
    commits: list[CommitRecord] = field(default_factory=list)
    releases: list[ReleaseRecord] = field(default_factory=list)
    # 资源名 -> 是否因分页上限被截断（true 表示「还有更多，但没取完」）
    truncated: dict[str, bool] = field(default_factory=dict)
    # 资源名 -> 被丢弃的条目数。`/issues` 会混入 PR，占比高时说明翻页预算
    # 有一大半花在丢数据上，比「预算不够」更值得先处理
    skipped: dict[str, int] = field(default_factory=dict)


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

    async def fetch_progress(self, query: ProgressQuery) -> ProgressResult:
        """拉取窗口内的 issues/PR（可选 commits/releases）明细。

        与 fetch 分开而不是复用一个方法：Page 只能装归一化事件，
        装不下 PR 的 additions/draft/merged_at，硬塞会把两个用途耦死。
        """
        ...

    async def rate_status(self) -> RateStatus: ...

    async def aclose(self) -> None: ...
