from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentinel.domain.digest import build_digest_input, content_hash
from sentinel.domain.enums import EventKind
from sentinel.domain.models import Subscription
from sentinel.infrastructure.llm.mock import MockLLM


def make_input(**kwargs):
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    sub = Subscription(repo_full_name="acme/widgets", title="Widgets", timezone="Asia/Shanghai")
    events = [
        kwargs["factory"](
            kind=EventKind.COMMIT, external_id="c1", authored_at=now - timedelta(hours=2)
        ),
        kwargs["factory"](
            kind=EventKind.PULL_REQUEST,
            external_id="12",
            title="feat: 支持周报",
            authored_at=now - timedelta(hours=1),
            state="merged",
        ),
        kwargs["factory"](
            kind=EventKind.RELEASE,
            external_id="v1.2.0",
            title="Release v1.2.0",
            authored_at=now - timedelta(minutes=30),
        ),
    ]
    return build_digest_input(sub, events, now - timedelta(days=1), now)


async def test_mock_summary_is_deterministic(make_event) -> None:
    request = make_input(factory=make_event)
    first = await MockLLM().summarize(request)
    second = await MockLLM().summarize(request)
    assert first == second


async def test_mock_summary_reports_real_counts(make_event) -> None:
    request = make_input(factory=make_event)
    summary = await MockLLM().summarize(request)
    assert "Widgets" in summary.headline
    assert "3 条" in summary.headline
    assert summary.model == "mock-rules"
    assert 1 <= len(summary.highlights) <= 5


async def test_mock_summary_lists_release_first(make_event) -> None:
    request = make_input(factory=make_event)
    summary = await MockLLM().summarize(request)
    assert "Release" in summary.highlights[0]


async def test_empty_input_is_handled(make_event) -> None:
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    sub = Subscription(repo_full_name="acme/widgets", timezone="Asia/Shanghai")
    request = build_digest_input(sub, [], now - timedelta(days=1), now)
    summary = await MockLLM().summarize(request)
    assert "没有新动态" in summary.headline


def test_content_hash_order_independent(make_event) -> None:
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    a = make_event(external_id="a", authored_at=now)
    b = make_event(external_id="b", authored_at=now)
    assert content_hash([a, b]) == content_hash([b, a])
    assert content_hash([a, b]) != content_hash([a])
