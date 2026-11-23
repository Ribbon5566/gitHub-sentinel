from __future__ import annotations

import argparse
from datetime import date, datetime

import pytest

from sentinel.application.dto import DailyProgressOutcome, DailyReportOutcome
from sentinel.cli import (
    _daily,
    _display,
    _parse_day,
    _resolve_period,
    _resolve_targets,
    build_parser,
)
from sentinel.domain.errors import GitHubError, NotFoundError, ValidationError
from sentinel.domain.models import Subscription
from sentinel.domain.period import ReportPeriod
from sentinel.domain.selection import ReportSelection


class StubSubs:
    def __init__(self, subs: list[Subscription]) -> None:
        self._subs = {sub.id: sub for sub in subs}

    async def get(self, sub_id: int) -> Subscription | None:
        return self._subs.get(sub_id)

    async def list_active(self) -> list[Subscription]:
        return [sub for sub in self._subs.values() if sub.enabled]


class StubProgress:
    def __init__(
        self,
        fail_for: set[str] | None = None,
        *,
        trimmed: tuple[str, ...] = (),
        truncated: tuple[str, ...] = (),
    ) -> None:
        self.calls: list[dict] = []
        self._fail_for = fail_for or set()
        self._trimmed = trimmed
        self._truncated = truncated

    async def build(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["repo_full_name"] in self._fail_for:
            raise GitHubError("404 仓库不存在", status=404)
        # 两种调用形状：单日给 day，区间给 period；桩要能接住两者
        period = kwargs.get("period")
        day = kwargs.get("day") or period.date_to
        return DailyProgressOutcome(
            repo_full_name=kwargs["repo_full_name"],
            day=day,
            path=f"daily/2026/11/x/{day}.md",
            stats={"issues_total": 3, "issues_closed": 1, "pulls_total": 2, "pulls_merged": 1},
            fingerprint="f" * 8,
            item_count=5,
            truncated=self._truncated,
            date_from=period.date_from if period else None,
            period_label=period.label() if period else "",
            trimmed=self._trimmed,
        )


class StubReport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def build(self, **kwargs):
        self.calls.append(kwargs)
        period = kwargs.get("period")
        day = kwargs.get("day") or period.date_to
        return DailyReportOutcome(
            repo_full_name=kwargs["repo_full_name"],
            day=day,
            path=f"daily/2026/11/x/{day}-report.md",
            model="deepseek-flash",
            period_label=period.label() if period else "",
        )


class StubContainer:
    def __init__(self, subs=(), progress=None, report=None) -> None:
        self.subs = StubSubs(list(subs))
        self.daily_progress_service = progress or StubProgress()
        self.daily_report_service = report or StubReport()

    async def shutdown(self) -> None:
        return None


def make_args(**overrides) -> argparse.Namespace:
    payload = {
        "repo": [],
        "id": [],
        "date": None,
        "date_from": None,
        "date_to": None,
        "merged": False,
        "closed": False,
        "activity": False,
        "force": False,
    }
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


# --------------------------------------------------- v3.0 区间与筛选口径
def test_parser_maps_from_to_to_date_destinations() -> None:
    """`--from` 与 Python 关键字撞名，dest 必须显式改名，否则静默丢参。"""
    args = build_parser().parse_args(
        ["progress", "--repo", "a/b", "--from", "2026-11-01", "--to", "2026-11-15", "--merged"]
    )
    assert args.date_from == "2026-11-01"
    assert args.date_to == "2026-11-15"
    assert args.merged is True
    assert args.closed is False
    assert args.activity is False


def test_resolve_period_returns_none_for_legacy_single_day(settings) -> None:
    """不给任何区间参数 → (None, None)，仍走 v2.0 单日产物。"""
    assert _resolve_period(make_args(), settings) == (None, None)


def test_resolve_period_defaults_to_completion_scope(settings) -> None:
    period, selection = _resolve_period(
        make_args(date_from="2026-11-01", date_to="2026-11-15"), settings
    )
    assert period == ReportPeriod(date(2026, 11, 1), date(2026, 11, 15))
    assert selection == ReportSelection.completion()
    # 默认口径不加后缀，是最常见的产物名
    assert selection.stem_suffix() == ""


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"activity": True}, ReportSelection.activity()),
        ({"merged": True}, ReportSelection.merged_only()),
        ({"closed": True}, ReportSelection.closed_only()),
        ({"merged": True, "closed": True}, ReportSelection.completion()),
    ],
)
def test_resolve_period_picks_scope_from_flags(settings, flags, expected) -> None:
    _, selection = _resolve_period(
        make_args(date_from="2026-11-01", date_to="2026-11-15", **flags), settings
    )
    assert selection == expected


def test_resolve_period_to_defaults_to_today(settings) -> None:
    period, _ = _resolve_period(make_args(date_from="2026-11-01"), settings)
    assert period.date_to == datetime.now(settings.daily_tz).date()


def test_resolve_period_from_equals_to_is_single_day(settings) -> None:
    period, _ = _resolve_period(
        make_args(date_from="2026-11-15", date_to="2026-11-15", activity=True), settings
    )
    assert period.is_single_day is True
    # 单日区间仍写成 A_to_A，不会与单日的 2026-11-15.md 撞名
    assert period.file_stem() == "2026-11-15_to_2026-11-15"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"date": "2026-11-15", "date_from": "2026-11-01"}, "--date 与 --from/--to 不能同时使用"),
        ({"date_to": "2026-11-15"}, "--to 需要配合 --from 使用"),
        ({"date_from": "2026-11-01", "activity": True, "merged": True}, "--activity 与"),
        ({"merged": True}, "--merged/--closed/--activity 需要配合 --from 使用"),
        ({"activity": True}, "--merged/--closed/--activity 需要配合 --from 使用"),
    ],
)
def test_resolve_period_rejects_conflicting_flags(settings, overrides, fragment) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _resolve_period(make_args(**overrides), settings)
    assert fragment in str(excinfo.value)


def test_resolve_period_rejects_bad_date_format(settings) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _resolve_period(make_args(date_from="2026/11/01"), settings)
    assert "--from 需要 YYYY-MM-DD" in str(excinfo.value)


def test_resolve_period_rejects_reversed_range(settings) -> None:
    with pytest.raises(ValidationError):
        _resolve_period(make_args(date_from="2026-11-15", date_to="2026-11-01"), settings)


async def test_daily_period_uses_period_dict_shape(monkeypatch, settings, capsys) -> None:
    """区间模式调用服务时只给 period/selection，不再塞 day。

    服务层用 `(day is None) == (period is None)` 校验二者必居其一，
    这里若顺手补一个 day 会让校验直接抛错。
    """
    from sentinel import cli

    container = StubContainer()

    async def fake_open(_settings):
        return container

    monkeypatch.setattr(cli, "_open", fake_open)
    code = await _daily(
        settings,
        make_args(
            repo=["acme/widgets"],
            date_from="2026-11-01",
            date_to="2026-11-15",
            closed=True,
        ),
        do_progress=True,
        do_report=True,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "研发周期 2026-11-01 ~ 2026-11-15" in out
    assert "[1/1] acme/widgets 2026-11-01 ~ 2026-11-15" in out
    # 完成口径的统计行不再重复打印总数与关闭数
    assert "关闭议题 1 / 合并 PR 1" in out

    progress_call = container.daily_progress_service.calls[0]
    assert "day" not in progress_call
    assert progress_call["period"] == ReportPeriod(date(2026, 11, 1), date(2026, 11, 15))
    assert progress_call["selection"] == ReportSelection.closed_only()
    report_call = container.daily_report_service.calls[0]
    assert "day" not in report_call
    assert report_call["period"].date_to == date(2026, 11, 15)
    # 报告必须与进展读同一份口径，否则 --merged 会悄悄报告另一套数据
    assert report_call["selection"] == ReportSelection.closed_only()


async def test_daily_period_truncation_is_a_warning_not_a_failure(
    monkeypatch, settings, capsys
) -> None:
    """翻页触顶时统计是下界：要人看见，但不该把批量任务的退出码全刷成 1。"""
    from sentinel import cli

    container = StubContainer(progress=StubProgress(truncated=("pulls",)))

    async def fake_open(_settings):
        return container

    monkeypatch.setattr(cli, "_open", fake_open)
    code = await _daily(
        settings,
        make_args(repo=["acme/widgets"], date_from="2026-11-01", date_to="2026-11-15"),
        do_progress=True,
        do_report=False,
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "警告" in captured.err
    assert "pulls" in captured.err


async def test_daily_period_trimmed_is_only_a_hint(monkeypatch, settings, capsys) -> None:
    """展示裁剪时数据是全的，只是列表印少了——不能和「可能漏报」混为一谈。"""
    from sentinel import cli

    container = StubContainer(progress=StubProgress(trimmed=("issues",)))

    async def fake_open(_settings):
        return container

    monkeypatch.setattr(cli, "_open", fake_open)
    code = await _daily(
        settings,
        make_args(repo=["acme/widgets"], date_from="2026-11-01", date_to="2026-11-15"),
        do_progress=True,
        do_report=False,
    )

    captured = capsys.readouterr()
    assert code == 0
    assert "提示" in captured.out
    assert "issues" in captured.out
    assert "警告" not in captured.err


async def test_daily_period_parameter_error_exits_two_and_skips_github(
    monkeypatch, settings, capsys
) -> None:
    from sentinel import cli

    container = StubContainer()

    async def fake_open(_settings):
        return container

    monkeypatch.setattr(cli, "_open", fake_open)
    code = await _daily(
        settings,
        make_args(repo=["acme/widgets"], date="2026-11-15", date_from="2026-11-01"),
        do_progress=True,
        do_report=False,
    )

    assert code == 2
    assert "参数错误" in capsys.readouterr().err
    assert container.daily_progress_service.calls == []



@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"date_to": "2099-01-01"}, "还没到"),
        ({"date_from": "2020-01-01", "date_to": "2026-01-01"}, "超过上限"),
    ],
)
async def test_daily_period_rejects_range_before_any_github_call(
    monkeypatch, settings, capsys, overrides, fragment
) -> None:
    """区间越界是全局参数问题：应只报一次并以 2 退出，而不是每个仓库各失败一次。"""
    from sentinel import cli

    container = StubContainer()

    async def fake_open(_settings):
        return container

    monkeypatch.setattr(cli, "_open", fake_open)
    code = await _daily(
        settings,
        make_args(repo=["a/b", "c/d"], **{"date_from": "2026-11-01", **overrides}),
        do_progress=True,
        do_report=False,
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "参数错误" in captured.err
    assert fragment in captured.err
    assert container.daily_progress_service.calls == []

