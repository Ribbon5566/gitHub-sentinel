from __future__ import annotations

import logging
from datetime import timedelta

from ..config import Settings
from ..domain.enums import RESOURCE_KINDS, RESOURCE_ORDER, RunStatus
from ..domain.errors import NotFoundError
from ..domain.filters import EventFilter, filter_events
from ..domain.models import FetchCursor, RepoEvent, Subscription
from ..ports.clock import Clock
from ..ports.github import FetchQuery, GitHubPort
from ..ports.repositories import CursorRepo, EventRepo, RunRepo, SubscriptionRepo
from .dto import CollectOutcome

logger = logging.getLogger(__name__)

# 水位回退量：GitHub 的时间戳精度是秒，且 since 是「闭区间」语义，
# 不退一点会漏掉与上次水位同一秒产生的事件。
SAFETY_MARGIN = timedelta(seconds=90)


class CollectService:
    """增量采集。

    固定顺序（顺序本身就是正确性的一部分）：
        repo_meta → run.start → 逐资源(cursor→since→fetch→304跳过→map→filter→insert→水位)
        → 全部成功才推进水位 → run.finish
    任何一步抛异常都不推进水位，下一次 tick 会重新拉同一段，靠唯一键去重保证不重复入库。
    """

    def __init__(
        self,
        *,
        subs: SubscriptionRepo,
        events: EventRepo,
        cursors: CursorRepo,
        runs: RunRepo,
        github: GitHubPort,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._subs = subs
        self._events = events
        self._cursors = cursors
        self._runs = runs
        self._github = github
        self._clock = clock
        self._settings = settings

    def resources_for(self, sub: Subscription) -> list[str]:
        """按固定顺序返回该订阅关心的资源。"""
        return [name for name in RESOURCE_ORDER if RESOURCE_KINDS[name] in sub.watch]

    async def collect(
        self, sub_id: int, *, resources: list[str] | None = None
    ) -> CollectOutcome:
        sub = await self._subs.get(sub_id)
        if sub is None:
            raise NotFoundError(f"订阅 {sub_id} 不存在")

        now = self._clock.now()
        bootstrap = not sub.bootstrap_done
        run = await self._runs.start(sub.id or 0, sub.repo_full_name)
        run_id = run.id or 0
        plan = resources or self.resources_for(sub)
        rule = EventFilter.from_subscription(sub)
        lookback = timedelta(days=self._settings.bootstrap_lookback_days)

        inserted = 0
        skipped = 0
        not_modified = 0
        per_resource: dict[str, int] = {}

        try:
            meta = await self._github.get_repo_meta(sub.repo_full_name)
            logger.info(
                "collect.start sub=%s repo=%s resources=%s bootstrap=%s stars=%s",
                sub.id,
                sub.repo_full_name,
                ",".join(plan),
                bootstrap,
                meta.stars,
            )

            for resource in plan:
                cursor = await self._cursors.get(sub.id or 0, resource)
                since = (
                    cursor.watermark - SAFETY_MARGIN
                    if cursor and cursor.watermark
                    else now - lookback
                )
                page = await self._github.fetch(
                    FetchQuery(
                        repo_full_name=sub.repo_full_name,
                        resource=resource,
                        since=since,
                        etag=cursor.etag if cursor else None,
                        subscription_id=sub.id,
                        per_page=self._settings.github_per_page,
                    )
                )

                if page.not_modified:
                    not_modified += 1
                    per_resource[resource] = 0
                    logger.info("collect.not_modified sub=%s resource=%s", sub.id, resource)
                    continue

                kept, dropped = filter_events(page.events, rule)
                result = await self._events.add_many(kept)
                inserted += result.inserted
                skipped += result.skipped
                per_resource[resource] = result.inserted
                logger.info(
                    "collect.resource sub=%s resource=%s fetched=%s kept=%s dropped=%s "
                    "inserted=%s skipped=%s",
                    sub.id,
                    resource,
                    len(page.events),
                    len(kept),
                    dropped,
                    result.inserted,
                    result.skipped,
                )

                await self._advance_cursor(
                    sub_id=sub.id or 0,
                    resource=resource,
                    previous=cursor,
                    events=kept,
                    etag=page.etag,
                    now=now,
                )

        except Exception as exc:  # noqa: BLE001 —— 任何异常都要落运行记录再抛
            await self._runs.finish(
                run_id,
                RunStatus.FAILED,
                inserted=inserted,
                skipped=skipped,
                resources=per_resource,
                not_modified=not_modified,
                error=str(exc)[:500],
            )
            logger.exception("collect.failed sub=%s err=%s", sub.id, exc)
            raise

        await self._runs.finish(
            run_id,
            RunStatus.SUCCESS,
            inserted=inserted,
            skipped=skipped,
            resources=per_resource,
            not_modified=not_modified,
        )

        sub.bootstrap_done = True
        sub.last_collected_at = now
        sub.updated_at = now
        sub = await self._subs.update(sub)

        logger.info(
            "collect.done sub=%s inserted=%s skipped=%s not_modified=%s",
            sub.id,
            inserted,
            skipped,
            not_modified,
        )
        return CollectOutcome(
            subscription=sub,
            run=run,
            inserted=inserted,
            skipped=skipped,
            not_modified=not_modified,
            bootstrap=bootstrap,
            resources=per_resource,
        )

    async def _advance_cursor(
        self,
        *,
        sub_id: int,
        resource: str,
        previous: FetchCursor | None,
        events: list[RepoEvent],
        etag: str | None,
        now,
    ) -> None:
        """水位语义：max(事件源侧时间)，且**永不回退**。

        用 now 当水位是常见错误——采集期间产生的事件会被永久跳过。
        """
        candidates = [
            event.source_updated_at or event.authored_at for event in events
        ]
        newest = max(candidates) if candidates else None
        old_watermark = previous.watermark if previous else None

        if newest is None and etag == (previous.etag if previous else None):
            return

        watermark = newest if newest and (old_watermark is None or newest > old_watermark) else old_watermark
        await self._cursors.upsert(
            FetchCursor(
                subscription_id=sub_id,
                resource=resource,
                watermark=watermark,
                etag=etag,
                updated_at=now,
            )
        )
