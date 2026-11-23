from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from ..domain.digest import DigestSummary
from ..domain.models import DigestReport, RepoEvent, Subscription
from ..domain.progress import CommitRecord, IssueRecord, PullRecord, ReleaseRecord


@dataclass(frozen=True, slots=True)
class EmailContent:
    subject: str
    html: str
    text: str


@dataclass(slots=True)
class ReportContext:
    subscription: Subscription
    report: DigestReport
    summary: DigestSummary
    events: list[RepoEvent]
    stats: dict[str, int]

    @property
    def report_title(self) -> str:
        return self.report.title or self.subscription.display_name


@dataclass(frozen=True, slots=True)
class RenderedReport:
    html: str
    text: str
    html_path: str   # 相对 data_dir 的路径，便于整体搬迁
    text_path: str


@dataclass(frozen=True, slots=True)
class DailyProgressContext:
    """渲染每日进展 md 所需的全部内容，都是已计算好的纯数据。"""

    repo_full_name: str
    repo_title: str
    day: date
    timezone: str
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    fingerprint: str
    resources: tuple[str, ...]
    issues: tuple[IssueRecord, ...]
    pulls: tuple[PullRecord, ...]
    commits: tuple[CommitRecord, ...]
    releases: tuple[ReleaseRecord, ...]
    stats: Mapping[str, int]
    truncated: frozenset[str]
    highlights: tuple[str, ...]
    risks: tuple[str, ...]
    summary: str
    max_items: int


@dataclass(frozen=True, slots=True)
class DailyReportContext:
    repo_full_name: str
    repo_title: str
    day: date
    timezone: str
    stats: Mapping[str, int]
    highlights: tuple[str, ...]
    risks: tuple[str, ...]


class ReportRendererPort(Protocol):
    def render_report(self, context: ReportContext, *, stem: str) -> RenderedReport:
        """渲染并落盘报告，返回正文与相对路径。"""
        ...

    def render_email(self, context: ReportContext) -> EmailContent: ...

    def render_page(self, template: str, context: dict[str, Any]) -> str:
        """渲染看板页面（服务端渲染，不走前端构建）。"""
        ...


class DailyRendererPort(Protocol):
    """每日进展 / 每日报告的 Markdown 渲染。

    与 ReportRendererPort 分开：这两个产物是给人看的 md 文件，
    不进看板、不走极简 md→HTML 渲染器（后者不支持表格）。
    """

    def render_progress(self, context: DailyProgressContext) -> str:
        """返回带 front matter 的完整进展文档。"""
        ...

    def render_report(
        self,
        *,
        meta: Mapping[str, str],
        body_md: str,
        sections: Mapping[str, str],
    ) -> str:
        """拼装最终报告：front matter + 标题 + 模型正文 + 附录（复用进展里的表格块）。"""
        ...

    def render_fallback_body(self, context: DailyReportContext) -> str:
        """模型不可用时的规则正文，必须包含四个固定章节。"""
        ...
