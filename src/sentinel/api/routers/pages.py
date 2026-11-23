from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ...domain.errors import NotFoundError
from ..schemas import ContainerDep

router = APIRouter(tags=["pages"], include_in_schema=False)
_basic = HTTPBasic(auto_error=False)


async def _page_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    """看板是给人看的，用 Basic Auth；密码就是 API Key，不额外引入用户体系。

    未配置 API Key 时不校验（此时 API 侧也只允许本机访问）。
    """
    expected = request.app.state.container.settings.api_key
    if not expected:
        return
    if credentials is None or not secrets.compare_digest(credentials.password, expected):
        raise HTTPException(
            status_code=401,
            detail="需要登录",
            headers={"WWW-Authenticate": 'Basic realm="GitHub Sentinel"'},
        )


def _render(container, template: str, **context) -> HTMLResponse:  # noqa: ANN001
    payload = {"settings": container.settings, **context}
    return HTMLResponse(container.renderer.render_page(template, payload))


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(_page_auth)])
async def dashboard(container: ContainerDep) -> HTMLResponse:
    subs = await container.subs.list(limit=20)
    reports = await container.reports.list(limit=10)
    runs = await container.runs.list(limit=8)
    counts = {
        "subscriptions": await container.subs.count(),
        "reports": await container.reports.count(),
        "runs": await container.runs.count(),
        "active": len([sub for sub in subs if sub.enabled]),
    }
    return _render(
        container,
        "dashboard.html",
        subs=subs,
        reports=reports,
        runs=runs,
        counts=counts,
        active_page="dashboard",
    )


@router.get("/subscriptions", response_class=HTMLResponse, dependencies=[Depends(_page_auth)])
async def subscriptions_page(container: ContainerDep) -> HTMLResponse:
    subs = await container.subs.list(limit=100)
    return _render(
        container, "subscriptions.html", subs=subs, active_page="subscriptions"
    )


@router.get("/reports/{report_id}", response_class=HTMLResponse, dependencies=[Depends(_page_auth)])
async def report_page(report_id: int, container: ContainerDep) -> HTMLResponse:
    report = await container.reports.get(report_id)
    if report is None:
        raise NotFoundError(f"报告 {report_id} 不存在")
    path = container.settings.resolved_path(report.html_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告文件已丢失")
    return HTMLResponse(path.read_text(encoding="utf-8"))
