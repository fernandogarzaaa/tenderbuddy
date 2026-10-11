"""Login/logout (HTML form + JSON token endpoint)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response

from tenderbuddy.config import Settings
from tenderbuddy.schemas import LoginIn, TokenOut
from tenderbuddy.security import RateLimiter, create_access_token
from tenderbuddy.users import authenticate
from tenderbuddy.web.deps import AUTH_COOKIE, ApiUser, Csrf, DbDep, SettingsDep
from tenderbuddy.web.templating import render

log = logging.getLogger(__name__)
router = APIRouter()


def _limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.login_limiter
    return limiter


def limiter_key(request: Request, email: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}|{email.strip().lower()}"


def issue_token(settings: Settings, email: str) -> str:
    return create_access_token(email, settings.SECRET_KEY, settings.ACCESS_TOKEN_EXPIRE_MINUTES)


@router.get("/login", include_in_schema=False)
def login_page(request: Request) -> Response:
    return render(request, "auth/login.html", error=request.query_params.get("error", ""))


@router.post("/login", include_in_schema=False)
def login_form(request: Request, db: DbDep, settings: SettingsDep, _csrf: Csrf,
               email: Annotated[str, Form(max_length=255)],
               password: Annotated[str, Form(max_length=200)]) -> Response:
    limiter = _limiter(request)
    key = limiter_key(request, email)
    if limiter.is_blocked(key):
        log.warning("login rate limited", extra={"email": email})
        return RedirectResponse("/login?error=locked", status_code=303)
    user = authenticate(db, email, password)
    if user is None:
        limiter.hit(key)
        log.info("login failed", extra={"email": email})
        return RedirectResponse("/login?error=invalid", status_code=303)
    limiter.reset(key)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(AUTH_COOKIE, issue_token(settings, user.email), httponly=True, samesite="lax",
                    secure=settings.cookie_secure, max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    log.info("login ok", extra={"email": user.email})
    return resp


@router.post("/logout", include_in_schema=False)
def logout(_csrf: Csrf) -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(AUTH_COOKIE)
    return resp


@router.post("/api/v1/auth/token", response_model=TokenOut, tags=["auth"])
def api_token(request: Request, payload: LoginIn, db: DbDep, settings: SettingsDep) -> TokenOut:
    limiter = _limiter(request)
    key = limiter_key(request, payload.email)
    if limiter.is_blocked(key):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many failed logins")
    user = authenticate(db, payload.email, payload.password)
    if user is None:
        limiter.hit(key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    limiter.reset(key)
    return TokenOut(access_token=issue_token(settings, user.email),
                    expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)


@router.get("/api/v1/me", tags=["auth"])
def me(user: ApiUser) -> dict[str, str]:
    return {"email": user.email, "name": user.name, "role": user.role}
