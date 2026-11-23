from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from sentinel.config import Settings
from sentinel.main import create_app

HEADERS = {"X-API-Key": "dev-key"}


@pytest.fixture
async def api(settings: Settings):
    app = create_app(settings)
    container = app.state.container
    await container.startup()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://sentinel.test") as client:
            yield client, container
    finally:
        await container.shutdown()


async def test_health_is_public(api) -> None:
    client, _ = api
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    ready = await client.get("/readyz")
    assert ready.json()["checks"]["db"] == "ok"


async def test_api_requires_key(api) -> None:
    client, _ = api
    response = await client.get("/api/v1/subscriptions")
    assert response.status_code == 401


async def test_full_flow(api, make_event, monkeypatch) -> None:
    client, container = api

    # 冻住时钟且取秒数 30：报告窗口起点是「当前分钟整分」，若用真实时间且恰好跑在
    # 整分后 1~4 秒，now-3s 就会落进上一分钟而被窗口排除，event_count 随机变成 2。
    now = datetime(2026, 11, 23, 8, 41, 30, tzinfo=UTC)
    monkeypatch.setattr(container.clock, "now", lambda: now)

    created = await client.post(
        "/api/v1/subscriptions",
        json={"repo_full_name": "fastapi/fastapi", "cron": "* * * * *", "title": "FastAPI"},
        headers=HEADERS,
    )
    assert created.status_code == 201, created.text
    sub = created.json()
    sub_id = sub["id"]
    assert sub["cron"] == "* * * * *"
    assert sub["next_run_at"] is not None

    collected = await client.post(f"/api/v1/subscriptions/{sub_id}/collect", headers=HEADERS)
    assert collected.status_code == 200, collected.text
    assert collected.json()["inserted"] > 0

    # 再跑一次：要么命中 304，要么新增但绝不重复
    again = await client.post(f"/api/v1/subscriptions/{sub_id}/collect", headers=HEADERS)
    assert again.status_code == 200

    now = container.clock.now()
    await container.events.add_many(
        [
            make_event(
                repo="fastapi/fastapi",
                sub_id=sub_id,
                external_id=f"api-{index}",
                authored_at=now - timedelta(seconds=3 + index),
            )
            for index in range(3)
        ]
    )

    digest = await client.post(f"/api/v1/subscriptions/{sub_id}/digest", headers=HEADERS)
    assert digest.status_code == 200, digest.text
    report_id = digest.json()["report_id"]
    assert report_id, digest.text

    report = await client.get(f"/api/v1/reports/{report_id}", headers=HEADERS)
    assert report.json()["event_count"] >= 3

    html = await client.get(f"/api/v1/reports/{report_id}/html", headers=HEADERS)
    assert html.status_code == 200
    assert "<html" in html.text.lower()
    assert "fastapi/fastapi" in html.text

    listing = await client.get("/api/v1/reports", headers=HEADERS)
    assert listing.json()["total"] >= 1

    runs = await client.get("/api/v1/runs", headers=HEADERS)
    assert runs.json()["total"] >= 2

    meta = await client.get("/api/v1/meta", headers=HEADERS)
    assert meta.json()["github_mode"] == "fake"


async def test_subscription_crud(api) -> None:
    client, _ = api
    created = await client.post(
        "/api/v1/subscriptions",
        json={"repo_full_name": "acme/widgets", "cron": "0 9 * * mon"},
        headers=HEADERS,
    )
    sub_id = created.json()["id"]

    patched = await client.patch(
        f"/api/v1/subscriptions/{sub_id}",
        json={"cron": "30 8 * * *", "label_exclude": ["wontfix"]},
        headers=HEADERS,
    )
    assert patched.status_code == 200
    assert patched.json()["cron"] == "30 8 * * *"

    disabled = await client.post(f"/api/v1/subscriptions/{sub_id}/disable", headers=HEADERS)
    assert disabled.json()["enabled"] is False

    deleted = await client.delete(f"/api/v1/subscriptions/{sub_id}", headers=HEADERS)
    assert deleted.status_code == 204

    missing = await client.get(f"/api/v1/subscriptions/{sub_id}", headers=HEADERS)
    assert missing.status_code == 404


async def test_invalid_cron_is_rejected(api) -> None:
    client, _ = api
    response = await client.post(
        "/api/v1/subscriptions",
        json={"repo_full_name": "acme/widgets", "cron": "0 9 * * 1"},
        headers=HEADERS,
    )
    assert response.status_code == 422


async def test_dashboard_requires_login_then_renders(api) -> None:
    """看板用 Basic Auth，密码就是 API Key；未登录时 401。"""
    client, _ = api
    anonymous = await client.get("/")
    assert anonymous.status_code == 401

    page = await client.get("/", auth=("sentinel", "dev-key"))
    assert page.status_code == 200
    assert "GitHub" in page.text
    assert "最近报告" in page.text
