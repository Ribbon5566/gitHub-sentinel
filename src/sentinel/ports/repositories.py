from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..domain.enums import RunStatus
from ..domain.models import (
    CollectRun,
    DigestReport,
    FetchCursor,
    NotificationLog,
    RepoEvent,
    Subscription,
)


@dataclass(frozen=True, slots=True)
class InsertResult:
    inserted: int
    skipped: int

    @property
    def total(self) -> int:
        return self.inserted + self.skipped


class SubscriptionRepo(Protocol):
    async def add(self, sub: Subscription) -> Subscription: ...

    async def get(self, sub_id: int, *, include_deleted: bool = False) -> Subscription | None: ...

    async def list(
        self,
        *,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
    ) -> list[Subscription]: ...

    async def list_active(self) -> list[Subscription]: ...

    async def count(self, *, include_deleted: bool = False) -> int: ...

    async def update(self, sub: Subscription) -> Subscription: ...

    async def soft_delete(self, sub_id: int) -> None: ...

    async def set_next_run(self, sub_id: int, next_run_at: datetime | None) -> None: ...

    async def claim_due(
        self, now: datetime, *, limit: int = 20, lease_seconds: int = 300
    ) -> list[Subscription]:
        """原子抢占到期订阅：把 next_run_at 推到租约到期时间，返回被抢占的订阅。

        单机单进程下也这么做，是为了让「同一订阅被两个进程/两次 tick 同时处理」
        变成不可能，而不是靠调用方自觉。
        """
        ...


class EventRepo(Protocol):
    async def add_many(self, events: list[RepoEvent]) -> InsertResult: ...

    async def list_window(
        self, sub_id: int, start: datetime, end: datetime, *, limit: int = 500
    ) -> list[RepoEvent]: ...

    async def latest(self, *, limit: int = 50) -> list[RepoEvent]: ...

    async def count_for(self, sub_id: int) -> int: ...


class CursorRepo(Protocol):
    async def get(self, sub_id: int, resource: str) -> FetchCursor | None: ...

    async def upsert(self, cursor: FetchCursor) -> None: ...

    async def all_for(self, sub_id: int) -> list[FetchCursor]: ...


class ReportRepo(Protocol):
    async def add(self, report: DigestReport) -> DigestReport: ...

    async def get(self, report_id: int) -> DigestReport | None: ...

    async def list(
        self, *, subscription_id: int | None = None, limit: int = 50, offset: int = 0
    ) -> list[DigestReport]: ...

    async def count(self, *, subscription_id: int | None = None) -> int: ...

    async def latest_for(self, sub_id: int) -> DigestReport | None: ...

    async def find_reusable(
        self, sub_id: int, content_hash: str, period_start: datetime
    ) -> DigestReport | None: ...

    async def find_by_window(
        self, sub_id: int, period_start: datetime, period_end: datetime
    ) -> DigestReport | None: ...

    async def update(self, report: DigestReport) -> DigestReport: ...

    async def mark_sent(self, report_id: int, sent_at: datetime) -> None: ...


class RunRepo(Protocol):
    async def start(self, sub_id: int, repo_full_name: str) -> CollectRun: ...

    async def finish(
        self,
        run_id: int,
        status: RunStatus,
        *,
        inserted: int = 0,
        skipped: int = 0,
        resources: dict[str, int] | None = None,
        not_modified: int = 0,
        error: str | None = None,
    ) -> None: ...

    async def list(self, *, limit: int = 50) -> list[CollectRun]: ...

    async def count(self) -> int: ...


class NotifyLogRepo(Protocol):
    async def exists(self, idempotency_key: str) -> bool: ...

    async def add(self, log: NotificationLog) -> NotificationLog: ...

    async def list_for_report(self, report_id: int) -> list[NotificationLog]: ...
