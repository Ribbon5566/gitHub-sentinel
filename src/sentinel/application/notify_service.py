from __future__ import annotations

import logging

from ..config import Settings
from ..domain.digest import DigestSummary
from ..domain.enums import NotifyStatus
from ..domain.errors import ConflictError, NotFoundError, NotifyError
from ..domain.models import NotificationLog
from ..ports.clock import Clock
from ..ports.notifier import Notifier, NotifyMessage
from ..ports.renderer import ReportContext, ReportRendererPort
from ..ports.repositories import EventRepo, NotifyLogRepo, ReportRepo, SubscriptionRepo
from .dto import NotifyOutcome

logger = logging.getLogger(__name__)


class NotifyService:
    """把报告推给用户。

    幂等键 = report_id + channel + target。同一份报告同一渠道只会成功发送一次；
    要重发必须显式 resend=True，此时键后追加 :resend:n 序号，
    这样「补发」是可审计的，而「重试」不会变成刷屏。
    """

    def __init__(
        self,
        *,
        subs: SubscriptionRepo,
        reports: ReportRepo,
        logs: NotifyLogRepo,
        events: EventRepo,
        notifier: Notifier,
        renderer: ReportRendererPort,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._subs = subs
        self._reports = reports
        self._logs = logs
        self._events = events
        self._notifier = notifier
        self._renderer = renderer
        self._clock = clock
        self._settings = settings

    async def notify(self, report_id: int, *, resend: bool = False) -> NotifyOutcome:
        report = await self._reports.get(report_id)
        if report is None:
            raise NotFoundError(f"报告 {report_id} 不存在")
        sub = await self._subs.get(report.subscription_id)
        if sub is None:
            raise NotFoundError(f"报告 {report_id} 对应的订阅已不存在")

        if not sub.notify_enabled:
            logger.info("notify.skipped report=%s reason=disabled", report_id)
            return NotifyOutcome(status=NotifyStatus.SKIPPED.value, detail="订阅未开启通知", skipped=True)

        channel = self._notifier.channel
        target = sub.notify_target or self._settings.smtp_to or ""
        key = await self._next_key(report_id, channel, target, resend=resend)
        if key is None:
            return NotifyOutcome(
                status=NotifyStatus.SKIPPED.value, detail="该报告已发送过", skipped=True
            )

        events = await self._events.list_window(
            report.subscription_id, report.period_start, report.period_end, limit=20
        )
        context = ReportContext(
            subscription=sub,
            report=report,
            summary=DigestSummary(
                headline=report.headline,
                overview_md=report.summary_md,
                highlights=report.highlights,
                risks=report.risks,
                model=report.model,
            ),
            events=events,
            stats=report.stats,
        )
        email = self._renderer.render_email(context)
        message = NotifyMessage(
            report_id=report.id or 0,
            channel=channel,
            target=target,
            subject=email.subject,
            html=email.html,
            text=email.text,
        )

        try:
            result = await self._notifier.send(message)
        except NotifyError as exc:
            await self._record(report.id or 0, channel, target, NotifyStatus.FAILED, key, str(exc))
            logger.warning("notify.failed report=%s err=%s", report_id, exc)
            return NotifyOutcome(status=NotifyStatus.FAILED.value, detail=str(exc))

        await self._record(report.id or 0, channel, target, result.status, key, result.detail or None)
        if result.delivered:
            await self._reports.mark_sent(report.id or 0, self._clock.now())

        logger.info(
            "notify.done report=%s channel=%s status=%s", report_id, channel, result.status.value
        )
        return NotifyOutcome(
            status=result.status.value,
            detail=result.detail,
            skipped=result.status is NotifyStatus.SKIPPED,
        )

    async def _next_key(
        self, report_id: int, channel: str, target: str, *, resend: bool
    ) -> str | None:
        base = f"{report_id}:{channel}:{target}"
        if not resend:
            return None if await self._logs.exists(base) else base
        seq = 1
        key = f"{base}:resend:{seq}"
        while await self._logs.exists(key):
            seq += 1
            key = f"{base}:resend:{seq}"
        return key

    async def _record(
        self,
        report_id: int,
        channel: str,
        target: str,
        status: NotifyStatus,
        key: str,
        detail: str | None,
    ) -> None:
        try:
            await self._logs.add(
                NotificationLog(
                    report_id=report_id,
                    channel=channel,
                    target=target,
                    status=status,
                    idempotency_key=key,
                    error=detail,
                    created_at=self._clock.now(),
                )
            )
        except ConflictError:
            # 并发下另一路已经记录了，视为已发送，不必再报错
            logger.info("notify.duplicate_report key=%s", key)
