"""领域模型。

这一层只有 pydantic 做校验，不认识数据库、HTTP、GitHub 的原始 JSON。
所有 datetime 在领域内一律是 **带时区的 UTC**（aware UTC），
只有持久化适配器在边界上做 naive/aware 转换。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .dedup import dedup_key, event_version, query_key
from .enums import DEFAULT_WATCH, DigestStyle, EventKind, NotifyStatus, ReportStatus, RunStatus
from .errors import ValidationError
from .schedule import normalize_cron, resolve_tz

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BODY_EXCERPT_LIMIT = 1200


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_repo_full_name(value: str) -> str:
    """校验并返回 owner/repo。

    单独抽出来是因为每日进展允许「不建订阅、直接指定仓库」，
    但校验口径必须和订阅完全一致，否则 CLI 与 API 会给出两套答案。
    """
    text = (value or "").strip().strip("/")
    if text.startswith("http"):
        raise ValidationError("repo_full_name 只写 owner/repo，不要带 URL")
    if not _REPO_PATTERN.match(text):
        raise ValidationError("repo_full_name 格式应为 owner/repo")
    return text


def as_utc(moment: datetime | None) -> datetime | None:
    """把任意 datetime 归一成 aware UTC；naive 一律按 UTC 解释。"""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


class RepoMeta(BaseModel):
    full_name: str
    description: str | None = None
    stars: int = 0
    forks: int = 0
    default_branch: str = "main"
    private: bool = False
    html_url: str = ""
    pushed_at: datetime | None = None


class Subscription(BaseModel):
    """一条订阅规则：看哪个仓库、按什么节奏看、关心哪些变化、推给谁。"""

    model_config = ConfigDict(validate_assignment=True)

    id: int | None = None
    repo_full_name: str
    title: str | None = None
    cron: str = "0 9 * * *"
    timezone: str = "Asia/Shanghai"
    watch: frozenset[EventKind] = DEFAULT_WATCH

    # 过滤规则，全部大小写不敏感
    label_include: tuple[str, ...] = ()
    label_exclude: tuple[str, ...] = ()
    author_exclude: tuple[str, ...] = ()
    keyword_include: tuple[str, ...] = ()
    keyword_exclude: tuple[str, ...] = ()
    skip_bots: bool = True

    digest_style: DigestStyle = DigestStyle.BRIEF
    language: str = "zh"
    notify_enabled: bool = True
    notify_target: str | None = None

    enabled: bool = True
    bootstrap_done: bool = False
    next_run_at: datetime | None = None
    last_collected_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    deleted_at: datetime | None = None

    @field_validator("repo_full_name")
    @classmethod
    def _check_repo(cls, value: str) -> str:
        # pydantic 只把 ValueError 收进自己的错误列表，域异常在这里转一次
        try:
            return normalize_repo_full_name(value)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, value: str) -> str:
        return normalize_cron(value)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        resolve_tz(value)
        return value

    @field_validator("title")
    @classmethod
    def _default_title(cls, value: str | None, info: Any) -> str | None:
        if value:
            return value.strip()
        repo = info.data.get("repo_full_name")
        return repo

    @property
    def display_name(self) -> str:
        return self.title or self.repo_full_name

    @property
    def is_active(self) -> bool:
        return self.enabled and self.deleted_at is None


class RepoEvent(BaseModel):
    """从 GitHub 采集到的一条动态，已归一化。"""

    id: int | None = None
    subscription_id: int | None = None
    repo_full_name: str
    kind: EventKind
    external_id: str
    version: str = ""
    dedup_key: str = ""

    title: str
    url: str = ""
    author: str = ""
    authored_at: datetime
    source_updated_at: datetime | None = None
    state: str | None = None
    labels: tuple[str, ...] = ()
    body_excerpt: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    def model_post_init(self, __context: Any) -> None:
        if not self.version:
            self.version = event_version(
                self.kind,
                source_updated_at=self.source_updated_at,
                authored_at=self.authored_at,
            )
        if not self.dedup_key:
            self.dedup_key = dedup_key(
                self.repo_full_name, self.kind, self.external_id, self.version
            )

    @property
    def query_key(self) -> str:
        return query_key(self.repo_full_name, self.kind, self.external_id)

    @property
    def is_terminal_state(self) -> bool:
        return (self.state or "").lower() in {"closed", "merged"}


class FetchCursor(BaseModel):
    """每个 (订阅, 资源) 的增量水位与 ETag。"""

    subscription_id: int
    resource: str
    watermark: datetime | None = None
    etag: str | None = None
    updated_at: datetime = Field(default_factory=utcnow)


class DigestReport(BaseModel):
    id: int | None = None
    subscription_id: int
    repo_full_name: str
    period_start: datetime
    period_end: datetime
    status: ReportStatus = ReportStatus.PENDING

    title: str = ""
    headline: str = ""
    summary_md: str = ""
    highlights: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    stats: dict[str, int] = Field(default_factory=dict)
    event_count: int = 0

    content_hash: str = ""
    model: str = ""
    html_path: str = ""
    text_path: str = ""
    error: str | None = None

    created_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None


class NotificationLog(BaseModel):
    id: int | None = None
    report_id: int
    channel: str
    target: str
    status: NotifyStatus = NotifyStatus.SKIPPED
    idempotency_key: str = ""
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class CollectRun(BaseModel):
    id: int | None = None
    subscription_id: int
    repo_full_name: str
    status: RunStatus = RunStatus.RUNNING
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    inserted: int = 0
    skipped: int = 0
    resources: dict[str, int] = Field(default_factory=dict)
    not_modified: int = 0
    error: str | None = None


def excerpt(text: str | None, limit: int = BODY_EXCERPT_LIMIT) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
