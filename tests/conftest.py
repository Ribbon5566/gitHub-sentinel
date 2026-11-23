from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from sentinel.config import Settings
from sentinel.container import Container
from sentinel.domain.enums import EventKind
from sentinel.domain.models import RepoEvent


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        api_key="dev-key",
        github_mode="fake",
        llm_provider="mock",
        notify_channel="null",
        enable_scheduler=False,
        timezone="Asia/Shanghai",
        default_cron="0 9 * * *",
        log_level="WARNING",
    )


@pytest.fixture
async def container(settings: Settings):
    c = Container(settings)
    await c.startup()
    try:
        yield c
    finally:
        await c.shutdown()


def build_event(
    *,
    repo: str = "acme/widgets",
    kind: EventKind = EventKind.COMMIT,
    external_id: str | None = None,
    authored_at: datetime | None = None,
    sub_id: int | None = None,
    title: str = "fix: 修正分页边界",
    state: str | None = None,
    labels: tuple[str, ...] = (),
    author: str = "alice",
) -> RepoEvent:
    moment = authored_at or datetime.now(UTC)
    return RepoEvent(
        subscription_id=sub_id,
        repo_full_name=repo,
        kind=kind,
        external_id=external_id or uuid.uuid4().hex[:10],
        title=title,
        url=f"https://github.com/{repo}/commit/x",
        author=author,
        authored_at=moment,
        source_updated_at=moment if kind is not EventKind.COMMIT else None,
        state=state,
        labels=labels,
        body_excerpt="改动涉及采集编排与报告模板。",
    )


@pytest.fixture
def make_event():
    return build_event
