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


# ------------------------------------------------- v3.0 研发周期报告接口
PERIOD_BODY = {
    "repo_full_name": "acme/widgets",
    "date_from": "2026-11-01",
    "date_to": "2026-11-15",
    "scope": "completion",
}


@pytest.fixture
def frozen_clock(monkeypatch):
    """冻住时钟：区间终点必须落在「今天」之前，否则会被 422 挡掉。"""

    def apply(container) -> datetime:
        moment = datetime(2026, 11, 23, 8, 41, 30, tzinfo=UTC)
        monkeypatch.setattr(container.clock, "now", lambda: moment)
        return moment

    return apply


async def test_period_report_writes_progress_and_report(api, frozen_clock) -> None:
    client, container = api
    frozen_clock(container)

    response = await client.post("/api/v1/daily/period", json=PERIOD_BODY, headers=HEADERS)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["period_label"] == "2026-11-01 ~ 2026-11-15"
    assert payload["scope"] == "completion"
    assert "完成口径" in payload["scope_label"]
    assert payload["progress_path"].endswith("2026-11-01_to_2026-11-15.md")
    assert payload["report_path"].endswith("2026-11-01_to_2026-11-15-report.md")
    # 完成口径的统计必须只有交付类指标
    assert "issues_closed" in payload["stats"]
    assert payload["stats"]["pulls_merged"] >= 1
    assert any("closed_at" in line for line in payload["criteria"])
    # mock 模型不支持自由文本，走规则兜底；接口要把这件事如实告诉调用方
    assert payload["report_model"] == "rules-fallback"
    assert payload["report_degraded"] is True


async def test_period_report_can_skip_llm(api, frozen_clock) -> None:
    client, container = api
    frozen_clock(container)

    response = await client.post(
        "/api/v1/daily/period",
        json={**PERIOD_BODY, "include_report": False},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["report_path"] is None
    assert payload["progress_path"].endswith(".md")


async def test_period_report_scope_merged_uses_own_file(api, frozen_clock) -> None:
    """不同口径必须落不同文件，否则两份交付清单会互相覆盖。"""
    client, container = api
    frozen_clock(container)

    response = await client.post(
        "/api/v1/daily/period",
        json={**PERIOD_BODY, "scope": "merged", "include_report": False},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["progress_path"].endswith("2026-11-01_to_2026-11-15.merged.md")
    assert payload["scope_label"] == "完成口径（PR `merged_at`）"
    # 只要 PR 时不该再有议题口径说明
    assert all("议题" not in line for line in payload["criteria"])


async def test_period_report_is_idempotent(api, frozen_clock) -> None:
    """同一份数据重复调用应复用进展文件，不重复写盘、不重复调模型。"""
    client, container = api
    frozen_clock(container)

    first = await client.post("/api/v1/daily/period", json=PERIOD_BODY, headers=HEADERS)
    second = await client.post("/api/v1/daily/period", json=PERIOD_BODY, headers=HEADERS)
    assert second.status_code == 200, second.text
    assert second.json()["progress_reused"] is True
    assert second.json()["progress_path"] == first.json()["progress_path"]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"date_from": "2026-11-15", "date_to": "2026-11-01"}, "晚于"),
        ({"date_to": "2030-01-01"}, "还没到"),
        ({"date_from": "2020-01-01", "date_to": "2026-11-01"}, "超过上限"),
    ],
)
async def test_period_report_rejects_bad_range(api, frozen_clock, overrides, fragment) -> None:
    client, container = api
    frozen_clock(container)

    response = await client.post(
        "/api/v1/daily/period", json={**PERIOD_BODY, **overrides}, headers=HEADERS
    )
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


async def test_period_report_rejects_unknown_scope(api, frozen_clock) -> None:
    client, container = api
    frozen_clock(container)

    response = await client.post(
        "/api/v1/daily/period", json={**PERIOD_BODY, "scope": "opened"}, headers=HEADERS
    )
    assert response.status_code == 422


async def test_period_report_requires_key(api) -> None:
    client, _ = api
    response = await client.post("/api/v1/daily/period", json=PERIOD_BODY)
    assert response.status_code == 401
