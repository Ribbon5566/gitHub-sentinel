from __future__ import annotations

from fastapi import APIRouter, Depends

from ...domain.enums import EventKind
from ..schemas import ContainerDep, HealthOut, MetaOut
from ..security import require_api_key

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthOut)
async def healthz(container: ContainerDep) -> HealthOut:
    """存活探针：不查库，进程还在就返回 200。"""
    return HealthOut(
        status="ok",
        version=container.settings.version,
        checks={"app": "ok"},
    )


@router.get("/readyz", response_model=HealthOut)
async def readyz(container: ContainerDep) -> HealthOut:
    """就绪探针：真的打一次数据库，确认能读写。"""
    checks = {"app": "ok"}
    try:
        await container.subs.count()
        checks["db"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["db"] = f"error: {exc}"
    status_text = "ok" if all(value == "ok" for value in checks.values()) else "degraded"
    return HealthOut(status=status_text, version=container.settings.version, checks=checks)


@router.get(
    "/api/v1/meta",
    response_model=MetaOut,
    dependencies=[Depends(require_api_key)],
    tags=["meta"],
)
async def meta(container: ContainerDep) -> MetaOut:
    settings = container.settings
    return MetaOut(
        name=settings.app_name,
        version=settings.version,
        github_mode=settings.github_mode,
        llm_provider=settings.llm_provider,
        notify_channel=settings.notify_channel,
        timezone=settings.timezone,
        scheduler_enabled=bool(container.scheduler and container.scheduler.running),
        event_kinds=[kind.value for kind in EventKind],
    )
