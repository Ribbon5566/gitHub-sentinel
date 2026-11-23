"""每日进展：GitHub 快照 -> 按「项目 + 日期」命名的 Markdown。

三条边界：
  1. 只读 GitHub、只写文件，**不碰数据库**。每日进展是快照，不是事件流；
     塞进 events 表会污染 digest 的 cron 窗口与去重键语义。
  2. 失败不落半成品：GitHub 抛错就直接上抛，磁盘上不会出现缺半张表的 md。
  3. 幂等靠内容指纹：同一天同样的数据重跑不重写文件（`--force` 可强制）。
"""

from __future__ import annotations

import logging
from datetime import date

from ..config import Settings
from ..domain.errors import ValidationError
from ..domain.models import normalize_repo_full_name
from ..domain.progress import (
    DailyProgress,
    build_daily_progress,
    detect_risks,
    rank_highlights,
    resolve_day_window,
    summary_line,
)
from ..ports.clock import Clock
from ..ports.daily import DailyStorePort
from ..ports.github import GitHubPort, ProgressQuery
from ..ports.renderer import DailyProgressContext, DailyRendererPort
from .dto import DailyProgressOutcome

logger = logging.getLogger(__name__)


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
        day: date,
        repo_title: str | None = None,
        force: bool = False,
    ) -> DailyProgressOutcome:
        repo = normalize_repo_full_name(repo_full_name)
        settings = self._settings
        tz = settings.daily_tz

        today = self._clock.now().astimezone(tz).date()
        if day > today:
            raise ValidationError(f"{day} 还没到，无法生成当日进展")

        window_start, window_end = resolve_day_window(day, tz, now=self._clock.now())
        if window_end <= window_start:
            raise ValidationError(f"{day} 的窗口尚未开始（当前时间 {self._clock.now():%H:%M}）")

        resources = settings.daily_resource_list
        query = ProgressQuery(
            repo_full_name=repo,
            window_start=window_start,
            window_end=window_end,
            resources=resources,
            per_page=settings.github_per_page,
            max_pages=settings.github_max_pages,
            body_excerpt_chars=settings.daily_body_excerpt_chars,
        )
        result = await self._github.fetch_progress(query)

        progress = build_daily_progress(
            repo_full_name=repo,
            # 没给标题时用仓库名而不是 owner/repo，报告标题才不至于太长
            repo_title=repo_title or repo.rsplit("/", 1)[-1],
            day=day,
            timezone=settings.daily_timezone or settings.timezone,
            window_start=window_start,
            window_end=window_end,
            generated_at=self._clock.now(),
            issues=result.issues,
            pulls=result.pulls,
            commits=result.commits,
            releases=result.releases,
            truncated=[name for name, flag in result.truncated.items() if flag],
            max_items=settings.daily_max_items_per_resource,
            skip_bots=settings.daily_skip_bots,
        )

        existing = self._store.read_progress(repo, day)
        if existing is not None and not force and existing.meta.get("fingerprint") == progress.fingerprint:
            logger.info("daily.reused repo=%s day=%s path=%s", repo, day, existing.relative_path)
            return _outcome(progress, existing.relative_path, reused=True)

        context = build_context(progress, resources, settings.daily_max_items_per_resource)
        markdown = self._renderer.render_progress(context)
        path = self._store.write_progress(repo_full_name=repo, day=day, content=markdown)
        logger.info(
            "daily.written repo=%s day=%s items=%s truncated=%s path=%s",
            repo,
            day,
            progress.item_count,
            ",".join(sorted(progress.truncated)) or "-",
            path,
        )
        return _outcome(progress, path)


def build_context(
    progress: DailyProgress, resources: tuple[str, ...], max_items: int
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
    )
