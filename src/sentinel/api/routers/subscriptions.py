from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ...config import Settings
from ...domain.enums import DEFAULT_WATCH, DigestStyle, EventKind
from ...domain.errors import ValidationError
from ...domain.models import Subscription
from ..schemas import (
    CollectResultOut,
    ContainerDep,
    DigestResultOut,
    EventOut,
    PageOut,
    ReportOut,
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionUpdate,
)
from ..security import require_api_key
from ._common import to_page

router = APIRouter(
    prefix="/subscriptions", tags=["subscriptions"], dependencies=[Depends(require_api_key)]
)


def _domain_subscription(payload: SubscriptionCreate, settings: Settings) -> Subscription:
    try:
        watch = (
            frozenset(EventKind(kind) for kind in payload.watch)
            if payload.watch
            else DEFAULT_WATCH
        )
        return Subscription(
            repo_full_name=payload.repo_full_name,
            title=payload.title,
            cron=payload.cron or settings.default_cron,
            timezone=payload.timezone or settings.timezone,
            watch=watch,
            label_include=tuple(payload.label_include),
            label_exclude=tuple(payload.label_exclude),
            author_exclude=tuple(payload.author_exclude),
            keyword_include=tuple(payload.keyword_include),
            keyword_exclude=tuple(payload.keyword_exclude),
            skip_bots=payload.skip_bots,
            digest_style=DigestStyle(payload.digest_style or DigestStyle.BRIEF),
            language=payload.language,
            notify_enabled=payload.notify_enabled,
            notify_target=payload.notify_target,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


@router.get("", response_model=PageOut[SubscriptionOut])
async def list_subscriptions(
    container: ContainerDep,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PageOut[SubscriptionOut]:
    items = await container.subs.list(search=q, limit=limit, offset=offset)
    total = await container.subs.count()
    return to_page(items, total, limit, offset, SubscriptionOut)


@router.post("", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
async def create_subscription(
    payload: SubscriptionCreate, container: ContainerDep
) -> SubscriptionOut:
    sub = _domain_subscription(payload, container.settings)
    created = await container.subscription_service.create(sub)
    return SubscriptionOut.model_validate(created)


@router.get("/{sub_id}", response_model=SubscriptionOut)
async def get_subscription(sub_id: int, container: ContainerDep) -> SubscriptionOut:
    sub = await container.subscription_service.get(sub_id)
    return SubscriptionOut.model_validate(sub)


@router.patch("/{sub_id}", response_model=SubscriptionOut)
async def update_subscription(
    sub_id: int, payload: SubscriptionUpdate, container: ContainerDep
) -> SubscriptionOut:
    changes = payload.model_dump(exclude_unset=True)
    if "watch" in changes and changes["watch"] is not None:
        try:
            changes["watch"] = frozenset(EventKind(kind) for kind in changes["watch"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if changes.get("digest_style"):
        try:
            changes["digest_style"] = DigestStyle(changes["digest_style"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        updated = await container.subscription_service.update(sub_id, changes)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SubscriptionOut.model_validate(updated)


@router.delete("/{sub_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscription(sub_id: int, container: ContainerDep) -> None:
    await container.subscription_service.delete(sub_id)


@router.post("/{sub_id}/enable", response_model=SubscriptionOut)
async def enable_subscription(sub_id: int, container: ContainerDep) -> SubscriptionOut:
    sub = await container.subscription_service.set_enabled(sub_id, True)
    return SubscriptionOut.model_validate(sub)


@router.post("/{sub_id}/disable", response_model=SubscriptionOut)
async def disable_subscription(sub_id: int, container: ContainerDep) -> SubscriptionOut:
    sub = await container.subscription_service.set_enabled(sub_id, False)
    return SubscriptionOut.model_validate(sub)


@router.post("/{sub_id}/collect", response_model=CollectResultOut)
async def collect_subscription(sub_id: int, container: ContainerDep) -> CollectResultOut:
    outcome = await container.collect_service.collect(sub_id)
    return CollectResultOut(
        subscription_id=outcome.subscription.id or sub_id,
        inserted=outcome.inserted,
        skipped=outcome.skipped,
        not_modified=outcome.not_modified,
        bootstrap=outcome.bootstrap,
        resources=outcome.resources,
    )


@router.post("/{sub_id}/digest", response_model=DigestResultOut)
async def digest_subscription(
    sub_id: int, container: ContainerDep, force: bool = False
) -> DigestResultOut:
    outcome = await container.digest_service.build(sub_id, force=force)
    return DigestResultOut(
        report_id=outcome.report.id if outcome.report else None,
        reused=outcome.reused,
        reason=outcome.reason,
    )


@router.get("/{sub_id}/events", response_model=PageOut[EventOut])
async def list_subscription_events(
    sub_id: int,
    container: ContainerDep,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> PageOut[EventOut]:
    await container.subscription_service.get(sub_id)
    now = container.clock.now()
    items = await container.events.list_window(
        sub_id, now - timedelta(days=days), now, limit=limit
    )
    total = await container.events.count_for(sub_id)
    return to_page(items, total, limit, offset, EventOut)


@router.get("/{sub_id}/reports", response_model=PageOut[ReportOut])
async def list_subscription_reports(
    sub_id: int,
    container: ContainerDep,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PageOut[ReportOut]:
    await container.subscription_service.get(sub_id)
    items = await container.reports.list(subscription_id=sub_id, limit=limit, offset=offset)
    total = await container.reports.count(subscription_id=sub_id)
    return to_page(items, total, limit, offset, ReportOut)
