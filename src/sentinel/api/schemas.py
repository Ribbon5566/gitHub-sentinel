from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, TypeVar

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..container import Container

T = TypeVar("T")


def get_container(request: Request) -> Container:
    return request.app.state.container


ContainerDep = Annotated[Container, Depends(get_container)]


class PageOut(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class HealthOut(BaseModel):
    status: str = "ok"
    version: str
    checks: dict[str, str] = Field(default_factory=dict)


class MetaOut(BaseModel):
    name: str
    version: str
    github_mode: str
    llm_provider: str
    notify_channel: str
    timezone: str
    scheduler_enabled: bool
    event_kinds: list[str]


class SubscriptionCreate(BaseModel):
    repo_full_name: str
    title: str | None = None
    cron: str | None = None
    timezone: str | None = None
    watch: list[str] | None = None
    label_include: list[str] = Field(default_factory=list)
    label_exclude: list[str] = Field(default_factory=list)
    author_exclude: list[str] = Field(default_factory=list)
    keyword_include: list[str] = Field(default_factory=list)
    keyword_exclude: list[str] = Field(default_factory=list)
    skip_bots: bool = True
    digest_style: str | None = None
    language: str = "zh"
    notify_enabled: bool = True
    notify_target: str | None = None
    enabled: bool = True


class SubscriptionUpdate(BaseModel):
    repo_full_name: str | None = None
    title: str | None = None
    cron: str | None = None
    timezone: str | None = None
    watch: list[str] | None = None
    label_include: list[str] | None = None
    label_exclude: list[str] | None = None
    author_exclude: list[str] | None = None
    keyword_include: list[str] | None = None
    keyword_exclude: list[str] | None = None
    skip_bots: bool | None = None
    digest_style: str | None = None
    language: str | None = None
    notify_enabled: bool | None = None
    notify_target: str | None = None
    enabled: bool | None = None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_full_name: str
    title: str | None = None
    display_name: str
    cron: str
    timezone: str
    watch: frozenset[str]
    digest_style: str
    language: str
    notify_enabled: bool
    notify_target: str | None = None
    enabled: bool
    bootstrap_done: bool
    next_run_at: datetime | None = None
    last_collected_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_full_name: str
    kind: str
    external_id: str
    title: str
    url: str
    author: str
    authored_at: datetime
    state: str | None = None
    labels: tuple[str, ...] = ()
    body_excerpt: str | None = None


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subscription_id: int
    repo_full_name: str
    period_start: datetime
    period_end: datetime
    status: str
    title: str
    headline: str
    summary_md: str
    highlights: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    stats: dict[str, int] = Field(default_factory=dict)
    event_count: int
    model: str
    html_path: str
    text_path: str
    created_at: datetime
    sent_at: datetime | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subscription_id: int
    repo_full_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    inserted: int
    skipped: int
    not_modified: int
    resources: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class CollectResultOut(BaseModel):
    subscription_id: int
    inserted: int
    skipped: int
    not_modified: int
    bootstrap: bool
    resources: dict[str, int] = Field(default_factory=dict)


class DigestResultOut(BaseModel):
    report_id: int | None = None
    reused: bool = False
    reason: str | None = None


class NotifyResultOut(BaseModel):
    status: str
    detail: str = ""
    skipped: bool = False
