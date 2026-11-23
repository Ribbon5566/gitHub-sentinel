"""调度器。

为什么不用「每个订阅一个 cron job」：
  * 增删改订阅都要同步改 job 表，两条真相源必然不一致；
  * job 存内存则重启全丢，存 SQLAlchemyJobStore 又和业务表抢同一把锁；
  * 多 worker 部署时每个进程各跑一份，任务被重复执行。

所以只保留**一个 interval tick**：每 60 秒扫一次「到期的订阅」，
到期判定与抢占都发生在数据库里（claim_due），进程只负责干活。
重启后过期的订阅会在下一 tick 自然被捡起来，不需要补偿逻辑。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

TickCallable = Callable[[], Awaitable[None]]


class TickScheduler:
    def __init__(
        self,
        tick: TickCallable,
        *,
        interval_seconds: int = 60,
        misfire_grace_seconds: int = 300,
    ) -> None:
        self._tick = tick
        self._interval = max(5, interval_seconds)
        self._grace = misfire_grace_seconds
        self._scheduler: AsyncIOScheduler | None = None
        # 进程内互斥：APScheduler 的 max_instances=1 只管 job 层，管不住手动调用 tick
        self._guard = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    async def run_tick(self) -> None:
        """手动触发一次，CLI 与测试都走这里，保证与定时路径行为一致。"""
        if self._guard.locked():
            logger.warning("scheduler.tick_skipped reason=previous_tick_running")
            return
        async with self._guard:
            await self._tick()

    def start(self) -> None:
        if self._scheduler is not None:
            return
        scheduler = AsyncIOScheduler(timezone=UTC)
        scheduler.add_job(
            self.run_tick,
            trigger=IntervalTrigger(seconds=self._interval),
            id="sentinel-tick",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=self._grace,
            # 首次触发安排在 5 秒后，避免和启动阶段的建表/装配抢资源
            next_run_time=datetime.now(UTC) + timedelta(seconds=5),
        )
        scheduler.start()
        self._scheduler = scheduler
        logger.info("scheduler.started interval=%ss", self._interval)

    def shutdown(self) -> None:
        if self._scheduler is None:
            return
        self._scheduler.shutdown(wait=False)
        self._scheduler = None
        logger.info("scheduler.stopped")
