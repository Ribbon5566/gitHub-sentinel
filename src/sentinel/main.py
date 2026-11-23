"""ASGI 入口。

启动：uvicorn sentinel.main:app --port 8000 --loop asyncio
（Windows 上必须显式 --loop asyncio；默认的 proactor 事件循环会让部分异步驱动行为异常）
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routers import activity, collect, health, pages, reports, subscriptions
from .config import Settings, load_settings
from .container import Container
from .domain.errors import (
    ConflictError,
    NotFoundError,
    SentinelError,
    ValidationError,
)
from .logging_setup import configure_logging

logger = logging.getLogger(__name__)

DESCRIPTION = """
GitHub Sentinel 的 HTTP 接口。

- 订阅管理：`/api/v1/subscriptions`
- 采集与摘要：`POST /api/v1/subscriptions/{id}/collect`、`POST /api/v1/subscriptions/{id}/digest`
- 报告：`/api/v1/reports`
- 运行记录：`/api/v1/runs`

鉴权：请求头 `X-API-Key`。未配置 `SENTINEL_API_KEY` 时仅允许本机访问。
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(
        settings.log_level,
        secrets=(settings.api_key, settings.github_token, settings.llm_api_key, settings.smtp_password),
    )
    container = Container(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await container.startup()
        logger.info(
            "app.started env=%s github=%s llm=%s notify=%s scheduler=%s",
            settings.env,
            settings.github_mode,
            settings.llm_provider,
            settings.notify_channel,
            settings.enable_scheduler,
        )
        try:
            yield
        finally:
            await container.shutdown()
            logger.info("app.stopped")

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.container = container
    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

    # 领域异常 → HTTP 状态码，集中一处，路由里不写 try/except 样板
    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValidationError)
    async def _invalid(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SentinelError)
    async def _sentinel(_: Request, exc: SentinelError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.include_router(health.router)
    app.include_router(subscriptions.router, prefix="/api/v1")
    app.include_router(collect.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(activity.router, prefix="/api/v1")
    app.include_router(pages.router)
    return app


app = create_app()
