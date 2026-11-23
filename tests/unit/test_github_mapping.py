from __future__ import annotations

from datetime import UTC, datetime

from sentinel.infrastructure.github.mapping import (
    commit_to_event,
    has_next_link,
    issue_to_event,
    labels_of,
    parse_commit,
    parse_issue,
    parse_pull,
    parse_release,
    parse_ts,
    pull_to_event,
)


def issue_payload(number: int = 1, **overrides):
    payload = {
        "number": number,
        "title": f"议题 {number}",
        "html_url": f"https://github.com/acme/widgets/issues/{number}",
        "user": {"login": "alice"},
        "state": "open",
        "labels": [{"name": "bug"}, {"name": "security"}],
        "created_at": "2026-11-20T01:00:00Z",
        "updated_at": "2026-11-23T09:00:00Z",
        "closed_at": None,
        "comments": 3,
        "body": "现象：偶发中断。\n期望：自动重试。",
    }
    payload.update(overrides)
    return payload


def pull_payload(number: int = 10, **overrides):
    payload = {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "user": {"login": "bob"},
        "state": "open",
        "draft": False,
        "labels": [{"name": "enhancement"}],
        "created_at": "2026-11-21T01:00:00Z",
        "updated_at": "2026-11-23T10:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "comments": 2,
        "review_comments": 5,
        "additions": 120,
        "deletions": 30,
        "changed_files": 5,
        "body": "把采集与渲染解耦。",
    }
    payload.update(overrides)
    return payload


def test_parse_ts_handles_z_and_naive_values() -> None:
    assert parse_ts("2026-11-23T12:00:00Z") == datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    assert parse_ts("2026-11-23T12:00:00") == datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    assert parse_ts(None) is None
    assert parse_ts("not-a-date") is None


def test_issues_endpoint_pull_requests_are_dropped() -> None:
    """/issues 会把 PR 一起返回，带 pull_request 字段的必须丢弃。"""
    assert parse_issue({"number": 1, "pull_request": {"url": "x"}}) is None
    record = parse_issue(issue_payload(7))
    assert record is not None
    assert record.number == 7
    assert record.labels == ("bug", "security")
    assert record.is_closed is False


def test_labels_accept_strings_and_objects() -> None:
    assert labels_of({"labels": ["bug", {"name": "docs"}, {"id": 1}]}) == ("bug", "docs")


def test_issue_without_timestamps_is_dropped() -> None:
    assert parse_issue(issue_payload(updated_at=None)) is None


def test_merged_pull_is_normalised_to_merged_state() -> None:
    record = parse_pull(
        pull_payload(10, state="closed", merged_at="2026-11-23T10:00:00Z")
    )
    assert record is not None
    assert record.is_merged is True
    assert record.status_label == "merged"
    event = pull_to_event(record, repo_full_name="acme/widgets", subscription_id=3)
    assert event.state == "merged"
    assert event.kind.value == "pull_request"


def test_draft_pull_gets_draft_label() -> None:
    record = parse_pull(pull_payload(11, draft=True))
    assert record is not None
    assert record.status_label == "draft"


def test_commit_message_first_line_becomes_title() -> None:
    record = parse_commit(
        {
            "sha": "a" * 40,
            "html_url": "https://github.com/acme/widgets/commit/aaaa",
            "commit": {
                "message": "fix: 修正分页边界\n\n详细说明第一行\n第二行",
                "author": {"name": "Carol", "date": "2026-11-23T08:00:00Z"},
            },
            "author": {"login": "carol"},
        }
    )
    assert record is not None
    assert record.title == "fix: 修正分页边界"
    assert record.author == "carol"
    assert record.body_excerpt and "详细说明" in record.body_excerpt
    event = commit_to_event(record, repo_full_name="acme/widgets")
    assert event.external_id == "a" * 40


def test_release_falls_back_to_created_at() -> None:
    record = parse_release(
        {"tag_name": "v1.2.3", "created_at": "2026-11-23T07:00:00Z", "author": {"login": "d"}}
    )
    assert record is not None
    assert record.published_at == datetime(2026, 11, 23, 7, 0, tzinfo=UTC)
    assert parse_release({"tag_name": "v1"}) is None


def test_issue_to_event_keeps_updated_at_as_version_source() -> None:
    record = parse_issue(issue_payload(5))
    assert record is not None
    event = issue_to_event(record, repo_full_name="acme/widgets")
    assert event.source_updated_at == record.updated_at
    assert event.authored_at == record.created_at


def test_has_next_link_reads_rel_next_only() -> None:
    assert has_next_link('<https://x?page=2>; rel="next"') is True
    assert has_next_link('<https://x?page=1>; rel="prev", <https://x?page=2>; rel="next"') is True
    assert has_next_link('<https://x?page=1>; rel="prev"') is False
    assert has_next_link(None) is False
