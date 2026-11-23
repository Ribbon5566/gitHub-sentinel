from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse

from ...domain.errors import NotFoundError
from ..schemas import ContainerDep, NotifyResultOut, PageOut, ReportOut
from ..security import require_api_key
from ._common import to_page

router = APIRouter(prefix="/reports", tags=["reports"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=PageOut[ReportOut])
async def list_reports(
    container: ContainerDep,
    subscription_id: int | None = None,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PageOut[ReportOut]:
    items = await container.reports.list(
        subscription_id=subscription_id, limit=limit, offset=offset
    )
    total = await container.reports.count(subscription_id=subscription_id)
    return to_page(items, total, limit, offset, ReportOut)


@router.get("/{report_id}", response_model=ReportOut)
async def get_report(report_id: int, container: ContainerDep) -> ReportOut:
    report = await container.reports.get(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"报告 {report_id} 不存在")
    return ReportOut.model_validate(report)


@router.get("/{report_id}/html", response_class=HTMLResponse)
async def get_report_html(report_id: int, container: ContainerDep) -> HTMLResponse:
    report = await container.reports.get(report_id)
    if report is None or not report.html_path:
        raise HTTPException(status_code=404, detail=f"报告 {report_id} 没有 HTML 文件")
    path = container.settings.resolved_path(report.html_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告文件已丢失")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.post("/{report_id}/resend", response_model=NotifyResultOut)
async def resend_report(
    report_id: int, container: ContainerDep, resend: bool = True
) -> NotifyResultOut:
    try:
        outcome = await container.notify_service.notify(report_id, resend=resend)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return NotifyResultOut(status=outcome.status, detail=outcome.detail, skipped=outcome.skipped)


@router.get("/{report_id}/text", response_class=HTMLResponse, status_code=status.HTTP_200_OK)
async def get_report_text(report_id: int, container: ContainerDep) -> HTMLResponse:
    """纯文本版，便于在终端里 curl 查看。"""
    report = await container.reports.get(report_id)
    if report is None or not report.text_path:
        raise HTTPException(status_code=404, detail=f"报告 {report_id} 没有文本文件")
    path = container.settings.resolved_path(report.text_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告文件已丢失")
    return HTMLResponse(f"<pre>{path.read_text(encoding='utf-8')}</pre>")
