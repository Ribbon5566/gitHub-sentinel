"""领域对象 ↔ 数据库行 的转换。

所有时间字段只在这里做 tz 处理：出库补 UTC，入库去 tz 存 naive UTC。
其他地方一律不碰 naive datetime。
"""

from __future__ import annotations

from datetime import UTC, datetime

from ...domain.enums import DigestStyle, EventKind, NotifyStatus, ReportStatus, RunStatus
from ...domain.models import (
    CollectRun,
    DigestReport,
    FetchCursor,
    NotificationLog,
    RepoEvent,
    Subscription,
)
from .tables import (
    CursorRow,
    EventRow,
    NotificationRow,
    ReportRow,
    RunRow,
    SubscriptionRow,
)


def to_db(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def from_db(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


# ---------------------------------------------------------------- subscription
def subscription_to_domain(row: SubscriptionRow) -> Subscription:
    return Subscription(
        id=row.id,
        repo_full_name=row.repo_full_name,
        title=row.title,
        cron=row.cron,
        timezone=row.timezone,
        watch=frozenset(EventKind(kind) for kind in (row.watch or [])),
        label_include=tuple(row.label_include or ()),
        label_exclude=tuple(row.label_exclude or ()),
        author_exclude=tuple(row.author_exclude or ()),
        keyword_include=tuple(row.keyword_include or ()),
        keyword_exclude=tuple(row.keyword_exclude or ()),
        skip_bots=bool(row.skip_bots),
        digest_style=DigestStyle(row.digest_style),
        language=row.language,
        notify_enabled=bool(row.notify_enabled),
        notify_target=row.notify_target,
        enabled=bool(row.enabled),
        bootstrap_done=bool(row.bootstrap_done),
        next_run_at=from_db(row.next_run_at),
        last_collected_at=from_db(row.last_collected_at),
        created_at=from_db(row.created_at) or datetime.now(UTC),
        updated_at=from_db(row.updated_at) or datetime.now(UTC),
        deleted_at=from_db(row.deleted_at),
    )


def subscription_to_row(sub: Subscription) -> SubscriptionRow:
    row = SubscriptionRow()
    apply_subscription(row, sub)
    return row


def apply_subscription(row: SubscriptionRow, sub: Subscription) -> None:
    row.repo_full_name = sub.repo_full_name
    row.title = sub.title
    row.cron = sub.cron
    row.timezone = sub.timezone
    row.watch = sorted(kind.value for kind in sub.watch)
    row.label_include = list(sub.label_include)
    row.label_exclude = list(sub.label_exclude)
    row.author_exclude = list(sub.author_exclude)
    row.keyword_include = list(sub.keyword_include)
    row.keyword_exclude = list(sub.keyword_exclude)
    row.skip_bots = sub.skip_bots
    row.digest_style = sub.digest_style.value
    row.language = sub.language
    row.notify_enabled = sub.notify_enabled
    row.notify_target = sub.notify_target
    row.enabled = sub.enabled
    row.bootstrap_done = sub.bootstrap_done
    row.next_run_at = to_db(sub.next_run_at)
    row.last_collected_at = to_db(sub.last_collected_at)
    row.updated_at = to_db(sub.updated_at) or to_db(datetime.now(UTC))
    row.deleted_at = to_db(sub.deleted_at)
    if sub.created_at is not None:
        row.created_at = to_db(sub.created_at)


# --------------------------------------------------------------------- event
def event_to_row(event: RepoEvent) -> EventRow:
    return EventRow(
        subscription_id=event.subscription_id,
        repo_full_name=event.repo_full_name,
        kind=event.kind.value,
        external_id=event.external_id,
        version=event.version,
        dedup_key=event.dedup_key,
        title=event.title[:500],
        url=event.url,
        author=event.author,
        authored_at=to_db(event.authored_at),
        source_updated_at=to_db(event.source_updated_at),
        state=event.state,
        labels=list(event.labels),
        body_excerpt=event.body_excerpt,
        created_at=to_db(event.created_at),
    )


def event_to_domain(row: EventRow) -> RepoEvent:
    return RepoEvent(
        id=row.id,
        subscription_id=row.subscription_id,
        repo_full_name=row.repo_full_name,
        kind=EventKind(row.kind),
        external_id=row.external_id,
        version=row.version,
        dedup_key=row.dedup_key,
        title=row.title,
        url=row.url,
        author=row.author,
        authored_at=from_db(row.authored_at) or datetime.now(UTC),
        source_updated_at=from_db(row.source_updated_at),
        state=row.state,
        labels=tuple(row.labels or ()),
        body_excerpt=row.body_excerpt,
        created_at=from_db(row.created_at) or datetime.now(UTC),
    )


# -------------------------------------------------------------------- cursor
def cursor_to_domain(row: CursorRow) -> FetchCursor:
    return FetchCursor(
        subscription_id=row.subscription_id,
        resource=row.resource,
        watermark=from_db(row.watermark),
        etag=row.etag,
        updated_at=from_db(row.updated_at) or datetime.now(UTC),
    )


# -------------------------------------------------------------------- report
def report_to_row(report: DigestReport) -> ReportRow:
    row = ReportRow()
    apply_report(row, report)
    return row


def apply_report(row: ReportRow, report: DigestReport) -> None:
    row.subscription_id = report.subscription_id
    row.repo_full_name = report.repo_full_name
    row.period_start = to_db(report.period_start)
    row.period_end = to_db(report.period_end)
    row.status = report.status.value
    row.title = report.title
    row.headline = report.headline
    row.summary_md = report.summary_md
    row.highlights = list(report.highlights)
    row.risks = list(report.risks)
    row.stats = dict(report.stats)
    row.event_count = report.event_count
    row.content_hash = report.content_hash
    row.model = report.model
    row.html_path = report.html_path
    row.text_path = report.text_path
    row.error = report.error
    row.sent_at = to_db(report.sent_at)


def report_to_domain(row: ReportRow) -> DigestReport:
    return DigestReport(
        id=row.id,
        subscription_id=row.subscription_id,
        repo_full_name=row.repo_full_name,
        period_start=from_db(row.period_start) or datetime.now(UTC),
        period_end=from_db(row.period_end) or datetime.now(UTC),
        status=ReportStatus(row.status),
        title=row.title,
        headline=row.headline,
        summary_md=row.summary_md,
        highlights=tuple(row.highlights or ()),
        risks=tuple(row.risks or ()),
        stats=dict(row.stats or {}),
        event_count=row.event_count,
        content_hash=row.content_hash,
        model=row.model,
        html_path=row.html_path,
        text_path=row.text_path,
        error=row.error,
        created_at=from_db(row.created_at) or datetime.now(UTC),
        sent_at=from_db(row.sent_at),
    )


# ----------------------------------------------------------------- run / notify
def run_to_domain(row: RunRow) -> CollectRun:
    return CollectRun(
        id=row.id,
        subscription_id=row.subscription_id,
        repo_full_name=row.repo_full_name,
        status=RunStatus(row.status),
        started_at=from_db(row.started_at) or datetime.now(UTC),
        finished_at=from_db(row.finished_at),
        inserted=row.inserted,
        skipped=row.skipped,
        resources=dict(row.resources or {}),
        not_modified=row.not_modified,
        error=row.error,
    )


def notification_to_domain(row: NotificationRow) -> NotificationLog:
    return NotificationLog(
        id=row.id,
        report_id=row.report_id,
        channel=row.channel,
        target=row.target,
        status=NotifyStatus(row.status),
        idempotency_key=row.idempotency_key,
        error=row.error,
        created_at=from_db(row.created_at) or datetime.now(UTC),
    )


def notification_to_row(log: NotificationLog) -> NotificationRow:
    return NotificationRow(
        report_id=log.report_id,
        channel=log.channel,
        target=log.target,
        status=log.status.value,
        idempotency_key=log.idempotency_key,
        error=log.error,
        created_at=to_db(log.created_at),
    )
