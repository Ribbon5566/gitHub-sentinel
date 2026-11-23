from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..schemas import ContainerDep, EventOut, PageOut, RunOut
from ..security import require_api_key
from ._common import to_page

router = APIRouter(tags=["activity"], dependencies=[Depends(require_api_key)])


@router.get("/runs", response_model=PageOut[RunOut])
async def list_runs(
    container: ContainerDep,
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PageOut[RunOut]:
    items = await container.runs.list(limit=limit)
    total = await container.runs.count()
    return to_page(items, total, limit, offset, RunOut)


@router.get("/events", response_model=PageOut[EventOut])
async def latest_events(
    container: ContainerDep,
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PageOut[EventOut]:
    items = await container.events.latest(limit=limit + offset)
    window = items[offset:]
    return to_page(window, len(window), limit, offset, EventOut)
