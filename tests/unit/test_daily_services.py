from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from sentinel.application.daily_progress_service import DailyProgressService
from sentinel.application.daily_report_service import DailyReportService
from sentinel.config import Settings
from sentinel.domain.completion import CompletionRequest, CompletionResult
from sentinel.domain.errors import LLMError, NotFoundError, ValidationError
from sentinel.domain.period import ReportPeriod
from sentinel.domain.selection import ReportSelection
from sentinel.infrastructure.github.fake import FakeGitHub
from sentinel.infrastructure.llm.mock import MockLLM
from sentinel.infrastructure.rendering.daily_renderer import JinjaDailyRenderer
from sentinel.infrastructure.rendering.daily_store import FileDailyStore

DAY = date(2026, 11, 23)
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)  # Asia/Shanghai 20:00
REPO = "fastapi/fastapi"
PERIOD = ReportPeriod(date(2026, 11, 1), date(2026, 11, 15))
PERIOD_STEM = "2026-11-01_to_2026-11-15"

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


def build_services(settings: Settings, *, llm=None, clock=None, github=None):
    clock = clock or FixedClock()
    store = FileDailyStore(daily_root=settings.daily_root, data_dir=settings.data_dir)
    renderer = JinjaDailyRenderer(settings)
    github = github or FakeGitHub(clock, timezone_name=settings.timezone)
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


# --------------------------------------------------- v3.0 研发周期（区间筛选）
class CountingGitHub:
    """包一层计数：验证「参数错误不触发任何 GitHub 请求」。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    async def get_repo_meta(self, repo_full_name):
        return await self._inner.get_repo_meta(repo_full_name)

    async def fetch(self, query):
        return await self._inner.fetch(query)

    async def fetch_progress(self, query):
        self.calls += 1
        return await self._inner.fetch_progress(query)

    async def rate_status(self):
        return await self._inner.rate_status()

    async def aclose(self) -> None:
        return None


async def test_period_progress_writes_range_file_without_touching_daily(tmp_path) -> None:
    settings = make_settings(tmp_path)
    progress_service, _, store = build_services(settings)

    await progress_service.build(repo_full_name=REPO, day=DAY)
    daily_before = store.read_progress(REPO, DAY)
    assert daily_before is not None

    outcome = await progress_service.build(repo_full_name=REPO, period=PERIOD)

    assert outcome.path == f"daily/2026/11/fastapi__fastapi/{PERIOD_STEM}.md"
    assert outcome.period_label == "2026-11-01 ~ 2026-11-15"
    # 单日产物一个字节都不能被动到
    daily_after = store.read_progress(REPO, DAY)
    assert daily_after is not None
    assert daily_after.markdown == daily_before.markdown


async def test_period_progress_is_reused_when_clock_moves_but_data_is_same(tmp_path) -> None:
    settings = make_settings(tmp_path)
    clock = FixedClock()
    progress_service, _, _ = build_services(settings, clock=clock)

    first = await progress_service.build(repo_full_name=REPO, period=PERIOD)
    clock.moment = NOW + timedelta(minutes=5)
    second = await progress_service.build(repo_full_name=REPO, period=PERIOD)

    assert second.reused is True
    assert second.fingerprint == first.fingerprint


async def test_invalid_period_is_rejected_before_any_github_call(tmp_path) -> None:
    settings = make_settings(tmp_path)
    github = CountingGitHub(FakeGitHub(FixedClock(), timezone_name=settings.timezone))
    progress_service, _, _ = build_services(settings, github=github)

    for period in (
        ReportPeriod(date(2026, 11, 20), date(2026, 11, 25)),  # 终点在未来
        ReportPeriod(date(2026, 1, 1), date(2026, 11, 23)),  # 超过 92 天跨度
    ):
        with pytest.raises(ValidationError):
            await progress_service.build(repo_full_name=REPO, period=period)

    with pytest.raises(ValidationError):
        await progress_service.build(repo_full_name=REPO, day=DAY, period=PERIOD)
    with pytest.raises(ValidationError):
        await progress_service.build(repo_full_name=REPO, day=DAY, selection=ReportSelection.activity())

    assert github.calls == 0


async def test_period_completion_only_keeps_finished_items(tmp_path) -> None:
    settings = make_settings(tmp_path)
    progress_service, _, store = build_services(settings)

    outcome = await progress_service.build(
        repo_full_name=REPO, period=PERIOD, selection=ReportSelection.completion()
    )

    assert outcome.stats["issues_open"] == 0
    assert outcome.stats["pulls_open"] == 0
    assert outcome.stats["issues_closed"] >= 1
    assert outcome.stats["pulls_merged"] >= 1

    document = store.read_progress(REPO, PERIOD.date_to, stem=PERIOD_STEM)
    assert document is not None
    assert "区间项目进展" in document.body
    assert "`closed_at`" in document.body
    assert "`merged_at`" in document.body
    assert document.meta["issue_axis"] == "closed_at"
    assert document.meta["date_from"] == "2026-11-01"


async def test_merged_only_period_narrows_resources_and_suffixes_file(tmp_path) -> None:
    settings = make_settings(tmp_path)
    progress_service, _, store = build_services(settings)

    outcome = await progress_service.build(
        repo_full_name=REPO, period=PERIOD, selection=ReportSelection.merged_only()
    )

    assert outcome.path.endswith(f"{PERIOD_STEM}.merged.md")
    assert outcome.stats["issues_total"] == 0
    assert outcome.stats["pulls_merged"] >= 1

    document = store.read_progress(REPO, PERIOD.date_to, stem=f"{PERIOD_STEM}.merged")
    assert document is not None
    assert "## 议题" not in document.body


async def test_period_report_uses_period_prompt_and_writes_range_report(tmp_path) -> None:
    settings = make_settings(tmp_path)
    llm = RecordingLLM()
    progress_service, report_service, store = build_services(settings, llm=llm)

    await progress_service.build(repo_full_name=REPO, period=PERIOD)
    outcome = await report_service.build(
        repo_full_name=REPO, period=PERIOD, selection=ReportSelection.completion()
    )

    assert outcome.path.endswith(f"{PERIOD_STEM}-report.md")
    assert outcome.period_label == "2026-11-01 ~ 2026-11-15"

    prompt = llm.calls[0].user
    assert "2026-11-01 ~ 2026-11-15" in prompt
    assert "完成口径" in prompt
    assert "研发周期报告" in llm.calls[0].system

    document = store.read_report(REPO, PERIOD.date_to, stem=PERIOD_STEM)
    assert document is not None
    assert "研发周期报告" in document.body
    assert "## 附录：明细数据" in document.body
    assert "<!-- section:" not in document.body


async def test_period_fallback_body_does_not_say_daily(tmp_path) -> None:
    settings = make_settings(tmp_path)
    llm = RecordingLLM(error=LLMError("超时"))
    progress_service, report_service, store = build_services(settings, llm=llm)

    await progress_service.build(repo_full_name=REPO, period=PERIOD)
    outcome = await report_service.build(repo_full_name=REPO, period=PERIOD)

    assert outcome.degraded is True
    document = store.read_report(REPO, PERIOD.date_to, stem=PERIOD_STEM)
    assert document is not None
    assert "本日 " not in document.body
    for section in ("一句话结论", "关键进展", "风险与阻塞", "下步计划"):
        assert f"## {section}" in document.body


async def test_period_report_missing_progress_hints_from_to_command(tmp_path) -> None:
    settings = make_settings(tmp_path)
    _, report_service, _ = build_services(settings)

    with pytest.raises(NotFoundError) as excinfo:
        await report_service.build(repo_full_name=REPO, period=PERIOD)
    message = str(excinfo.value)
    assert "--from 2026-11-01" in message
    assert "--to 2026-11-15" in message


async def test_period_report_hint_points_at_the_same_scope_file(tmp_path) -> None:
    """提示命令漏了口径参数，用户照着跑还是找不到文件——提示必须能一次性解决问题。"""
    settings = make_settings(tmp_path)
    _, report_service, _ = build_services(settings)

    with pytest.raises(NotFoundError) as excinfo:
        await report_service.build(
            repo_full_name=REPO, period=PERIOD, selection=ReportSelection.merged_only()
        )
    message = str(excinfo.value)
    assert "--from 2026-11-01" in message
    assert "--to 2026-11-15" in message
    # 口径参数必须落在命令末尾，照抄即可复现同一份文件
    assert message.endswith("--merged")
