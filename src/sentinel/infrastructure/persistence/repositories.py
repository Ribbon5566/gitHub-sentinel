"""仓储实现（SQLAlchemy async + SQLite）。

写在这里的每个「原子操作」都是为了让上层不必关心并发：
  * claim_due  —— 抢占到期订阅
  * add_many   —— 唯一键冲突静默跳过
  * find_reusable —— 同内容复用摘要
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...domain.enums import ReportStatus, RunStatus
from ...domain.errors import ConflictError, NotFoundError
from ...domain.models import (
    CollectRun,
    DigestReport,
    FetchCursor,
    NotificationLog,
    RepoEvent,
    Subscription,
)
from ...ports.repositories import InsertResult
from .mappers import (
    apply_report,
    apply_subscription,
    cursor_to_domain,
    event_to_domain,
    event_to_row,
    notification_to_domain,
    notification_to_row,
    report_to_domain,
    report_to_row,
    run_to_domain,
    subscription_to_domain,
    subscription_to_row,
    to_db,
)
from .tables import (
    CursorRow,
    EventRow,
    NotificationRow,
    ReportRow,
    RunRow,
    SubscriptionRow,
)

SessionFactory = async_sessionmaker[AsyncSession]


class SqlSubscriptionRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def add(self, sub: Subscription) -> Subscription:
        async with self._sessions() as session:
            row = subscription_to_row(sub)
            session.add(row)
            await session.commit()
            return subscription_to_domain(row)

    async def get(self, sub_id: int, *, include_deleted: bool = False) -> Subscription | None:
        async with self._sessions() as session:
            row = await session.get(SubscriptionRow, sub_id)
            if row is None:
                return None
            if row.deleted_at is not None and not include_deleted:
                return None
            return subscription_to_domain(row)

    async def list(
        self,
        *,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
    ) -> list[Subscription]:
        stmt = select(SubscriptionRow)
        if not include_deleted:
            stmt = stmt.where(SubscriptionRow.deleted_at.is_(None))
        if search:
            stmt = stmt.where(SubscriptionRow.repo_full_name.like(f"%{search.strip()}%"))
        stmt = stmt.order_by(SubscriptionRow.id.desc()).limit(limit).offset(offset)
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [subscription_to_domain(row) for row in rows]

    async def list_active(self) -> list[Subscription]:
        stmt = (
            select(SubscriptionRow)
            .where(
                SubscriptionRow.enabled.is_(True),
                SubscriptionRow.deleted_at.is_(None),
            )
            .order_by(SubscriptionRow.id.asc())
        )
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [subscription_to_domain(row) for row in rows]

    async def count(self, *, include_deleted: bool = False) -> int:
        stmt = select(func.count()).select_from(SubscriptionRow)
        if not include_deleted:
            stmt = stmt.where(SubscriptionRow.deleted_at.is_(None))
        async with self._sessions() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def update(self, sub: Subscription) -> Subscription:
        if sub.id is None:
            raise NotFoundError("订阅缺少 id，无法更新")
        async with self._sessions() as session:
            row = await session.get(SubscriptionRow, sub.id)
            if row is None:
                raise NotFoundError(f"订阅 {sub.id} 不存在")
            apply_subscription(row, sub)
            await session.commit()
            return subscription_to_domain(row)

    async def soft_delete(self, sub_id: int) -> None:
        now = datetime.now(UTC)
        async with self._sessions() as session:
            row = await session.get(SubscriptionRow, sub_id)
            if row is None:
                raise NotFoundError(f"订阅 {sub_id} 不存在")
            row.deleted_at = to_db(now)
            row.enabled = False
            row.next_run_at = None
            await session.commit()

    async def set_next_run(self, sub_id: int, next_run_at: datetime | None) -> None:
        async with self._sessions() as session:
            await session.execute(
                update(SubscriptionRow)
                .where(SubscriptionRow.id == sub_id)
                .values(next_run_at=to_db(next_run_at))
            )
            await session.commit()

    async def claim_due(
        self, now: datetime, *, limit: int = 20, lease_seconds: int = 300
    ) -> list[Subscription]:
        """两步抢占：先选出候选，再用带条件的 UPDATE 把 next_run_at 推到租约时间。

        第二步的 WHERE 里重复了「到期」判断，这样即使两个 tick 同时读到同一批候选，
        也只有一方能真正抢到，另一方拿到的 RETURNING 为空。
        """
        now_db = to_db(now)
        lease_until = to_db(now + timedelta(seconds=lease_seconds))
        due_condition = or_(
            SubscriptionRow.next_run_at.is_(None),
            SubscriptionRow.next_run_at <= now_db,
        )
        async with self._sessions() as session:
            candidate_ids = (
                (
                    await session.execute(
                        select(SubscriptionRow.id)
                        .where(
                            SubscriptionRow.enabled.is_(True),
                            SubscriptionRow.deleted_at.is_(None),
                            due_condition,
                        )
                        .order_by(SubscriptionRow.next_run_at.asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            if not candidate_ids:
                return []

            claimed_ids = (
                (
                    await session.execute(
                        update(SubscriptionRow)
                        .where(SubscriptionRow.id.in_(candidate_ids), due_condition)
                        .values(next_run_at=lease_until)
                        .returning(SubscriptionRow.id)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()
            if not claimed_ids:
                return []

            rows = (
                (
                    await session.execute(
                        select(SubscriptionRow)
                        .where(SubscriptionRow.id.in_(claimed_ids))
                        .order_by(SubscriptionRow.next_run_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            return [subscription_to_domain(row) for row in rows]


class SqlEventRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def add_many(self, events: list[RepoEvent]) -> InsertResult:
        if not events:
            return InsertResult(inserted=0, skipped=0)

        # 批内先去重：同一响应里出现两条同 key 的情况真实存在（分页边界）
        unique: dict[str, RepoEvent] = {}
        for event in events:
            unique.setdefault(event.dedup_key, event)
        candidates = list(unique.values())

        async with self._sessions() as session:
            existing = set(
                (
                    await session.execute(
                        select(EventRow.dedup_key).where(
                            EventRow.dedup_key.in_([e.dedup_key for e in candidates])
                        )
                    )
                )
                .scalars()
                .all()
            )
            fresh = [e for e in candidates if e.dedup_key not in existing]
            if fresh:
                session.add_all([event_to_row(event) for event in fresh])
                try:
                    await session.commit()
                except IntegrityError:
                    # 极端并发下的兜底：整批回滚，改为逐条插入并跳过冲突
                    await session.rollback()
                    inserted = 0
                    for event in fresh:
                        session.add(event_to_row(event))
                        try:
                            await session.commit()
                            inserted += 1
                        except IntegrityError:
                            await session.rollback()
                    return InsertResult(inserted=inserted, skipped=len(events) - inserted)
            return InsertResult(
                inserted=len(fresh),
                skipped=len(events) - len(fresh),
            )

    async def list_window(
        self, sub_id: int, start: datetime, end: datetime, *, limit: int = 500
    ) -> list[RepoEvent]:
        stmt = (
            select(EventRow)
            .where(
                EventRow.subscription_id == sub_id,
                EventRow.authored_at >= to_db(start),
                EventRow.authored_at <= to_db(end),
            )
            .order_by(EventRow.authored_at.desc())
            .limit(limit)
        )
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [event_to_domain(row) for row in rows]

    async def latest(self, *, limit: int = 50) -> list[RepoEvent]:
        stmt = select(EventRow).order_by(EventRow.authored_at.desc()).limit(limit)
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [event_to_domain(row) for row in rows]

    async def count_for(self, sub_id: int) -> int:
        stmt = select(func.count()).select_from(EventRow).where(
            EventRow.subscription_id == sub_id
        )
        async with self._sessions() as session:
            return int((await session.execute(stmt)).scalar_one())


class SqlCursorRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def get(self, sub_id: int, resource: str) -> FetchCursor | None:
        async with self._sessions() as session:
            row = await session.get(CursorRow, (sub_id, resource))
            return cursor_to_domain(row) if row else None

    async def upsert(self, cursor: FetchCursor) -> None:
        values = {
            "subscription_id": cursor.subscription_id,
            "resource": cursor.resource,
            "watermark": to_db(cursor.watermark),
            "etag": cursor.etag,
            "updated_at": to_db(cursor.updated_at),
        }
        # SQLite 专用 upsert：唯一索引即复合主键
        stmt = sqlite_insert(CursorRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["subscription_id", "resource"],
            set_={
                "watermark": stmt.excluded.watermark,
                "etag": stmt.excluded.etag,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        async with self._sessions() as session:
            await session.execute(stmt)
            await session.commit()

    async def all_for(self, sub_id: int) -> list[FetchCursor]:
        stmt = select(CursorRow).where(CursorRow.subscription_id == sub_id)
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [cursor_to_domain(row) for row in rows]


class SqlReportRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def add(self, report: DigestReport) -> DigestReport:
        async with self._sessions() as session:
            row = report_to_row(report)
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError(
                    f"订阅 {report.subscription_id} 在 "
                    f"{report.period_start.isoformat()} ~ {report.period_end.isoformat()} "
                    "已有报告"
                ) from exc
            return report_to_domain(row)

    async def get(self, report_id: int) -> DigestReport | None:
        async with self._sessions() as session:
            row = await session.get(ReportRow, report_id)
            return report_to_domain(row) if row else None

    async def list(
        self, *, subscription_id: int | None = None, limit: int = 50, offset: int = 0
    ) -> list[DigestReport]:
        stmt = select(ReportRow)
        if subscription_id is not None:
            stmt = stmt.where(ReportRow.subscription_id == subscription_id)
        stmt = stmt.order_by(ReportRow.created_at.desc(), ReportRow.id.desc())
        stmt = stmt.limit(limit).offset(offset)
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [report_to_domain(row) for row in rows]

    async def count(self, *, subscription_id: int | None = None) -> int:
        stmt = select(func.count()).select_from(ReportRow)
        if subscription_id is not None:
            stmt = stmt.where(ReportRow.subscription_id == subscription_id)
        async with self._sessions() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def latest_for(self, sub_id: int) -> DigestReport | None:
        stmt = (
            select(ReportRow)
            .where(ReportRow.subscription_id == sub_id)
            .order_by(ReportRow.created_at.desc(), ReportRow.id.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            row = (await session.execute(stmt)).scalars().first()
            return report_to_domain(row) if row else None

    async def find_reusable(
        self, sub_id: int, content_hash: str, period_start: datetime
    ) -> DigestReport | None:
        stmt = (
            select(ReportRow)
            .where(
                ReportRow.subscription_id == sub_id,
                ReportRow.content_hash == content_hash,
                ReportRow.period_start == to_db(period_start),
                ReportRow.status == ReportStatus.READY.value,
            )
            .order_by(ReportRow.created_at.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            row = (await session.execute(stmt)).scalars().first()
            return report_to_domain(row) if row else None

    async def find_by_window(
        self, sub_id: int, period_start: datetime, period_end: datetime
    ) -> DigestReport | None:
        stmt = (
            select(ReportRow)
            .where(
                ReportRow.subscription_id == sub_id,
                ReportRow.period_start == to_db(period_start),
                ReportRow.period_end == to_db(period_end),
            )
            .limit(1)
        )
        async with self._sessions() as session:
            row = (await session.execute(stmt)).scalars().first()
            return report_to_domain(row) if row else None

    async def update(self, report: DigestReport) -> DigestReport:
        if report.id is None:
            raise NotFoundError("报告缺少 id，无法更新")
        async with self._sessions() as session:
            row = await session.get(ReportRow, report.id)
            if row is None:
                raise NotFoundError(f"报告 {report.id} 不存在")
            apply_report(row, report)
            await session.commit()
            return report_to_domain(row)

    async def mark_sent(self, report_id: int, sent_at: datetime) -> None:
        async with self._sessions() as session:
            await session.execute(
                update(ReportRow)
                .where(ReportRow.id == report_id)
                .values(sent_at=to_db(sent_at))
            )
            await session.commit()

    async def delete_for_subscription(self, sub_id: int) -> None:
        async with self._sessions() as session:
            await session.execute(delete(ReportRow).where(ReportRow.subscription_id == sub_id))
            await session.commit()


class SqlRunRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def start(self, sub_id: int, repo_full_name: str) -> CollectRun:
        async with self._sessions() as session:
            row = RunRow(
                subscription_id=sub_id,
                repo_full_name=repo_full_name,
                status=RunStatus.RUNNING.value,
            )
            session.add(row)
            await session.commit()
            return run_to_domain(row)

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
    ) -> None:
        async with self._sessions() as session:
            await session.execute(
                update(RunRow)
                .where(RunRow.id == run_id)
                .values(
                    status=status.value,
                    inserted=inserted,
                    skipped=skipped,
                    resources=resources or {},
                    not_modified=not_modified,
                    error=error,
                    finished_at=to_db(datetime.now(UTC)),
                )
            )
            await session.commit()

    async def list(self, *, limit: int = 50) -> list[CollectRun]:
        stmt = select(RunRow).order_by(RunRow.id.desc()).limit(limit)
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [run_to_domain(row) for row in rows]

    async def count(self) -> int:
        async with self._sessions() as session:
            return int(
                (await session.execute(select(func.count()).select_from(RunRow))).scalar_one()
            )


class SqlNotifyLogRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def exists(self, idempotency_key: str) -> bool:
        stmt = select(NotificationRow.id).where(
            NotificationRow.idempotency_key == idempotency_key
        )
        async with self._sessions() as session:
            return (await session.execute(stmt)).first() is not None

    async def add(self, log: NotificationLog) -> NotificationLog:
        async with self._sessions() as session:
            row = notification_to_row(log)
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError(f"通知已记录: {log.idempotency_key}") from exc
            return notification_to_domain(row)

    async def list_for_report(self, report_id: int) -> list[NotificationLog]:
        stmt = (
            select(NotificationRow)
            .where(NotificationRow.report_id == report_id)
            .order_by(NotificationRow.id.asc())
        )
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [notification_to_domain(row) for row in rows]


__all__ = [
    "SqlCursorRepo",
    "SqlEventRepo",
    "SqlNotifyLogRepo",
    "SqlReportRepo",
    "SqlRunRepo",
    "SqlSubscriptionRepo",
]
