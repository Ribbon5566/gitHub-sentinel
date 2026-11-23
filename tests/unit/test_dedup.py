from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentinel.domain.dedup import dedup_key, event_version
from sentinel.domain.enums import EventKind
from sentinel.domain.models import RepoEvent


def make(kind: EventKind, external_id: str, *, updated: datetime | None = None) -> RepoEvent:
    moment = updated or datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    return RepoEvent(
        repo_full_name="acme/widgets",
        kind=kind,
        external_id=external_id,
        title="t",
        authored_at=moment,
        source_updated_at=moment,
    )


def test_commit_identity_is_sha() -> None:
    event = make(EventKind.COMMIT, "abc123")
    assert event.version == "sha"
    assert event.dedup_key == dedup_key("acme/widgets", EventKind.COMMIT, "abc123", "sha")


def test_same_commit_twice_same_key() -> None:
    a = make(EventKind.COMMIT, "abc123")
    b = make(EventKind.COMMIT, "abc123", updated=datetime(2026, 11, 24, tzinfo=UTC))
    assert a.dedup_key == b.dedup_key


def test_issue_state_change_produces_new_key_but_same_query_key() -> None:
    """issue 关闭必须能再次播报，但不能被当成两个不同 issue。"""
    opened = make(EventKind.ISSUE, "42")
    closed = make(
        EventKind.ISSUE, "42", updated=datetime(2026, 11, 23, 10, 0, tzinfo=UTC) + timedelta(hours=2)
    )
    assert opened.query_key == closed.query_key
    assert opened.dedup_key != closed.dedup_key


def test_version_is_second_precision() -> None:
    """同一秒内的 updated_at 变化视为同一版本，避免抖动导致重复。"""
    base = datetime(2026, 11, 23, 10, 0, 0, tzinfo=UTC)
    a = make(EventKind.ISSUE, "7", updated=base)
    b = make(EventKind.ISSUE, "7", updated=base.replace(microsecond=800_000))
    assert a.version == b.version


def test_release_version_uses_publish_time() -> None:
    moment = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    assert event_version(EventKind.RELEASE, authored_at=moment).startswith("2026-11-20T08:00")


def test_different_repos_never_collide() -> None:
    left = dedup_key("acme/left", EventKind.COMMIT, "sha", "sha")
    right = dedup_key("acme/right", EventKind.COMMIT, "sha", "sha")
    assert left != right
