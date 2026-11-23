from __future__ import annotations

import argparse
from datetime import date, datetime

import pytest

from sentinel.application.dto import DailyProgressOutcome, DailyReportOutcome
from sentinel.cli import _daily, _display, _parse_day, _resolve_targets, build_parser
from sentinel.domain.errors import GitHubError, NotFoundError, ValidationError
from sentinel.domain.models import Subscription


class StubSubs:
    def __init__(self, subs: list[Subscription]) -> None:
        self._subs = {sub.id: sub for sub in subs}

    async def get(self, sub_id: int) -> Subscription | None:
        return self._subs.get(sub_id)

    async def list_active(self) -> list[Subscription]:
        return [sub for sub in self._subs.values() if sub.enabled]


class StubProgress:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.calls: list[dict] = []
        self._fail_for = fail_for or set()

    async def build(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["repo_full_name"] in self._fail_for:
            raise GitHubError("404 仓库不存在", status=404)
        return DailyProgressOutcome(
            repo_full_name=kwargs["repo_full_name"],
            day=kwargs["day"],
            path=f"daily/2026/11/x/{kwargs['day']}.md",
            stats={"issues_total": 3, "issues_closed": 1, "pulls_total": 2, "pulls_merged": 1},
            fingerprint="f" * 8,
            item_count=5,
        )


class StubReport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def build(self, **kwargs):
        self.calls.append(kwargs)
        return DailyReportOutcome(
            repo_full_name=kwargs["repo_full_name"],
            day=kwargs["day"],
            path=f"daily/2026/11/x/{kwargs['day']}-report.md",
            model="deepseek-flash",
        )


class StubContainer:
    def __init__(self, subs=(), progress=None, report=None) -> None:
        self.subs = StubSubs(list(subs))
        self.daily_progress_service = progress or StubProgress()
        self.daily_report_service = report or StubReport()

    async def shutdown(self) -> None:
        return None


def make_args(**overrides) -> argparse.Namespace:
    payload = {"repo": [], "id": [], "date": None, "force": False}
    payload.update(overrides)
    return argparse.Namespace(**payload)


def test_parser_accepts_repeated_repo_and_flags() -> None:
    args = build_parser().parse_args(
        ["progress", "--repo", "a/b", "--repo", "c/d", "--date", "2026-11-23", "--force"]
    )
    assert args.command == "progress"
    assert args.repo == ["a/b", "c/d"]
    assert args.id == []
    assert args.date == "2026-11-23"
    assert args.force is True


@pytest.mark.parametrize("command", ["progress", "report", "daily"])
def test_all_three_daily_commands_exist(command: str) -> None:
    args = build_parser().parse_args([command])
    assert args.command == command


def test_parse_day_defaults_to_local_today(settings) -> None:
    assert _parse_day(None, settings) == datetime.now(settings.daily_tz).date()


def test_parse_day_reads_iso_date(settings) -> None:
    assert _parse_day("2026-11-23", settings) == date(2026, 11, 23)


def test_parse_day_rejects_bad_format(settings) -> None:
    with pytest.raises(ValidationError):
        _parse_day("2026/11/23", settings)


async def test_targets_deduplicate_repo_and_skip_validation() -> None:
    container = StubContainer()
    targets = await _resolve_targets(
        container, make_args(repo=["acme/widgets", "acme/widgets", "fastapi/fastapi"])
    )
    assert targets == [("acme/widgets", None), ("fastapi/fastapi", None)]


async def test_targets_reject_repo_together_with_id() -> None:
    with pytest.raises(ValidationError):
        await _resolve_targets(StubContainer(), make_args(repo=["a/b"], id=[1]))


async def test_targets_from_ids_use_display_name() -> None:
    sub = Subscription(id=7, repo_full_name="acme/widgets", title="Widgets")
    targets = await _resolve_targets(StubContainer([sub]), make_args(id=[7, 7]))
    assert targets == [("acme/widgets", "Widgets")]


async def test_targets_unknown_id_raises() -> None:
    with pytest.raises(NotFoundError):
        await _resolve_targets(StubContainer(), make_args(id=[99]))


async def test_targets_fall_back_to_active_subscriptions() -> None:
    subs = [
        Subscription(id=1, repo_full_name="a/b"),
        Subscription(id=2, repo_full_name="c/d", enabled=False),
    ]
    targets = await _resolve_targets(StubContainer(subs), make_args())
    assert targets == [("a/b", "a/b")]


def test_display_prefixes_relative_paths_only(tmp_path) -> None:
    assert _display("daily/2026/11/a__b/2026-11-23.md") == "data/daily/2026/11/a__b/2026-11-23.md"
    absolute = (tmp_path / "x.md").as_posix()
    assert _display(absolute) == absolute


async def test_daily_reports_parameter_error(monkeypatch, settings) -> None:
    from sentinel import cli

    async def fake_open(_settings):
        return StubContainer()

    monkeypatch.setattr(cli, "_open", fake_open)
    code = await _daily(
        settings, make_args(repo=["a/b"], id=[1]), do_progress=True, do_report=True
    )
    assert code == 2


async def test_daily_runs_both_steps_and_prints_stats(monkeypatch, settings, capsys) -> None:
    from sentinel import cli

    container = StubContainer()

    async def fake_open(_settings):
        return container

    monkeypatch.setattr(cli, "_open", fake_open)
    code = await _daily(
        settings,
        make_args(repo=["acme/widgets"], date="2026-11-23"),
        do_progress=True,
        do_report=True,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "进展: data/daily/2026/11/x/2026-11-23.md" in out
    assert "议题 3（关闭 1）" in out
    assert "报告: data/daily/2026/11/x/2026-11-23-report.md  模型 deepseek-flash" in out
    assert container.daily_report_service.calls[0]["day"] == date(2026, 11, 23)


async def test_daily_continues_after_one_repo_fails(monkeypatch, settings, capsys) -> None:
    from sentinel import cli

    container = StubContainer(progress=StubProgress(fail_for={"bad/repo"}))

    async def fake_open(_settings):
        return container

    monkeypatch.setattr(cli, "_open", fake_open)
    code = await _daily(
        settings,
        make_args(repo=["bad/repo", "good/repo"], date="2026-11-23"),
        do_progress=True,
        do_report=False,
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "404 仓库不存在" in captured.err
    assert "good/repo" in captured.out
    assert [call["repo_full_name"] for call in container.daily_progress_service.calls] == [
        "bad/repo",
        "good/repo",
    ]


async def test_daily_without_targets_returns_one(monkeypatch, settings, capsys) -> None:
    from sentinel import cli

    async def fake_open(_settings):
        return StubContainer()

    monkeypatch.setattr(cli, "_open", fake_open)
    code = await _daily(settings, make_args(), do_progress=True, do_report=False)
    assert code == 1
    assert "没有可处理的目标" in capsys.readouterr().err
