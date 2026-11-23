"""每日进展 / 每日报告的 Markdown 渲染。

为什么单独一个渲染器：现有 `render_markdown()` 只支持标题、无序列表、粗体、
行内代码，喂不进表格与 front matter。这两个 md 是交给人和模型读的，
不经看板，所以直接用 Jinja 拼文本，不做 HTML 转义。

表格单元格统一过 `cell` 过滤器：`|` 必须转义，否则标题里带竖线会把表格撑破；
换行必须压平，否则一行变三行、整张表错位。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from jinja2 import Environment

from ...config import Settings
from ...domain.frontmatter import dump_front_matter
from ...domain.progress import STAT_KEYS
from ...ports.renderer import DailyProgressContext, DailyReportContext
from .jinja_renderer import build_environment

# 附录顺序固定为：议题 -> PR -> 提交 -> 发布
_APPENDIX: tuple[tuple[str, str], ...] = (
    ("issues", "议题"),
    ("pulls", "合并请求"),
    ("commits", "代码提交"),
    ("releases", "版本发布"),
)


class JinjaDailyRenderer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.env: Environment = build_environment(settings.templates_dir)
        self.env.filters["cell"] = _cell
        self.env.filters["dt"] = self._format_datetime
        self.env.filters["delta"] = _delta

    # ------------------------------------------------------------------ 进度
    def render_progress(self, context: DailyProgressContext) -> str:
        stats = {key: context.stats.get(key, 0) for key in STAT_KEYS}
        meta: dict[str, object] = {
            "repo": context.repo_full_name,
            "repo_title": context.repo_title,
            "date": f"{context.day:%Y-%m-%d}",
            "timezone": context.timezone,
            "window_start": _iso(context.window_start),
            "window_end": _iso(context.window_end),
            "generated_at": _iso(context.generated_at),
            "fingerprint": context.fingerprint,
            **stats,
            "truncated": ",".join(sorted(context.truncated)),
            "highlights_json": list(context.highlights),
            "risks_json": list(context.risks),
        }
        body = self.env.get_template("daily_progress.md.j2").render(
            ctx=context,
            s=stats,
            resources=context.resources,
            day_label=f"{context.day:%Y-%m-%d}",
            window_start_label=self._format_datetime(context.window_start),
            window_end_label=self._format_datetime(context.window_end),
            generated_label=self._format_datetime(context.generated_at),
            max_items=context.max_items,
            truncated="、".join(sorted(context.truncated)),
        )
        return f"{dump_front_matter(meta)}\n\n{body.strip()}\n"

    # ------------------------------------------------------------------ 报告
    def render_report(
        self,
        *,
        meta: Mapping[str, str],
        body_md: str,
        sections: Mapping[str, str],
    ) -> str:
        appendix = [
            (title, sections[name].strip())
            for name, title in _APPENDIX
            if sections.get(name, "").strip()
        ]
        title = (
            f"{meta.get('repo_title') or meta.get('repo', '')} 项目每日报告"
            f" · {meta.get('date', '')}"
        )
        body = self.env.get_template("daily_report.md.j2").render(
            title=title,
            body_md=body_md.strip(),
            appendix=appendix,
        )
        return f"{dump_front_matter(meta)}\n\n{body.strip()}\n"

    def render_fallback_body(self, context: DailyReportContext) -> str:
        stats = dict(context.stats)
        issues_total = stats.get("issues_total", 0)
        pulls_total = stats.get("pulls_total", 0)
        merged = stats.get("pulls_merged", 0)
        releases = stats.get("releases_total", 0)

        headline = (
            f"本日 {issues_total} 个议题、{pulls_total} 个 PR 有更新，"
            f"{merged} 个 PR 合并、{releases} 个版本发布。"
        )
        bullets = list(context.highlights) or ["本日没有记录到发布、合并或关闭事件。"]
        risks = list(context.risks)

        plans: list[str] = []
        if stats.get("issues_open"):
            plans.append(f"跟进 {stats['issues_open']} 个未关闭议题，明确负责人与处理时限。")
        if stats.get("pulls_open"):
            plans.append(f"推进 {stats['pulls_open']} 个待合并 PR 的评审与合入。")
        if stats.get("pulls_draft"):
            plans.append(f"确认 {stats['pulls_draft']} 个草稿 PR 的验收标准。")
        if not plans:
            plans.append("保持当前节奏，关注次日新增议题与 PR。")

        return self.env.get_template("daily_report_fallback.md.j2").render(
            headline=headline,
            bullets=bullets,
            risks=risks,
            plans=plans,
        )

    # ---------------------------------------------------------------- 过滤器
    def _format_datetime(self, value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        if value is None:
            return "-"
        return value.astimezone(self.settings.daily_tz).strftime(fmt)


def _cell(value: object, limit: int = 60) -> str:
    text = " ".join(str(value or "").split())
    text = text.replace("|", r"\|")
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _delta(additions: int | None, deletions: int | None, files: int | None) -> str:
    if additions is None and deletions is None:
        return "—"
    files_part = f"/{files} 文件" if files is not None else ""
    return f"+{additions or 0}/−{deletions or 0}{files_part}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
