"""SQLite 表结构。

两个刻意的设计：
  1. 时间列一律存 **naive UTC**。SQLite 没有时区类型，存 aware 只会得到一个
     丢掉 tzinfo 的字符串；统一在 mapper 边界转换，比让每处查询各自猜要安全。
  2. 唯一约束下沉到数据库，不靠应用层「先查再插」——后者在并发下必然漏。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utc_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class SubscriptionRow(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_full_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(200))
    cron: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    watch: Mapped[list[str]] = mapped_column(JSON, default=list)
    label_include: Mapped[list[str]] = mapped_column(JSON, default=list)
    label_exclude: Mapped[list[str]] = mapped_column(JSON, default=list)
    author_exclude: Mapped[list[str]] = mapped_column(JSON, default=list)
    keyword_include: Mapped[list[str]] = mapped_column(JSON, default=list)
    keyword_exclude: Mapped[list[str]] = mapped_column(JSON, default=list)
    skip_bots: Mapped[bool] = mapped_column(default=True)

    digest_style: Mapped[str] = mapped_column(String(20), default="brief")
    language: Mapped[str] = mapped_column(String(16), default="zh")
    notify_enabled: Mapped[bool] = mapped_column(default=True)
    notify_target: Mapped[str | None] = mapped_column(String(200))

    enabled: Mapped[bool] = mapped_column(default=True)
    bootstrap_done: Mapped[bool] = mapped_column(default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_naive)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_events_dedup_key"),
        Index("ix_events_sub_authored", "subscription_id", "authored_at"),
        Index("ix_events_query_key", "repo_full_name", "kind", "external_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    repo_full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    dedup_key: Mapped[str] = mapped_column(String(80), nullable=False)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(500), default="")
    author: Mapped[str] = mapped_column(String(120), default="")
    authored_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    state: Mapped[str | None] = mapped_column(String(40))
    labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    body_excerpt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_naive)


class CursorRow(Base):
    __tablename__ = "fetch_cursors"

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), primary_key=True
    )
    resource: Mapped[str] = mapped_column(String(24), primary_key=True)
    watermark: Mapped[datetime | None] = mapped_column(DateTime)
    etag: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_naive)


class ReportRow(Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "period_start", "period_end", name="uq_reports_window"
        ),
        Index("ix_reports_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    repo_full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")

    title: Mapped[str] = mapped_column(String(300), default="")
    headline: Mapped[str] = mapped_column(String(500), default="")
    summary_md: Mapped[str] = mapped_column(Text, default="")
    highlights: Mapped[list[str]] = mapped_column(JSON, default=list)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list)
    stats: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    event_count: Mapped[int] = mapped_column(Integer, default=0)

    content_hash: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    html_path: Mapped[str] = mapped_column(String(500), default="")
    text_path: Mapped[str] = mapped_column(String(500), default="")
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_naive)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotificationRow(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_notifications_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="skipped")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_naive)


class RunRow(Base):
    __tablename__ = "collect_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    repo_full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_naive)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    resources: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    not_modified: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
