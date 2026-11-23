from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ...config import Settings
from ...domain.enums import EventKind
from ...ports.renderer import EmailContent, RenderedReport, ReportContext
from .markdown import render_markdown
from .storage import ReportStorage

KIND_LABELS: dict[str, str] = {
    EventKind.COMMIT.value: "提交",
    EventKind.ISSUE.value: "议题",
    EventKind.PULL_REQUEST.value: "合并请求",
    EventKind.RELEASE.value: "发布",
    EventKind.DISCUSSION.value: "讨论",
}


def build_environment(templates_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md"] = render_markdown
    env.filters["kind_label"] = lambda kind: KIND_LABELS.get(str(kind), str(kind))
    return env


class JinjaReportRenderer:
    """一份领域上下文，三套模板：看板报告页 / 邮件 HTML / 邮件纯文本。

    邮件不复用网页模板：Outlook 等客户端对 flex/grid 支持很差，邮件模板用表格式布局。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.env = build_environment(settings.templates_dir)
        self.storage = ReportStorage(settings.reports_root, settings.data_dir)
        # 模板里所有时间都按用户时区显示；数据库里存的是 UTC
        self.env.filters["dt"] = self._format_datetime
        self.env.globals["tz_name"] = settings.timezone

    def _format_datetime(self, value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        if value is None:
            return "-"
        return value.astimezone(self.settings.tz).strftime(fmt)

    # ------------------------------------------------------------- report
    def render_report(self, context: ReportContext, *, stem: str) -> RenderedReport:
        payload = self._context(context)
        html = self.env.get_template("report.html").render(**payload)
        text = self.env.get_template("report.txt.j2").render(**payload)
        html_path = self.storage.write(
            sub_id=context.subscription.id or 0,
            moment=context.report.period_end,
            stem=stem,
            suffix=".html",
            content=html,
        )
        text_path = self.storage.write(
            sub_id=context.subscription.id or 0,
            moment=context.report.period_end,
            stem=stem,
            suffix=".txt",
            content=text,
        )
        return RenderedReport(html=html, text=text, html_path=html_path, text_path=text_path)

    def render_email(self, context: ReportContext) -> EmailContent:
        payload = self._context(context)
        subject = (
            f"[GitHub Sentinel] {context.subscription.display_name} "
            f"{context.report.period_end:%m-%d} 动态简报（{context.report.event_count} 条）"
        )
        return EmailContent(
            subject=subject,
            html=self.env.get_template("email.html").render(subject=subject, **payload),
            text=self.env.get_template("email.txt.j2").render(subject=subject, **payload),
        )

    def render_page(self, template: str, context: dict[str, Any]) -> str:
        return self.env.get_template(template).render(**context)

    # ------------------------------------------------------------ helpers
    def _context(self, context: ReportContext) -> dict[str, Any]:
        report = context.report
        return {
            "sub": context.subscription,
            "report": report,
            "summary": context.summary,
            "events": context.events,
            "stats": context.stats,
            "kind_labels": KIND_LABELS,
            "generated_at": datetime.now(self.settings.tz),
            "period_label": (
                f"{report.period_start.astimezone(self.settings.tz):%Y-%m-%d %H:%M}"
                f" ~ {report.period_end.astimezone(self.settings.tz):%Y-%m-%d %H:%M}"
            ),
        }
