from __future__ import annotations

from sentinel.domain.enums import EventKind
from sentinel.domain.filters import EventFilter, filter_events
from sentinel.domain.models import Subscription


def make_sub(**kwargs) -> Subscription:
    return Subscription(repo_full_name="acme/widgets", timezone="Asia/Shanghai", **kwargs)


def test_watch_limits_kinds(make_event) -> None:
    rule = EventFilter.from_subscription(make_sub(watch=frozenset({EventKind.COMMIT})))
    assert rule.accepts(make_event(kind=EventKind.COMMIT))
    assert not rule.accepts(make_event(kind=EventKind.RELEASE))


def test_bots_are_skipped_by_default(make_event) -> None:
    rule = EventFilter.from_subscription(make_sub())
    assert not rule.accepts(make_event(author="dependabot[bot]"))
    assert not rule.accepts(make_event(author="renovate"))
    assert rule.accepts(make_event(author="alice"))


def test_bots_can_be_enabled(make_event) -> None:
    rule = EventFilter.from_subscription(make_sub(skip_bots=False))
    assert rule.accepts(make_event(author="dependabot[bot]"))


def test_label_exclude_wins(make_event) -> None:
    rule = EventFilter.from_subscription(make_sub(label_exclude=("Wontfix",)))
    assert not rule.accepts(make_event(labels=("wontfix",)))
    assert rule.accepts(make_event(labels=("bug",)))


def test_label_include_is_a_whitelist(make_event) -> None:
    rule = EventFilter.from_subscription(make_sub(label_include=("breaking-change",)))
    assert rule.accepts(make_event(labels=("Breaking-Change",)))
    assert not rule.accepts(make_event(labels=("docs",)))
    assert not rule.accepts(make_event(labels=()))


def test_keyword_include_matches_title_or_body(make_event) -> None:
    rule = EventFilter.from_subscription(make_sub(keyword_include=("security",)))
    assert rule.accepts(make_event(title="fix security hole"))
    assert not rule.accepts(make_event(title="update docs"))


def test_keyword_exclude(make_event) -> None:
    rule = EventFilter.from_subscription(make_sub(keyword_exclude=("typo",)))
    assert not rule.accepts(make_event(title="fix typo in readme"))
    assert rule.accepts(make_event(title="add retry"))


def test_author_exclude(make_event) -> None:
    rule = EventFilter.from_subscription(make_sub(author_exclude=("alice",)))
    assert not rule.accepts(make_event(author="alice"))
    assert rule.accepts(make_event(author="bob"))


def test_filter_events_counts_dropped(make_event) -> None:
    rule = EventFilter.from_subscription(make_sub(watch=frozenset({EventKind.ISSUE})))
    events = [
        make_event(kind=EventKind.ISSUE),
        make_event(kind=EventKind.COMMIT),
        make_event(kind=EventKind.RELEASE),
    ]
    kept, dropped = filter_events(events, rule)
    assert len(kept) == 1
    assert dropped == 2
