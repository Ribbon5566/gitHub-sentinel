from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sentinel.domain.errors import ConflictError
from sentinel.domain.models import DigestReport, Subscription
from sentinel.ports.github import FetchQuery


async def make_sub(container, *, repo: str = "acme/widgets", **kwargs) -> Subscription:
    return await container.subscription_service.create(
        Subscription(repo_full_name=repo, timezone="Asia/Shanghai", **kwargs)
    )


async def test_add_many_is_idempotent(container, make_event) -> None:
    sub = await make_sub(container)
    events = [make_event(sub_id=sub.id, external_id=f"c{index}") for index in range(3)]

    first = await container.events.add_many(events)
    second = await container.events.add_many(events)

    assert (first.inserted, first.skipped) == (3, 0)
    assert (second.inserted, second.skipped) == (0, 3)
    assert await container.events.count_for(sub.id or 0) == 3


async def test_add_many_dedupes_within_batch(container, make_event) -> None:
    sub = await make_sub(container)
    same = make_event(sub_id=sub.id, external_id="dup")
    result = await container.events.add_many([same, same])
    assert result.inserted == 1


async def test_claim_due_is_exclusive(container) -> None:
    sub = await make_sub(container)
    now = container.clock.now()
    await container.subs.set_next_run(sub.id or 0, now - timedelta(minutes=1))

    first = await container.subs.claim_due(now, limit=5, lease_seconds=60)
    second = await container.subs.claim_due(now, limit=5, lease_seconds=60)

    assert [item.id for item in first] == [sub.id]
    assert second == []


async def test_claim_due_skips_disabled(container) -> None:
    sub = await make_sub(container)
    now = container.clock.now()
    await container.subs.set_next_run(sub.id or 0, now - timedelta(minutes=1))
    await container.subscription_service.set_enabled(sub.id or 0, False)

    assert await container.subs.claim_due(now, limit=5) == []


async def test_report_window_is_unique(container) -> None:
    sub = await make_sub(container)
    start = datetime(2026, 11, 23, 1, 0, tzinfo=UTC)
    end = datetime(2026, 11, 23, 2, 0, tzinfo=UTC)
    report = DigestReport(
        subscription_id=sub.id or 0,
        repo_full_name=sub.repo_full_name,
        period_start=start,
        period_end=end,
        content_hash="abc",
        title="t",
    )
    await container.reports.add(report)

    with pytest.raises(ConflictError):
        await container.reports.add(report.model_copy(update={"id": None}))

    found = await container.reports.find_by_window(sub.id or 0, start, end)
    assert found is not None and found.content_hash == "abc"


async def test_collect_advances_watermark_without_duplicates(container) -> None:
    sub = await make_sub(container)

    first = await container.collect_service.collect(sub.id or 0)
    second = await container.collect_service.collect(sub.id or 0)

    assert first.bootstrap is True
    assert second.bootstrap is False
    assert first.inserted > 0
    # 两次插入之和等于库里的总数，说明没有重复入库
    assert await container.events.count_for(sub.id or 0) == first.inserted + second.inserted

    cursor = await container.cursors.get(sub.id or 0, "commits")
    assert cursor is not None and cursor.watermark is not None


async def test_fake_github_honours_etag(container) -> None:
    """304 分支必须在本地就能被验证，否则「不重复入库」只是纸面承诺。"""
    since = container.clock.now() - timedelta(days=1)
    query = FetchQuery(repo_full_name="acme/widgets", resource="commits", since=since)

    first = await container.github.fetch(query)
    assert first.events and first.etag

    again = await container.github.fetch(
        FetchQuery(
            repo_full_name="acme/widgets",
            resource="commits",
            since=since,
            etag=first.etag,
        )
    )
    assert again.not_modified is True
    assert again.events == []


async def test_digest_is_idempotent_for_same_content(
    container, make_event, monkeypatch
) -> None:
    # 必须冻住时钟：报告窗口起点是「上一分钟的整分」，两次 build 之间一旦跨过分钟边界，
    # period_start 就会变，幂等复用自然查不到——那会让这个用例随跑测时间随机失败。
    now = datetime(2026, 11, 23, 1, 30, 15, tzinfo=UTC)
    monkeypatch.setattr(container.clock, "now", lambda: now)

    sub = await make_sub(container, cron="* * * * *")
    await container.events.add_many(
        [make_event(sub_id=sub.id, external_id=f"e{i}", authored_at=now - timedelta(seconds=5))
         for i in range(3)]
    )

    first = await container.digest_service.build(sub.id or 0)
    second = await container.digest_service.build(sub.id or 0)

    assert first.report is not None
    assert second.report is not None
    assert second.reused is True
    assert second.report.id == first.report.id
    assert await container.reports.count(subscription_id=sub.id or 0) == 1
