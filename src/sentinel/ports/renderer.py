from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..domain.digest import DigestSummary
from ..domain.models import DigestReport, RepoEvent, Subscription


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


class ReportRendererPort(Protocol):
    def render_report(self, context: ReportContext, *, stem: str) -> RenderedReport:
        """渲染并落盘报告，返回正文与相对路径。"""
        ...

    def render_email(self, context: ReportContext) -> EmailContent: ...

    def render_page(self, template: str, context: dict[str, Any]) -> str:
        """渲染看板页面（服务端渲染，不走前端构建）。"""
        ...
