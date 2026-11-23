"""研发周期报告接口：一次请求跑完「取数 -> 进展 md -> 正式报告」。

为什么不拆成两个端点：区间报告的两步之间有隐式的口径耦合（报告的口径
从进展文件的 front matter 读），拆开等于让调用方负责传递这个耦合，
而它只要传错一次就会拿到「合并清单 + 活动口径提示词」这种混合产物。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from ...domain.period import ReportPeriod
from ...domain.selection import ReportSelection
from ..schemas import ContainerDep, PeriodReportOut, PeriodReportRequest
from ..security import require_api_key

router = APIRouter(prefix="/daily", tags=["daily"], dependencies=[Depends(require_api_key)])

# 入参 scope -> 领域构造器。放在模块级，路由里就不必写 if/elif 长链
_SCOPES: dict[str, object] = {
    "completion": ReportSelection.completion,
    "activity": ReportSelection.activity,
    "merged": ReportSelection.merged_only,
    "closed": ReportSelection.closed_only,
}


@router.post("/period", response_model=PeriodReportOut)
async def create_period_report(
    payload: PeriodReportRequest, container: ContainerDep
) -> PeriodReportOut:
    """生成区间项目进展（以及可选的研发周期报告）。

    - `scope=completion`（默认）：区间内**已合并的 PR** + **已关闭的议题**
    - `scope=activity`：退回 v2.0 的 `updated_at` 口径
    - `scope=merged` / `scope=closed`：只保留其中一类对象

    全部参数校验都在请求 GitHub 之前完成：区间倒序、终点在未来、跨度超过
    `SENTINEL_REPORT_RANGE_MAX_DAYS` 都会直接 422，不会白烧一次配额。
    """
    factory = _SCOPES[payload.scope]
    selection = factory()  # type: ignore[operator]
    date_to = payload.date_to or datetime.now(container.settings.daily_tz).date()
    period = ReportPeriod(date_from=payload.date_from, date_to=date_to)

    progress = await container.daily_progress_service.build(
        repo_full_name=payload.repo_full_name,
        period=period,
        selection=selection,
        repo_title=payload.repo_title,
        force=payload.force,
    )

    out = PeriodReportOut(
        repo_full_name=progress.repo_full_name,
        period_label=progress.period_label or period.label(),
        scope=payload.scope,
        scope_label=selection.scope_label(),
        criteria=list(
            selection.criteria(
                period_label=progress.period_label or period.label(),
                timezone=container.settings.daily_timezone or container.settings.timezone,
            )
        ),
        progress_path=progress.path,
        progress_reused=progress.reused,
        item_count=progress.item_count,
        stats=dict(progress.stats),
        trimmed=list(progress.trimmed),
        truncated=list(progress.truncated),
    )

    if not payload.include_report:
        return out

    report = await container.daily_report_service.build(
        repo_full_name=payload.repo_full_name,
        period=period,
        selection=selection,
        force=payload.force,
    )
    out.report_path = report.path
    out.report_reused = report.reused
    out.report_model = report.model
    out.report_degraded = report.degraded
    return out
