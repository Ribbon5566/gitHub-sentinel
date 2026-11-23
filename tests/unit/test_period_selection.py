from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sentinel.domain.errors import ValidationError
from sentinel.domain.period import ReportPeriod, resolve_period_window
from sentinel.domain.selection import IssueScope, PullScope, ReportSelection

SH = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 11, 23)


@dataclass
class FakeIssue:
    updated_at: datetime
    closed_at: datetime | None = None
    state: str = "open"


@dataclass
class FakePull:
    updated_at: datetime
    merged_at: datetime | None = None
    state: str = "open"


def ts(day: int, hour: int = 0) -> datetime:
    """2026-11-{day} {hour}:00 UTC，测试里只关心相对位置。"""
    return datetime(2026, 11, day, hour, tzinfo=UTC)


# ------------------------------------------------------------------ 周期
def test_period_rejects_reversed_dates() -> None:
    with pytest.raises(ValidationError):
        ReportPeriod(date_from=date(2026, 11, 15), date_to=date(2026, 11, 1))


def test_span_days_counts_both_ends() -> None:
    assert ReportPeriod.single(DAY).span_days == 1
    assert ReportPeriod(date(2026, 11, 1), date(2026, 11, 15)).span_days == 15
    # 闰日也要算进去：2-28、2-29、3-01
    assert ReportPeriod(date(2028, 2, 28), date(2028, 3, 1)).span_days == 3


def test_label_and_file_stem_cover_single_range_and_cross_year() -> None:
    single = ReportPeriod.single(DAY)
    assert single.label() == "2026-11-23"
    assert single.is_single_day is True
    # 单日区间也写成 A_to_A：它和 v2.0 的 2026-11-23.md 是两份不同产物
    assert single.file_stem() == "2026-11-23_to_2026-11-23"

    span = ReportPeriod(date(2026, 11, 1), date(2026, 11, 15))
    assert span.label() == "2026-11-01 ~ 2026-11-15"
    assert span.file_stem() == "2026-11-01_to_2026-11-15"

    cross = ReportPeriod(date(2026, 12, 20), date(2027, 1, 5))
    assert cross.label() == "2026-12-20 ~ 2027-01-05"
    assert cross.file_stem() == "2026-12-20_to_2027-01-05"


def test_validate_rejects_future_span_and_keeps_boundary() -> None:
    today = date(2026, 11, 23)
    ReportPeriod.single(today).validate(today=today, max_days=92)

    with pytest.raises(ValidationError, match="还没到"):
        ReportPeriod(date(2026, 11, 20), date(2026, 11, 24)).validate(today=today, max_days=92)
    with pytest.raises(ValidationError, match="超过上限"):
        ReportPeriod(date(2026, 1, 1), date(2026, 6, 1)).validate(today=today, max_days=92)


def test_window_is_local_midnight_in_utc() -> None:
    period = ReportPeriod(date(2026, 11, 1), date(2026, 11, 15))
    start, end = resolve_period_window(period, SH)
    assert start == datetime(2026, 10, 31, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 15, 16, 0, tzinfo=UTC)
    assert (end - start) == timedelta(days=15)


def test_window_is_clipped_to_now() -> None:
    period = ReportPeriod(date(2026, 11, 1), date(2026, 11, 15))
    moment = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
    _, end = resolve_period_window(period, SH, now=moment)
    assert end == moment


def test_window_collapses_when_period_starts_today() -> None:
    period = ReportPeriod.single(date(2026, 11, 23))
    start, end = resolve_period_window(
        period, SH, now=datetime(2026, 11, 22, 16, 0, tzinfo=UTC)
    )
    assert start == end


# ---------------------------------------------------------------- 选择器
def test_completion_selects_both_resources() -> None:
    selection = ReportSelection.completion()
    assert selection.resources == ("issues", "pulls")
    assert selection.issue_axis == "closed"
    assert selection.pull_axis == "merged"
    assert selection.completion_mode is True
    assert selection.stem_suffix() == ""
    assert selection.fingerprint_payload() == {"issue_scope": "closed", "pull_scope": "merged"}


def test_single_resource_selections_narrow_resources_and_suffix() -> None:
    merged = ReportSelection.merged_only()
    assert merged.resources == ("pulls",)
    assert merged.issue_axis is None
    assert merged.stem_suffix() == ".merged"

    closed = ReportSelection.closed_only()
    assert closed.resources == ("issues",)
    assert closed.pull_axis is None
    assert closed.stem_suffix() == ".closed"


def test_activity_selection_keeps_legacy_semantics() -> None:
    selection = ReportSelection.activity()
    assert selection.resources == ("issues", "pulls")
    assert selection.issue_axis == "updated"
    assert selection.pull_axis == "updated"
    assert selection.completion_mode is False
    assert selection.stem_suffix() == ".activity"


def test_empty_selection_is_rejected() -> None:
    with pytest.raises(ValueError):
        ReportSelection(IssueScope.NONE, PullScope.NONE)


def test_predicate_is_half_open_and_ignores_missing_completion_time() -> None:
    selection = ReportSelection.completion()
    start, end = ts(1), ts(15)

    assert selection.keeps_issue(FakeIssue(updated_at=ts(20), closed_at=ts(1)), start, end)
    assert not selection.keeps_issue(FakeIssue(updated_at=ts(20), closed_at=ts(15)), start, end)
    assert not selection.keeps_issue(FakeIssue(updated_at=ts(10), closed_at=None), start, end)
    assert selection.issue_time(FakeIssue(updated_at=ts(20), closed_at=ts(5))) == ts(5)


def test_merged_in_window_still_kept_when_commented_after_window() -> None:
    """区间内合并、区间后又被评论的 PR 必须收——这正是完成口径唯一的价值。"""
    selection = ReportSelection.completion()
    pull = FakePull(updated_at=ts(20), merged_at=ts(10), state="closed")
    assert selection.keeps_pull(pull, ts(1), ts(15)) is True


def test_merged_before_window_is_dropped_even_if_commented_inside() -> None:
    selection = ReportSelection.completion()
    # 10-20 合并、11-05 区间内被评论：updated_at 落在区间，但它不是本期交付
    pull = FakePull(
        updated_at=ts(5),
        merged_at=datetime(2026, 10, 20, tzinfo=UTC),
        state="closed",
    )
    assert selection.keeps_pull(pull, ts(1), ts(15)) is False


def test_activity_scope_uses_updated_at() -> None:
    selection = ReportSelection.activity()
    # 活动口径下「区间内合并、区间后被评论」按 updated_at 判定，落在区间外就不收
    pull = FakePull(updated_at=ts(20), merged_at=ts(10), state="closed")
    assert selection.keeps_pull(pull, ts(1), ts(15)) is False
    assert selection.pull_time(pull) == ts(20)


def test_criteria_mentions_each_requested_axis_only() -> None:
    lines = ReportSelection.completion().criteria(
        period_label="2026-11-01 ~ 2026-11-15", timezone="Asia/Shanghai"
    )
    text = "\n".join(lines)
    assert "`closed_at`" in text
    assert "`merged_at`" in text
    assert "翻页" in text

    merged_only = ReportSelection.merged_only().criteria(
        period_label="2026-11-01 ~ 2026-11-15", timezone="Asia/Shanghai"
    )
    assert "`closed_at`" not in "\n".join(merged_only)


def test_cli_flags_match_stem_suffix_partition() -> None:
    """提示里的命令必须与文件名后缀一一对应，否则提示本身就是死循环。"""
    cases = [
        (ReportSelection.completion(), "", ""),
        (ReportSelection.closed_only(), ".closed", " --closed"),
        (ReportSelection.merged_only(), ".merged", " --merged"),
        (ReportSelection.activity(), ".activity", " --activity"),
    ]
    for selection, suffix, flags in cases:
        assert selection.stem_suffix() == suffix
        assert selection.cli_flags() == flags
