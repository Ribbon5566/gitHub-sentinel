"""每日 / 区间进展：GitHub 快照 -> 按「项目 + 周期」命名的 Markdown。

四条边界：
  1. 只读 GitHub、只写文件，**不碰数据库**。进展是快照，不是事件流；
     塞进 events 表会污染 digest 的 cron 窗口与去重键语义。
  2. 失败不落半成品：GitHub 抛错就直接上抛，磁盘上不会出现缺半张表的 md。
  3. 幂等靠内容指纹：同一份数据重跑不重写文件（`--force` 可强制）。
  4. **参数错误一律在任何 GitHub 请求之前拦掉**：区间倒序、跨度过大、
     终点在未来这些错误如果跑到取数之后才发现，等于白烧一次配额。
"""

from __future__ import annotations

import logging
from datetime import date

from ..config import Settings
from ..domain.errors import ValidationError
from ..domain.models import normalize_repo_full_name
from ..domain.period import ReportPeriod, resolve_period_window
from ..domain.progress import (
    DailyProgress,
    build_daily_progress,
    detect_risks,
    rank_highlights,
    resolve_day_window,
    summary_line,
)
from ..domain.selection import ReportSelection
from ..ports.clock import Clock
from ..ports.daily import DailyStorePort
from ..ports.github import GitHubPort, ProgressQuery
from ..ports.renderer import DailyProgressContext, DailyRendererPort
from .dto import DailyProgressOutcome

logger = logging.getLogger(__name__)

# 区间模式仍然可以带上提交与发布，但只认配置里显式开出来的资源：
# issues/pulls 由筛选口径决定，commits/releases 没有「合并/关闭」概念。
_EXTRA_RESOURCES = ("commits", "releases")


class DailyProgressService:
    def __init__(
        self,
        *,
        github: GitHubPort,
        store: DailyStorePort,
        renderer: DailyRendererPort,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._github = github
        self._store = store
        self._renderer = renderer
        self._clock = clock
        self._settings = settings

    async def build(
        self,
        *,
        repo_full_name: str,
        day: date | None = None,
        period: ReportPeriod | None = None,
        selection: ReportSelection | None = None,
        repo_title: str | None = None,
        force: bool = False,
    ) -> DailyProgressOutcome:
        repo = normalize_repo_full_name(repo_full_name)
        settings = self._settings
        tz = settings.daily_tz
        now = self._clock.now()
        today = now.astimezone(tz).date()

        if (day is None) == (period is None):
            raise ValidationError("day 与 period 必须且只能给一个")

        anchor: date
        date_from: date | None
        stem: str | None
        max_pages: int
        requested: tuple[str, ...]

        if period is not None:
            period.validate(today=today, max_days=settings.report_range_max_days)
            selection = selection or ReportSelection.completion()
            if not selection.resources:
                raise ValidationError("本次筛选没有选中任何资源")
            anchor = period.date_to
            date_from = period.date_from
            window_start, window_end = resolve_period_window(period, tz, now=now)
            stem = f"{period.file_stem()}{selection.stem_suffix()}"
            max_pages = settings.report_range_max_pages
            requested = selection.resources + tuple(
                name for name in settings.daily_resource_list if name in _EXTRA_RESOURCES
            )
        else:
            assert day is not None  # 上面已校验二者必有一个
            if selection is not None:
                raise ValidationError("单日模式不支持自定义筛选口径")
            if day > today:
                raise ValidationError(f"{day} 还没到，无法生成当日进展")
            anchor = day
            date_from = None
            window_start, window_end = resolve_day_window(day, tz, now=now)
            stem = None
            max_pages = settings.github_max_pages
            requested = settings.daily_resource_list

        if window_end <= window_start:
            raise ValidationError(
                f"{anchor} 的窗口尚未开始（当前时间 {now.astimezone(tz):%H:%M}）"
            )

        query = ProgressQuery(
            repo_full_name=repo,
            window_start=window_start,
            window_end=window_end,
            resources=requested,
            per_page=settings.github_per_page,
            max_pages=max_pages,
            body_excerpt_chars=settings.daily_body_excerpt_chars,
            issue_axis=(selection.issue_axis if selection and selection.issue_axis else "updated"),
            pull_axis=(selection.pull_axis if selection and selection.pull_axis else "updated"),
        )
        result = await self._github.fetch_progress(query)

        progress = build_daily_progress(
            repo_full_name=repo,
            # 没给标题时用仓库名而不是 owner/repo，报告标题才不至于太长
            repo_title=repo_title or repo.rsplit("/", 1)[-1],
            day=anchor,
            date_from=date_from,
            timezone=settings.daily_timezone or settings.timezone,
            window_start=window_start,
            window_end=window_end,
            generated_at=now,
            issues=result.issues,
            pulls=result.pulls,
            commits=result.commits,
            releases=result.releases,
            truncated=[name for name, flag in result.truncated.items() if flag],
            max_items=settings.daily_max_items_per_resource,
            skip_bots=settings.daily_skip_bots,
            selection=selection,
        )

        existing = self._store.read_progress(repo, anchor, stem=stem)
        if existing is not None and not force and existing.meta.get("fingerprint") == progress.fingerprint:
            logger.info(
                "daily.reused repo=%s period=%s path=%s",
                repo,
                progress.period_label,
                existing.relative_path,
            )
            return _outcome(progress, existing.relative_path, reused=True)

        context = build_context(
            progress, requested, settings.daily_max_items_per_resource, selection
        )
        markdown = self._renderer.render_progress(context)
        path = self._store.write_progress(
            repo_full_name=repo, day=anchor, content=markdown, stem=stem
        )
        logger.info(
            "daily.written repo=%s period=%s items=%s truncated=%s skipped=%s path=%s",
            repo,
            progress.period_label,
            progress.item_count,
            ",".join(sorted(progress.truncated)) or "-",
            ",".join(f"{name}:{count}" for name, count in result.skipped.items() if count) or "-",
            path,
        )
        return _outcome(progress, path)


def build_context(
    progress: DailyProgress,
    resources: tuple[str, ...],
    max_items: int,
    selection: ReportSelection | None = None,
) -> DailyProgressContext:
    return DailyProgressContext(
        repo_full_name=progress.repo_full_name,
        repo_title=progress.repo_title,
        day=progress.day,
        timezone=progress.timezone,
        window_start=progress.window_start,
        window_end=progress.window_end,
        generated_at=progress.generated_at,
        fingerprint=progress.fingerprint,
        resources=resources,
        issues=progress.issues,
        pulls=progress.pulls,
        commits=progress.commits,
        releases=progress.releases,
        stats=progress.stats,
        truncated=progress.truncated,
        highlights=rank_highlights(progress),
        risks=detect_risks(progress),
        summary=summary_line(progress),
        max_items=max_items,
        date_from=progress.date_from,
        period_label=progress.period_label if progress.date_from else "",
        report_kind_label="区间项目进展" if progress.date_from else "每日进展",
        scope_completion=progress.completion_mode,
        issue_axis_attr=progress.issue_axis or "updated_at",
        pull_axis_attr=progress.pull_axis or "updated_at",
        criteria=(
            selection.criteria(period_label=progress.period_label, timezone=progress.timezone)
            if selection is not None
            else ()
        ),
        trimmed=progress.trimmed,
    )


def _outcome(progress: DailyProgress, path: str, *, reused: bool = False) -> DailyProgressOutcome:
    return DailyProgressOutcome(
        repo_full_name=progress.repo_full_name,
        day=progress.day,
        path=path,
        stats=progress.stats,
        fingerprint=progress.fingerprint,
        item_count=progress.item_count,
        truncated=tuple(sorted(progress.truncated)),
        reused=reused,
        date_from=progress.date_from,
        period_label=progress.period_label,
        trimmed=tuple(sorted(progress.trimmed)),
    )
