from __future__ import annotations

import logging
from datetime import timedelta

from ..config import Settings
from ..domain.digest import DigestSummary, build_digest_input, build_stats, content_hash
from ..domain.enums import ReportStatus
from ..domain.errors import LLMError, NotFoundError
from ..domain.models import DigestReport, RepoEvent, Subscription
from ..domain.schedule import parse_cron, previous_fire, resolve_tz
from ..ports.clock import Clock
from ..ports.llm import LLMProvider
from ..ports.renderer import ReportContext, ReportRendererPort
from ..ports.repositories import EventRepo, ReportRepo, SubscriptionRepo
from .dto import DigestOutcome

logger = logging.getLogger(__name__)


class DigestService:
    """把一个周期内的事件变成一份报告。

    幂等设计（很重要，直接决定 token 账单）：
      1. 同一周期 + 内容指纹相同 → 直接复用已有报告，不调模型；
      2. 同一 (订阅, 周期起止) 已有报告 → 覆盖更新，而不是插入第二份；
      3. 模型失败 → 降级到 Mock，报告照常产出，只是没有润色。
    """

    def __init__(
        self,
        *,
        subs: SubscriptionRepo,
        events: EventRepo,
        reports: ReportRepo,
        llm: LLMProvider,
        renderer: ReportRendererPort,
        clock: Clock,
        settings: Settings,
        fallback_llm: LLMProvider | None = None,
    ) -> None:
        self._subs = subs
        self._events = events
        self._reports = reports
        self._llm = llm
        self._fallback = fallback_llm
        self._renderer = renderer
        self._clock = clock
        self._settings = settings

    async def build(self, sub_id: int, *, force: bool = False) -> DigestOutcome:
        sub = await self._subs.get(sub_id)
        if sub is None:
            raise NotFoundError(f"订阅 {sub_id} 不存在")

        now = self._clock.now()
        spec = parse_cron(sub.cron)
        # 严格早于「现在」的上一次触发，作为报告窗口起点
        period_start = previous_fire(spec, now - timedelta(seconds=1), resolve_tz(sub.timezone))
        period_end = now

        window = await self._events.list_window(
            sub_id, period_start, period_end, limit=self._settings.max_events_per_report
        )
        if not window and not self._settings.emit_empty_reports:
            logger.info("digest.skipped sub=%s reason=no_events", sub_id)
            return DigestOutcome(report=None, reason="本周期没有新事件")

        digest_input = build_digest_input(
            sub, window, period_start, period_end, max_events=self._settings.max_events_per_report
        )
        fingerprint = content_hash(window)

        if not force:
            reusable = await self._reports.find_reusable(sub_id, fingerprint, period_start)
            if reusable is not None:
                logger.info("digest.reused sub=%s report=%s", sub_id, reusable.id)
                return DigestOutcome(report=reusable, reused=True, reason="内容未变，复用已有摘要")

        summary = await self._summarize(digest_input)
        existing = await self._reports.find_by_window(sub_id, period_start, period_end)

        report = DigestReport(
            subscription_id=sub.id or 0,
            repo_full_name=sub.repo_full_name,
            period_start=period_start,
            period_end=period_end,
            status=ReportStatus.READY,
            title=f"{sub.display_name} 动态简报 {period_end.astimezone(self._settings.tz):%Y-%m-%d}",
            headline=summary.headline,
            summary_md=summary.overview_md,
            highlights=summary.highlights,
            risks=summary.risks,
            stats=digest_input.stats or build_stats(window),
            event_count=len(window),
            content_hash=fingerprint,
            model=summary.model,
        )

        stem = f"{sub.id}-{period_end:%Y%m%d%H%M%S}-{fingerprint[:8]}"
        context = ReportContext(
            subscription=sub,
            report=report,
            summary=summary,
            events=window[:50],
            stats=report.stats,
        )
        rendered = self._renderer.render_report(context, stem=stem)
        report.html_path = rendered.html_path
        report.text_path = rendered.text_path

        if existing is not None:
            report.id = existing.id
            saved = await self._reports.update(report)
            logger.info("digest.updated sub=%s report=%s", sub_id, saved.id)
            return DigestOutcome(report=saved, reason="同周期报告已更新")

        saved = await self._reports.add(report)
        logger.info(
            "digest.created sub=%s report=%s events=%s model=%s",
            sub_id,
            saved.id,
            saved.event_count,
            saved.model,
        )
        return DigestOutcome(report=saved)

    async def _summarize(self, digest_input) -> DigestSummary:  # noqa: ANN001
        try:
            return await self._llm.summarize(digest_input)
        except LLMError as exc:
            if self._fallback is None:
                raise
            logger.warning("digest.llm_failed fallback=mock err=%s", exc)
            return await self._fallback.summarize(digest_input)

    @staticmethod
    def summary_from_report(report: DigestReport) -> DigestSummary:
        return DigestSummary(
            headline=report.headline,
            overview_md=report.summary_md,
            highlights=report.highlights,
            risks=report.risks,
            model=report.model,
        )

    async def context_for_report(
        self, report: DigestReport, sub: Subscription
    ) -> tuple[ReportContext, list[RepoEvent]]:
        events = await self._events.list_window(
            report.subscription_id, report.period_start, report.period_end, limit=50
        )
        context = ReportContext(
            subscription=sub,
            report=report,
            summary=self.summary_from_report(report),
            events=events,
            stats=report.stats,
        )
        return context, events
