from __future__ import annotations

import logging
from datetime import datetime

from ..domain.errors import NotFoundError, ValidationError
from ..domain.models import Subscription
from ..domain.schedule import next_fire, parse_cron, resolve_tz
from ..ports.clock import Clock
from ..ports.repositories import SubscriptionRepo

logger = logging.getLogger(__name__)

# 允许通过 API 修改的字段。白名单而不是黑名单：
# 避免有人顺手把 bootstrap_done / next_run_at 改坏，导致重复 bootstrap 或漏跑。
_UPDATABLE = frozenset(
    {
        "repo_full_name",
        "title",
        "cron",
        "timezone",
        "watch",
        "label_include",
        "label_exclude",
        "author_exclude",
        "keyword_include",
        "keyword_exclude",
        "skip_bots",
        "digest_style",
        "language",
        "notify_enabled",
        "notify_target",
        "enabled",
    }
)


class SubscriptionService:
    def __init__(self, subs: SubscriptionRepo, clock: Clock) -> None:
        self._subs = subs
        self._clock = clock

    @property
    def repo(self) -> SubscriptionRepo:
        return self._subs

    def compute_next_run(self, sub: Subscription, *, after: datetime | None = None) -> datetime:
        spec = parse_cron(sub.cron)
        return next_fire(spec, after or self._clock.now(), resolve_tz(sub.timezone))

    async def create(self, sub: Subscription) -> Subscription:
        sub.next_run_at = self.compute_next_run(sub)
        sub.updated_at = self._clock.now()
        created = await self._subs.add(sub)
        logger.info(
            "subscription.created id=%s repo=%s cron=%s next_run=%s",
            created.id,
            created.repo_full_name,
            created.cron,
            created.next_run_at,
        )
        return created

    async def get(self, sub_id: int) -> Subscription:
        sub = await self._subs.get(sub_id)
        if sub is None:
            raise NotFoundError(f"订阅 {sub_id} 不存在")
        return sub

    async def list(self, **kwargs: object) -> list[Subscription]:
        return await self._subs.list(**kwargs)  # type: ignore[arg-type]

    async def update(self, sub_id: int, changes: dict[str, object]) -> Subscription:
        unknown = set(changes) - _UPDATABLE
        if unknown:
            raise ValidationError(f"不允许修改的字段: {', '.join(sorted(unknown))}")

        sub = await self.get(sub_id)
        reschedule = "cron" in changes or "timezone" in changes
        for key, value in changes.items():
            setattr(sub, key, value)
        sub.updated_at = self._clock.now()
        if reschedule or sub.next_run_at is None:
            sub.next_run_at = self.compute_next_run(sub)
        return await self._subs.update(sub)

    async def set_enabled(self, sub_id: int, enabled: bool) -> Subscription:
        sub = await self.get(sub_id)
        sub.enabled = enabled
        sub.updated_at = self._clock.now()
        sub.next_run_at = self.compute_next_run(sub) if enabled else None
        return await self._subs.update(sub)

    async def delete(self, sub_id: int) -> None:
        await self.get(sub_id)
        await self._subs.soft_delete(sub_id)
        logger.info("subscription.deleted id=%s", sub_id)

    async def mark_collected(self, sub: Subscription, *, collected_at: datetime) -> None:
        sub.bootstrap_done = True
        sub.last_collected_at = collected_at
        sub.updated_at = collected_at
        await self._subs.update(sub)

    async def reschedule(self, sub: Subscription) -> Subscription:
        sub.next_run_at = self.compute_next_run(sub)
        sub.updated_at = self._clock.now()
        return await self._subs.update(sub)
