from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..schemas import CollectResultOut, ContainerDep
from ..security import require_api_key

router = APIRouter(prefix="/collect", tags=["collect"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=list[CollectResultOut])
async def collect_all(
    container: ContainerDep, limit: int = Query(default=10, ge=1, le=50)
) -> list[CollectResultOut]:
    """立即检查所有启用的订阅（采集 + 摘要 + 通知），并重排下一次运行。"""
    subs = await container.subs.list_active()
    results: list[CollectResultOut] = []
    for sub in subs[:limit]:
        if sub.id is None:
            continue
        outcome = await container.collect_service.collect(sub.id)
        results.append(
            CollectResultOut(
                subscription_id=sub.id,
                inserted=outcome.inserted,
                skipped=outcome.skipped,
                not_modified=outcome.not_modified,
                bootstrap=outcome.bootstrap,
                resources=outcome.resources,
            )
        )
    return results
