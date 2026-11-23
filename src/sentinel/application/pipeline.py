from __future__ import annotations

import logging

from ..config import Settings
from ..domain.schedule import lease_seconds_for, next_fire, parse_cron, resolve_tz
from ..ports.clock import Clock
from ..ports.repositories import SubscriptionRepo
from .collect_service import CollectService
from .digest_service import DigestService
from .dto import CollectOutcome, DigestOutcome, NotifyOutcome, PipelineOutcome
from .notify_service import NotifyService

logger = logging.getLogger(__name__)


class PipelineService:
    """采集 → 摘要 → 通知 → 重排下一次运行。

    这是唯一「有副作用串联」的地方，也是调度器 tick 的实际工作单元。
    无论中间哪一步失败，finally 都会把 next_run_at 重新算到下一个 cron 时刻，
    避免订阅卡在租约里被反复重试。
    """

    def __init__(
        self,
        *,
        collect: CollectService,
        digest: DigestService,
        notify: NotifyService,
        subs: SubscriptionRepo,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._collect = collect
        self._digest = digest
        self._notify = notify
        self._subs = subs
        self._clock = clock
        self._settings = settings

    async def run(self, sub_id: int) -> PipelineOutcome:
        collect_out: CollectOutcome | None = None
        rescheduled = False
        try:
            collect_out = await self._collect.collect(sub_id)
            # 采集一结束就把 next_run_at 排到真实的 cron 时刻：
            # 否则报告页脚会显示 claim_due 写入的租约时间（现在+3 分钟），
            # 而且后续步骤再失败也不会把订阅卡在租约里。
            await self._reschedule(sub_id)
            rescheduled = True

            digest_out: DigestOutcome = await self._digest.build(sub_id)
            notify_out: NotifyOutcome | None = None
            if digest_out.report is not None and digest_out.report.id is not None:
                notify_out = await self._notify.notify(digest_out.report.id)
            return PipelineOutcome(
                subscription_id=sub_id,
                collect=collect_out,
                digest=digest_out,
                notify=notify_out,
            )
        except Exception as exc:  # noqa: BLE001 —— 单订阅失败不应中断整轮 tick
            logger.exception("pipeline.failed sub=%s", sub_id)
            if collect_out is None:
                raise
            return PipelineOutcome(
                subscription_id=sub_id, collect=collect_out, error=str(exc)
            )
        finally:
            if not rescheduled:
                await self._reschedule(sub_id)

    async def tick(self, *, limit: int = 10) -> list[PipelineOutcome]:
        """调度器每 60 秒调用一次：抢占到期订阅，逐个跑 pipeline。

        串行而不是并发：GitHub 限额是按 token 算的，并发只会更快撞限流，
        而本项目对延迟不敏感（分钟级都可以）。
        """
        now = self._clock.now()
        lease = lease_seconds_for(self._settings.tick_interval_seconds)
        due = await self._subs.claim_due(now, limit=limit, lease_seconds=lease)
        if not due:
            return []

        logger.info("tick.claimed count=%s", len(due))
        outcomes: list[PipelineOutcome] = []
        for sub in due:
            if sub.id is None:
                continue
            try:
                outcomes.append(await self.run(sub.id))
            except Exception:  # noqa: BLE001
                logger.exception("tick.subscription_failed sub=%s", sub.id)
        return outcomes

    async def _reschedule(self, sub_id: int) -> None:
        try:
            sub = await self._subs.get(sub_id)
            if sub is None:
                return
            now = self._clock.now()
            spec = parse_cron(sub.cron)
            sub.next_run_at = next_fire(spec, now, resolve_tz(sub.timezone))
            sub.updated_at = now
            await self._subs.update(sub)
        except Exception:  # noqa: BLE001
            logger.exception("pipeline.reschedule_failed sub=%s", sub_id)
