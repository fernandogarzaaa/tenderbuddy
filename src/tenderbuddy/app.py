"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from tenderbuddy import __version__
from tenderbuddy.ai import get_llm
from tenderbuddy.config import Settings, get_settings
from tenderbuddy.db import get_engine, init_engine, ping, session_scope
from tenderbuddy.logging_setup import configure_logging, request_id_var
from tenderbuddy.security import RateLimiter, new_csrf_token
from tenderbuddy.web import api, auth, pages
from tenderbuddy.web.deps import CSRF_COOKIE, LoginRequired
from tenderbuddy.web.templating import PACKAGE_DIR, render

log = logging.getLogger(__name__)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
QUIET_PATHS = frozenset({"/healthz", "/readyz"})


def bootstrap(settings: Settings) -> None:
    from tenderbuddy.demo import seed_demo
    from tenderbuddy.services import ensure_sources
    from tenderbuddy.users import bootstrap_admin

    with session_scope() as db:
        bootstrap_admin(db, settings)
        ensure_sources(db, settings)
        if settings.SEED_DEMO_DATA:
            seed_demo(db)


def scheduled_sync(settings: Settings) -> None:
    from tenderbuddy.services import sync_all

    try:
        with session_scope() as db:
            sync_all(db, settings)
    except Exception:
        log.exception("scheduled sync failed")


def start_scheduler(settings: Settings) -> Any:
    if settings.SYNC_INTERVAL_MINUTES <= 0:
        return None
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(scheduled_sync, "interval", args=[settings], minutes=settings.SYNC_INTERVAL_MINUTES,
                  id="sync_all", max_instances=1, coalesce=True)
    sched.start()
    log.info("scheduler started", extra={"interval_minutes": settings.SYNC_INTERVAL_MINUTES})
    return sched


def create_app(settings: Settings | None = None, *, migrate: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    engine = init_engine(settings.DATABASE_URL)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if migrate:
            from tenderbuddy.migrate import upgrade

            upgrade(engine)
        bootstrap(settings)
        sched = start_scheduler(settings)
        log.info("tenderbuddy started", extra={"version": __version__, "env": settings.APP_ENV,
                                               "llm": settings.llm_mode, "connectors": settings.CONNECTORS})
        try:
            yield
        finally:
            if sched is not None:
                sched.shutdown(wait=False)

    app = FastAPI(title=settings.APP_NAME, version=__version__, lifespan=lifespan, docs_url="/docs", redoc_url=None)
    app.state.settings = settings
    app.state.llm = get_llm(settings)
    app.state.login_limiter = RateLimiter(settings.LOGIN_RATE_LIMIT, settings.LOGIN_RATE_WINDOW_SECONDS)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("X-Request-ID", "")[:64] or uuid.uuid4().hex
        token = request_id_var.set(rid)
        csrf = request.cookies.get(CSRF_COOKIE)
        request.state.csrf_token = csrf or new_csrf_token()
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"path": request.url.path})
            response = JSONResponse({"detail": "Internal server error", "request_id": rid}, status_code=500)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path not in ("/docs", "/openapi.json"):
            response.headers["Content-Security-Policy"] = CSP
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if not csrf:
            response.set_cookie(CSRF_COOKIE, request.state.csrf_token, httponly=True, samesite="lax",
                                secure=settings.cookie_secure)
        if request.url.path not in QUIET_PATHS:
            log.info("request", extra={"method": request.method, "path": request.url.path,
                                       "status": response.status_code,
                                       "ms": round((time.perf_counter() - start) * 1000, 1)})
        request_id_var.reset(token)
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        wants_html = "text/html" in request.headers.get("accept", "")
        if request.url.path.startswith("/api/") or not wants_html:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, detail=exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse({"detail": "Validation failed", "errors": errors}, status_code=422)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Response:
        from tenderbuddy.migrate import is_up_to_date

        db_ok = ping()
        migrated = db_ok and is_up_to_date(get_engine())
        body = {"status": "ok" if migrated else "unavailable", "database": db_ok, "migrations": migrated}
        return JSONResponse(body, status_code=200 if migrated else 503)

    @app.get("/api/health", include_in_schema=False)
    def legacy_health() -> dict[str, str]:
        return {"status": "ok", "app": settings.APP_NAME, "version": __version__}

    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.include_router(auth.router)
    app.include_router(api.router)
    app.include_router(pages.router)
    return app
