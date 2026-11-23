from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sentinel.domain.errors import ValidationError
from sentinel.domain.progress import (
    CommitRecord,
    IssueRecord,
    PullRecord,
    ReleaseRecord,
    build_daily_progress,
    detect_risks,
    rank_highlights,
    repo_slug,
    resolve_day_window,
    summary_line,
)
from sentinel.domain.selection import ReportSelection

SH = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 11, 23)
NOON = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def make_issue(
    number: int = 1,
    *,
    state: str = "open",
    author: str = "alice",
    updated: datetime | None = None,
    closed: datetime | None = None,
    labels: tuple[str, ...] = (),
    comments: int = 0,
) -> IssueRecord:
    moment = updated or NOON
    return IssueRecord(
        number=number,
        title=f"议题 {number}",
        author=author,
        state=state,
        labels=labels,
        created_at=moment,
        updated_at=moment,
        closed_at=closed,
        comments=comments,
    )


def make_pull(
    number: int = 10,
    *,
    merged: bool = False,
    merged_at: datetime | None = None,
    draft: bool = False,
    author: str = "bob",
    updated: datetime | None = None,
    labels: tuple[str, ...] = (),
) -> PullRecord:
    moment = updated or NOON
    stamp = merged_at or (moment if merged else None)
    return PullRecord(
        number=number,
        title=f"PR {number}",
        author=author,
        state="closed" if stamp else "open",
        draft=draft,
        labels=labels,
        created_at=moment,
        updated_at=moment,
        merged_at=stamp,
    )


def make_commit(sha: str = "a1b2c3d", *, author: str = "carol") -> CommitRecord:
    return CommitRecord(sha=sha, title=f"提交 {sha}", author=author, committed_at=NOON)


def make_release(tag: str = "v2.1.0") -> ReleaseRecord:
    return ReleaseRecord(tag=tag, name=f"Release {tag}", published_at=NOON)


def build(**overrides):
    start, end = resolve_day_window(DAY, SH)
    payload = {
        "repo_full_name": "fastapi/fastapi",
        "repo_title": "FastAPI",
        "day": DAY,
        "timezone": "Asia/Shanghai",
        "window_start": start,
        "window_end": end,
        "generated_at": end,
    }
    payload.update(overrides)
    return build_daily_progress(**payload)


def test_day_window_is_local_natural_day_in_utc() -> None:
    """Asia/Shanghai 的 11-23 全天 = UTC 11-22 16:00 ~ 11-23 16:00。"""
    start, end = resolve_day_window(DAY, SH)
    assert start == datetime(2026, 11, 22, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 23, 16, 0, tzinfo=UTC)


def test_day_window_is_clipped_to_now_for_today() -> None:
    """当天报告不该把未来几小时算进窗口。"""
    moment = datetime(2026, 11, 23, 3, 30, tzinfo=UTC)
    start, end = resolve_day_window(DAY, SH, now=moment)
    assert start == datetime(2026, 11, 22, 16, 0, tzinfo=UTC)
    assert end == moment


def test_day_window_collapses_for_future_day() -> None:
    start, end = resolve_day_window(DAY, SH, now=datetime(2026, 11, 20, tzinfo=UTC))
    assert start == end


def test_repo_slug_is_readable_and_unambiguous() -> None:
    assert repo_slug("fastapi/fastapi") == "fastapi__fastapi"
    assert repo_slug("my-org/my.repo-name") == "my-org__my.repo-name"
    assert repo_slug("a_b/c__d") != repo_slug("a__b/c_d")
    assert "%5F" in repo_slug("a_b/c")


def test_repo_slug_rejects_broken_input() -> None:
    with pytest.raises(ValidationError):
        repo_slug("fastapi")


def test_stats_count_states_and_contributors() -> None:
    progress = build(
        issues=[make_issue(1), make_issue(2, state="closed", author="bob")],
        pulls=[make_pull(10, merged=True), make_pull(11, draft=True)],
        commits=[make_commit("aaa", author="alice"), make_commit("bbb", author="carol")],
        releases=[make_release()],
    )
    assert progress.stats["issues_total"] == 2
    assert progress.stats["issues_open"] == 1
    assert progress.stats["issues_closed"] == 1
    assert progress.stats["pulls_total"] == 2
    assert progress.stats["pulls_merged"] == 1
    assert progress.stats["pulls_draft"] == 1
    assert progress.stats["commits_total"] == 2
    assert progress.stats["releases_total"] == 1
    assert progress.stats["contributors"] == 3


def test_bots_are_filtered_and_counted() -> None:
    progress = build(
        issues=[make_issue(1, author="dependabot[bot]"), make_issue(2, author="alice")],
        skip_bots=True,
    )
    assert len(progress.issues) == 1
    assert progress.stats["bots_filtered"] == 1


def test_order_is_newest_first() -> None:
    older = NOON - timedelta(hours=3)
    progress = build(issues=[make_issue(1, updated=older), make_issue(2, updated=NOON)])
    assert [item.number for item in progress.issues] == [2, 1]


def test_fingerprint_ignores_generated_at_and_input_order() -> None:
    """同一天同样数据重跑必须得到同一指纹，否则每次都会白调一次模型。"""
    first = build(issues=[make_issue(1), make_issue(2)])
    second = build(
        issues=[make_issue(2), make_issue(1)],
        generated_at=NOON + timedelta(hours=5),
    )
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint


def test_fingerprint_changes_when_content_changes() -> None:
    before = build(issues=[make_issue(1)])
    after = build(issues=[make_issue(1, updated=NOON + timedelta(minutes=10))])
    assert before.fingerprint != after.fingerprint


def test_truncation_marks_resource() -> None:
    progress = build(issues=[make_issue(index) for index in range(5)], max_items=3)
    assert len(progress.issues) == 3
    # 展示裁剪写 trimmed：数据本身是完整的，统计准确，只是表格少几行
    assert "issues" in progress.trimmed
    assert progress.truncated == frozenset()
    assert progress.stats["issues_total"] == 5


def test_page_budget_truncation_stays_in_truncated() -> None:
    """翻页触顶与展示裁剪是两件事：前者意味着统计只是下界。"""
    progress = build(issues=[make_issue(1)], truncated=["issues"])
    assert "issues" in progress.truncated
    assert progress.trimmed == frozenset()


def test_legacy_mode_matches_explicit_activity_selection() -> None:
    """不传 selection 的单日路径，必须与显式活动口径完全一致。"""
    issues = [make_issue(1), make_issue(2, state="closed", updated=NOON - timedelta(hours=1))]
    pulls = [make_pull(10, merged=True), make_pull(11)]

    legacy = build(issues=issues, pulls=pulls)
    activity = build(issues=issues, pulls=pulls, selection=ReportSelection.activity())

    assert legacy.fingerprint == activity.fingerprint
    assert legacy.stats == activity.stats
    assert [item.number for item in legacy.issues] == [item.number for item in activity.issues]
    assert legacy.completion_mode is False


def test_completion_keeps_only_items_completed_inside_window() -> None:
    selection = ReportSelection.completion()
    start, end = resolve_day_window(DAY, SH)

    # 当日关闭、之后（窗口外）又被评论 → 仍属于本日交付
    commented_later = make_issue(2, state="closed", updated=end + timedelta(hours=5), closed=NOON)
    # 昨天关闭、今天被评论 → 不属于本日交付
    closed_yesterday = make_issue(3, state="closed", updated=NOON, closed=start - timedelta(days=1))

    progress = build(issues=[commented_later, closed_yesterday], selection=selection)

    assert [item.number for item in progress.issues] == [2]
    assert progress.issue_axis == "closed_at"
    assert progress.completion_mode is True


def test_completion_sorts_by_completion_time_not_update_time() -> None:
    earlier_merge_but_later_update = make_pull(
        10, merged_at=NOON - timedelta(hours=4), updated=NOON
    )
    later_merge = make_pull(11, merged_at=NOON - timedelta(hours=1), updated=NOON)

    progress = build(
        pulls=[earlier_merge_but_later_update, later_merge],
        selection=ReportSelection.completion(),
    )

    assert [item.number for item in progress.pulls] == [11, 10]


def test_completion_stats_have_no_open_items() -> None:
    progress = build(
        issues=[make_issue(1, state="closed", closed=NOON)],
        pulls=[make_pull(10, merged_at=NOON)],
        selection=ReportSelection.completion(),
    )
    assert progress.stats["issues_open"] == 0
    assert progress.stats["pulls_open"] == 0
    assert progress.stats["pulls_merged"] == 1


def test_fingerprint_ignores_window_end_so_reruns_are_reused() -> None:
    """窗口右端会被裁到「现在」；纳入指纹会让同一天重跑就白调一次模型。"""
    first = build(issues=[make_issue(1)])
    second = build(
        issues=[make_issue(1)], window_end=first.window_end + timedelta(minutes=5)
    )
    assert first.fingerprint == second.fingerprint


def test_fingerprint_reacts_to_completion_time() -> None:
    selection = ReportSelection.completion()
    before = build(issues=[make_issue(1, state="closed", closed=NOON)], selection=selection)
    after = build(
        issues=[make_issue(1, state="closed", closed=NOON - timedelta(hours=2))],
        selection=selection,
    )
    assert before.fingerprint != after.fingerprint


def test_summary_line_switches_to_completion_wording() -> None:
    progress = build(
        issues=[make_issue(1, state="closed", closed=NOON)],
        pulls=[make_pull(10, merged_at=NOON)],
        selection=ReportSelection.completion(),
    )
    line = summary_line(progress)
    assert line.startswith("本期")
    assert "1 个议题关闭" in line
    assert "1 个 PR 合并" in line


def test_risks_in_completion_mode_flag_merged_hot_labels() -> None:
    progress = build(
        pulls=[make_pull(10, merged_at=NOON, labels=("breaking-change",))],
        selection=ReportSelection.completion(),
    )
    risks = detect_risks(progress)
    assert len(risks) == 1
    assert "breaking-change" in risks[0]
    assert "回归验证" in risks[0]


def test_highlights_prioritise_release_then_merge() -> None:
    progress = build(
        issues=[make_issue(1)],
        pulls=[make_pull(10, merged=True), make_pull(11, draft=True)],
        releases=[make_release()],
        commits=[make_commit()],
    )
    highlights = rank_highlights(progress)
    assert highlights[0].startswith("发布 v2.1.0")
    assert highlights[1].startswith("PR #10 已合并")
    assert all("提交" not in item for item in highlights)


def test_risks_flag_open_items_with_hot_labels() -> None:
    progress = build(
        issues=[make_issue(1, labels=("security",)), make_issue(2, state="closed", labels=("bug",))],
        pulls=[make_pull(10, labels=("breaking-change",))],
    )
    risks = detect_risks(progress)
    assert len(risks) == 2
    assert "security" in risks[0]
    assert "breaking-change" in risks[1]


def test_risks_warn_when_no_comments() -> None:
    progress = build(issues=[make_issue(7, comments=0)])
    assert "零评论" in detect_risks(progress)[0]


def test_pull_status_label_covers_merged_draft_open() -> None:
    assert make_pull(1, merged=True).status_label == "merged"
    assert make_pull(2, draft=True).status_label == "draft"
    assert make_pull(3).status_label == "open"


def test_empty_window_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build(window_start=NOON, window_end=NOON)


def test_summary_line_uses_stats() -> None:
    progress = build(issues=[make_issue(1), make_issue(2, state="closed")])
    line = summary_line(progress)
    assert "2 个议题有更新" in line
    assert "1 个关闭" in line
