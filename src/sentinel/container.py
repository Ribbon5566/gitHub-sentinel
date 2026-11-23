"""组合根：把所有端口实现装配成一个可用的对象图。

只有这里同时知道 api / application / infrastructure 三层的具体类型；
其他模块一律通过 Protocol 或构造函数注入拿依赖，因此换实现（Fake→REST、
Mock→OpenAI、null→SMTP）只改这一个文件。
"""

from __future__ import annotations

import logging

from .application.collect_service import CollectService
from .application.daily_progress_service import DailyProgressService
from .application.daily_report_service import DailyReportService
from .application.digest_service import DigestService
from .application.notify_service import NotifyService
from .application.pipeline import PipelineService
from .application.subscription_service import SubscriptionService
from .config import Settings
from .domain.errors import LLMError, SentinelError
from .infrastructure.clock import SystemClock
from .infrastructure.github.fake import FakeGitHub
from .infrastructure.github.rest_client import RestGitHub
from .infrastructure.llm.mock import MockLLM
from .infrastructure.llm.openai_compat import OpenAICompatLLM
from .infrastructure.notify.registry import build_notifier
from .infrastructure.persistence.db import create_engine, create_schema, make_session_factory
from .infrastructure.persistence.repositories import (
    SqlCursorRepo,
    SqlEventRepo,
    SqlNotifyLogRepo,
    SqlReportRepo,
    SqlRunRepo,
    SqlSubscriptionRepo,
)
from .infrastructure.rendering.daily_renderer import JinjaDailyRenderer
from .infrastructure.rendering.daily_store import FileDailyStore
from .infrastructure.rendering.jinja_renderer import JinjaReportRenderer
from .infrastructure.scheduling.scheduler import TickScheduler
from .ports.daily import DailyStorePort
from .ports.github import GitHubPort
from .ports.llm import LLMProvider
from .ports.notifier import Notifier
from .ports.renderer import DailyRendererPort, ReportRendererPort

logger = logging.getLogger(__name__)


class Container:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.clock = SystemClock()

        self.engine = None
        self.sessions = None

        self.subs: SqlSubscriptionRepo
        self.events: SqlEventRepo
        self.cursors: SqlCursorRepo
        self.reports: SqlReportRepo
        self.runs: SqlRunRepo
        self.notify_logs: SqlNotifyLogRepo

        self.github: GitHubPort
        self.llm: LLMProvider
        self.fallback_llm: LLMProvider | None = None
        self.notifier: Notifier
        self.renderer: ReportRendererPort
        self.daily_store: DailyStorePort
        self.daily_renderer: DailyRendererPort

        self.subscription_service: SubscriptionService
        self.collect_service: CollectService
        self.digest_service: DigestService
        self.notify_service: NotifyService
        self.pipeline: PipelineService
        self.daily_progress_service: DailyProgressService
        self.daily_report_service: DailyReportService
        self.scheduler: TickScheduler | None = None

    # ------------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        settings = self.settings
        settings.ensure_dirs()

        self.engine = create_engine(settings.db_url)
        await create_schema(self.engine)
        self.sessions = make_session_factory(self.engine)
        logger.info("container.db_ready url=%s", settings.db_url)

        self._build_repositories()
        self._build_adapters()
        self._build_services()

        if settings.enable_scheduler:
            self.scheduler = TickScheduler(
                self.pipeline.tick, interval_seconds=settings.tick_interval_seconds
            )
            self.scheduler.start()

    async def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown()
            self.scheduler = None
        for adapter in (self.github, self.llm, self.notifier):
            try:
                await adapter.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("container.close_failed adapter=%s", type(adapter).__name__)
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None

    # ---------------------------------------------------------------- build
    def _build_repositories(self) -> None:
        assert self.sessions is not None
        self.subs = SqlSubscriptionRepo(self.sessions)
        self.events = SqlEventRepo(self.sessions)
        self.cursors = SqlCursorRepo(self.sessions)
        self.reports = SqlReportRepo(self.sessions)
        self.runs = SqlRunRepo(self.sessions)
        self.notify_logs = SqlNotifyLogRepo(self.sessions)

    def _build_adapters(self) -> None:
        settings = self.settings
        self.github = self._build_github()
        self.llm, self.fallback_llm = self._build_llm()
        self.notifier = build_notifier(settings)
        self.renderer = JinjaReportRenderer(settings)
        self.daily_store = FileDailyStore(
            daily_root=settings.daily_root, data_dir=settings.data_dir
        )
        self.daily_renderer = JinjaDailyRenderer(settings)

    def _build_github(self) -> GitHubPort:
        settings = self.settings
        if settings.github_mode == "live":
            if not settings.github_token:
                # 宁可启动失败，也不要静默降级到假数据——用户会误以为在采真实仓库
                raise SentinelError(
                    "SENTINEL_GITHUB_MODE=live 需要配置 SENTINEL_GITHUB_TOKEN"
                )
            logger.info("github.mode=live repo_api=%s", settings.github_api_url)
            return RestGitHub(
                token=settings.github_token,
                api_url=settings.github_api_url,
                timeout=settings.github_timeout,
                per_page=settings.github_per_page,
                max_pages=settings.github_max_pages,
                retry_attempts=settings.github_retry_attempts,
                max_retry_wait=settings.github_max_retry_wait,
            )
        if settings.github_mode == "replay":
            logger.warning("github.mode=replay 尚未实现录制回放，暂用 FakeGitHub 代替")
        else:
            logger.info("github.mode=fake 使用内置演示数据")
        return FakeGitHub(self.clock, timezone_name=settings.timezone)

    def _build_llm(self) -> tuple[LLMProvider, LLMProvider | None]:
        settings = self.settings
        if settings.llm_provider != "openai_compat":
            logger.info("llm.primary=mock")
            return MockLLM(), None

        try:
            primary = OpenAICompatLLM(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout=settings.llm_timeout,
                max_input_chars=settings.llm_max_input_chars,
                temperature=settings.llm_temperature,
                thinking=settings.llm_thinking,
                json_mode=settings.llm_json_mode,
                report_timeout=settings.llm_report_timeout,
            )
        except LLMError as exc:
            # 没有 key 不该连 GitHub 都跑不了：每日进展只依赖 GitHub，
            # 报告环节再退回规则模板，比整个进程起不来更有用
            if not settings.llm_fallback_to_mock:
                raise
            logger.warning("llm.primary_unavailable fallback=mock err=%s", exc)
            return MockLLM(), None

        fallback = MockLLM() if settings.llm_fallback_to_mock else None
        logger.info(
            "llm.primary=%s fallback=%s thinking=%s",
            primary.name,
            bool(fallback),
            settings.llm_thinking,
        )
        return primary, fallback

    def _build_services(self) -> None:
        settings = self.settings
        self.subscription_service = SubscriptionService(self.subs, self.clock)
        self.collect_service = CollectService(
            subs=self.subs,
            events=self.events,
            cursors=self.cursors,
            runs=self.runs,
            github=self.github,
            clock=self.clock,
            settings=settings,
        )
        self.digest_service = DigestService(
            subs=self.subs,
            events=self.events,
            reports=self.reports,
            llm=self.llm,
            fallback_llm=self.fallback_llm,
            renderer=self.renderer,
            clock=self.clock,
            settings=settings,
        )
        self.notify_service = NotifyService(
            subs=self.subs,
            reports=self.reports,
            logs=self.notify_logs,
            events=self.events,
            notifier=self.notifier,
            renderer=self.renderer,
            clock=self.clock,
            settings=settings,
        )
        self.pipeline = PipelineService(
            collect=self.collect_service,
            digest=self.digest_service,
            notify=self.notify_service,
            subs=self.subs,
            clock=self.clock,
            settings=settings,
        )
        self.daily_progress_service = DailyProgressService(
            github=self.github,
            store=self.daily_store,
            renderer=self.daily_renderer,
            clock=self.clock,
            settings=settings,
        )
        self.daily_report_service = DailyReportService(
            store=self.daily_store,
            llm=self.llm,
            renderer=self.daily_renderer,
            clock=self.clock,
            settings=settings,
        )

    # ------------------------------------------------------------- helpers
    async def run_pipeline(self, sub_id: int):  # noqa: ANN201
        return await self.pipeline.run(sub_id)

    async def tick_once(self, *, limit: int = 10):  # noqa: ANN201
        return await self.pipeline.tick(limit=limit)


def build_container(settings: Settings) -> Container:
    return Container(settings)


__all__ = ["Container", "LLMError", "build_container"]
