import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.agent.provider import HttpProvider
from app.api.routes import admin, conversations, events, personal, plans
from app.api.schemas import ErrorResponse
from app.common.errors import AppError
from app.config import Settings, get_settings
from app.storage.postgres.engine import build_database

logger = logging.getLogger("bashculture.http")
REQUESTS = Counter(
    "bashculture_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
LATENCY = Histogram("bashculture_http_seconds", "HTTP latency", ["route"])


def error_response(request: Request, error: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={
            "error": {"code": error.code, "message": error.message, "details": error.details},
            "request_id": getattr(request.state, "request_id", str(uuid4())),
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()
    root = Path(__file__).resolve().parents[2]
    migration_config = Config(str(root / "alembic.ini"))
    migration_config.set_main_option("script_location", str(root / "migrations"))
    expected_revision = ScriptDirectory.from_config(migration_config).get_current_head()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine, sessions = build_database(config.database_url)
        app.state.engine, app.state.sessions, app.state.settings = engine, sessions, config
        try:
            async with AsyncExitStack() as resources:
                managed_provider = None
                if (
                    config.llm_api_url
                    and config.llm_model
                    and (not config.llm_require_api_key or config.llm_api_key.get_secret_value())
                    and not hasattr(app.state, "agent_provider")
                ):
                    client = await resources.enter_async_context(
                        httpx.AsyncClient(
                            timeout=config.llm_timeout_seconds,
                            limits=httpx.Limits(
                                max_connections=20,
                                max_keepalive_connections=10,
                                keepalive_expiry=60,
                            ),
                        )
                    )
                    managed_provider = HttpProvider(config, client)
                    app.state.agent_provider = managed_provider
                try:
                    yield
                finally:
                    if (
                        managed_provider is not None
                        and app.state.agent_provider is managed_provider
                    ):
                        del app.state.agent_provider
        finally:
            await engine.dispose()

    app = FastAPI(
        title="Афиша культуры Башкортостана",
        version="0.1.0",
        lifespan=lifespan,
        description="Демонстрационный проект. Каталог работает без модели. "
        "Цены, даты и источники берутся из импортированных данных.",
        responses={code: {"model": ErrorResponse} for code in (400, 401, 404, 409, 413, 422, 503)},
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid4())
        started = time.monotonic()
        try:
            response = await call_next(request)
        except OSError:
            response = error_response(
                request,
                AppError(
                    "database_unavailable",
                    "Каталог временно недоступен. Повторите запрос позже",
                    503,
                ),
            )
        except Exception as exc:
            logger.error(
                json.dumps(
                    {"request_id": request.state.request_id, "error_type": type(exc).__name__}
                )
            )
            response = error_response(
                request,
                AppError("internal_error", "Не удалось обработать запрос. Попробуйте позже", 500),
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        route = getattr(request.scope.get("route"), "path", "unmatched")
        duration = time.monotonic() - started
        REQUESTS.labels(request.method, route, response.status_code).inc()
        LATENCY.labels(route).observe(duration)
        logger.info(
            json.dumps(
                {
                    "request_id": request.state.request_id,
                    "method": request.method,
                    "route": route,
                    "status": response.status_code,
                    "duration_ms": round(duration * 1000, 2),
                }
            )
        )
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [{"field": ".".join(map(str, e["loc"])), "code": e["type"]} for e in exc.errors()]
        return error_response(
            request,
            AppError("validation_error", "Проверьте значения полей", 422, {"fields": fields}),
        )

    @app.exception_handler(HTTPException)
    async def handle_http(request: Request, exc: HTTPException) -> JSONResponse:
        return error_response(request, AppError("http_error", "Запрос недоступен", exc.status_code))

    @app.exception_handler(SQLAlchemyError)
    async def handle_database(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return error_response(
            request,
            AppError(
                "database_unavailable", "Каталог временно недоступен. Повторите запрос позже", 503
            ),
        )

    @app.get("/health", tags=["Состояние"])
    @app.get("/health/live", tags=["Состояние"])
    async def live() -> dict[str, str]:
        configured = bool(config.llm_api_url and config.llm_model)
        model_state = "configured_unchecked" if configured else "not_configured"
        if configured and config.llm_require_api_key and not config.llm_api_key.get_secret_value():
            model_state = "missing_credentials"
        return {"status": "ok", "model": model_state}

    @app.get("/health/ready", tags=["Состояние"])
    async def ready(request: Request) -> dict[str, str]:
        async with request.app.state.sessions() as session:
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != expected_revision:
                raise AppError("migration_required", "Требуется обновление схемы базы", 503)
            await session.execute(text("SELECT id FROM events LIMIT 1"))
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    app.include_router(events.router)
    app.include_router(admin.router)
    app.include_router(plans.router)
    app.include_router(personal.router)
    app.include_router(conversations.router)
    return app


app = create_app()
