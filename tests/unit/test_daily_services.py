from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sentinel.application.daily_progress_service import DailyProgressService
from sentinel.application.daily_report_service import DailyReportService
from sentinel.config import Settings
from sentinel.domain.completion import CompletionRequest, CompletionResult
from sentinel.domain.errors import LLMError, NotFoundError, ValidationError
from sentinel.infrastructure.github.fake import FakeGitHub
from sentinel.infrastructure.llm.mock import MockLLM
from sentinel.infrastructure.rendering.daily_renderer import JinjaDailyRenderer
from sentinel.infrastructure.rendering.daily_store import FileDailyStore

DAY = date(2026, 11, 23)
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)  # Asia/Shanghai 20:00
REPO = "fastapi/fastapi"

GOOD_BODY = (
    "## 一句话结论\n\n本日交付平稳。\n\n"
    "## 关键进展\n\n- 合并了 1 个 PR。\n\n"
    "## 风险与阻塞\n\n无。\n\n"
    "## 下步计划\n\n- 继续跟进未关闭议题。"
)


class FixedClock:
    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


class RecordingLLM:
    name = "stub"

    def __init__(self, *, text: str | None = GOOD_BODY, error: Exception | None = None) -> None:
        self.calls: list[CompletionRequest] = []
        self._text = text
        self._error = error

    async def summarize(self, request):  # noqa: ANN001, ANN201 - 用例不覆盖摘要路径
        raise NotImplementedError

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return CompletionResult(text=self._text or "", model="deepseek-flash")

    async def aclose(self) -> None:
        return None


def make_settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        timezone="Asia/Shanghai",
        github_mode="fake",
        llm_provider="mock",
        notify_channel="null",
        enable_scheduler=False,
        log_level="WARNING",
    )


def build_services(settings: Settings, *, llm=None, clock=None):
    clock = clock or FixedClock()
    store = FileDailyStore(daily_root=settings.daily_root, data_dir=settings.data_dir)
    renderer = JinjaDailyRenderer(settings)
    github = FakeGitHub(clock, timezone_name=settings.timezone)
    progress = DailyProgressService(
        github=github, store=store, renderer=renderer, clock=clock, settings=settings
    )
    report = DailyReportService(
        store=store,
        llm=llm or RecordingLLM(),
        renderer=renderer,
        clock=clock,
        settings=settings,
    )
    return progress, report, store


async def test_progress_writes_markdown_with_stats(tmp_path) -> None:
    settings = make_settings(tmp_path)
    progress_service, _, store = build_services(settings)

    outcome = await progress_service.build(repo_full_name=REPO, day=DAY)

    assert outcome.path == "daily/2026/11/fastapi__fastapi/2026-11-23.md"
    assert outcome.reused is False
    assert outcome.item_count > 0
    document = store.read_progress(REPO, DAY)
    assert document is not None
    assert document.meta["repo"] == REPO
    assert int(document.meta["issues_total"]) == outcome.stats["issues_total"]
    assert "## 议题" in document.body


async def test_same_day_rerun_is_reused(tmp_path) -> None:
    settings = make_settings(tmp_path)
    progress_service, _, store = build_services(settings)

    first = await progress_service.build(repo_full_name=REPO, day=DAY)
    content_before = store.read_progress(REPO, DAY).markdown  # type: ignore[union-attr]
    second = await progress_service.build(repo_full_name=REPO, day=DAY)
    content_after = store.read_progress(REPO, DAY).markdown  # type: ignore[union-attr]

    assert second.reused is True
    assert second.fingerprint == first.fingerprint
    assert content_after == content_before


async def test_force_rewrites_progress(tmp_path) -> None:
    settings = make_settings(tmp_path)
    progress_service, _, _ = build_services(settings)

    await progress_service.build(repo_full_name=REPO, day=DAY)
    forced = await progress_service.build(repo_full_name=REPO, day=DAY, force=True)
    assert forced.reused is False


async def test_future_day_is_rejected(tmp_path) -> None:
    settings = make_settings(tmp_path)
    progress_service, _, _ = build_services(settings)
    with pytest.raises(ValidationError):
        await progress_service.build(repo_full_name=REPO, day=date(2026, 11, 25))


async def test_report_calls_model_and_writes_four_sections(tmp_path) -> None:
    settings = make_settings(tmp_path)
    progress_service, report_service, store = build_services(settings)
    await progress_service.build(repo_full_name=REPO, day=DAY)

    outcome = await report_service.build(repo_full_name=REPO, day=DAY)

    assert outcome.degraded is False
    assert outcome.model == "deepseek-flash"
    assert outcome.path.endswith("2026-11-23-report.md")
    document = store.read_report(REPO, DAY)
    assert document is not None
    assert document.meta["source_fingerprint"] == outcome.source_fingerprint
    assert "## 一句话结论" in document.body
    assert "## 附录：明细数据" in document.body
    assert "<!-- section:" not in document.body


async def test_report_is_not_regenerated_when_nothing_changed(tmp_path) -> None:
    settings = make_settings(tmp_path)
    llm = RecordingLLM()
    progress_service, report_service, _ = build_services(settings, llm=llm)
    await progress_service.build(repo_full_name=REPO, day=DAY)

    await report_service.build(repo_full_name=REPO, day=DAY)
    second = await report_service.build(repo_full_name=REPO, day=DAY)

    assert len(llm.calls) == 1
    assert second.reused is True


async def test_force_regenerates_report(tmp_path) -> None:
    settings = make_settings(tmp_path)
    llm = RecordingLLM()
    progress_service, report_service, _ = build_services(settings, llm=llm)
    await progress_service.build(repo_full_name=REPO, day=DAY)
    await report_service.build(repo_full_name=REPO, day=DAY)

    await report_service.build(repo_full_name=REPO, day=DAY, force=True)
    assert len(llm.calls) == 2


async def test_llm_failure_falls_back_to_rule_template(tmp_path) -> None:
    settings = make_settings(tmp_path)
    llm = RecordingLLM(error=LLMError("连接超时"))
    progress_service, report_service, store = build_services(settings, llm=llm)
    await progress_service.build(repo_full_name=REPO, day=DAY)

    outcome = await report_service.build(repo_full_name=REPO, day=DAY)

    assert outcome.degraded is True
    assert outcome.model == "rules-fallback"
    document = store.read_report(REPO, DAY)
    assert document is not None
    assert document.meta["degraded"] == "true"
    for section in ("一句话结论", "关键进展", "风险与阻塞", "下步计划"):
        assert f"## {section}" in document.body


async def test_degraded_report_is_retried_without_force(tmp_path) -> None:
    """一次临时故障不能把「规则兜底版」永久缓存下来。"""
    settings = make_settings(tmp_path)
    failing = RecordingLLM(error=LLMError("临时故障"))
    progress_service, failing_report, _ = build_services(settings, llm=failing)
    await progress_service.build(repo_full_name=REPO, day=DAY)
    await failing_report.build(repo_full_name=REPO, day=DAY)

    healthy = RecordingLLM()
    _, healthy_report, _ = build_services(settings, llm=healthy)
    outcome = await healthy_report.build(repo_full_name=REPO, day=DAY)

    assert outcome.degraded is False
    assert len(healthy.calls) == 1


async def test_mock_provider_always_degrades(tmp_path) -> None:
    settings = make_settings(tmp_path)
    progress_service, report_service, _ = build_services(settings, llm=MockLLM())
    await progress_service.build(repo_full_name=REPO, day=DAY)

    outcome = await report_service.build(repo_full_name=REPO, day=DAY)
    assert outcome.degraded is True


async def test_missing_progress_raises_with_hint(tmp_path) -> None:
    settings = make_settings(tmp_path)
    _, report_service, _ = build_services(settings)
    with pytest.raises(NotFoundError) as excinfo:
        await report_service.build(repo_full_name=REPO, day=DAY)
    assert "sentinel progress" in str(excinfo.value)


async def test_mismatched_repo_in_progress_file_is_rejected(tmp_path) -> None:
    settings = make_settings(tmp_path)
    _, report_service, store = build_services(settings)
    store.write_progress(
        repo_full_name=REPO,
        day=DAY,
        content="---\nrepo: other/repo\nfingerprint: x\n---\n\n# 进展\n",
    )
    with pytest.raises(ValidationError):
        await report_service.build(repo_full_name=REPO, day=DAY)


async def test_report_body_rejects_model_output_without_sections(tmp_path) -> None:
    settings = make_settings(tmp_path)
    llm = RecordingLLM(text="抱歉，我无法完成这个任务。")
    progress_service, report_service, _ = build_services(settings, llm=llm)
    await progress_service.build(repo_full_name=REPO, day=DAY)

    outcome = await report_service.build(repo_full_name=REPO, day=DAY)
    assert outcome.degraded is True
