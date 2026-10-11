"""Request dependencies: settings, DB session, authentication and CSRF protection.

Authentication accepts either an ``Authorization: Bearer <jwt>`` header (API
clients) or the ``access_token`` HttpOnly cookie (browser UI). Unsafe requests
authenticated by cookie must also carry a CSRF token (form field ``csrf_token``
or header ``X-CSRF-Token``) matching the ``csrf_token`` cookie.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderbuddy import models
from tenderbuddy.ai import LLMClient
from tenderbuddy.config import Settings
from tenderbuddy.db import session_factory
from tenderbuddy.security import csrf_matches, decode_access_token

AUTH_COOKIE = "access_token"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class LoginRequired(Exception):
    """Raised for HTML pages; the app turns it into a redirect to /login."""


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_db() -> Iterator[Session]:
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()


def get_llm_dep(request: Request) -> LLMClient:
    llm: LLMClient = request.app.state.llm
    return llm


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbDep = Annotated[Session, Depends(get_db)]
LLMDep = Annotated[LLMClient, Depends(get_llm_dep)]


def token_from(request: Request) -> tuple[str | None, str]:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip(), "bearer"
    return request.cookies.get(AUTH_COOKIE), "cookie"


def optional_user(request: Request, db: DbDep, settings: SettingsDep) -> models.User | None:
    token, _ = token_from(request)
    if not token:
        return None
    email = decode_access_token(token, settings.SECRET_KEY)
    if not email:
        return None
    user = db.scalar(select(models.User).where(models.User.email == email))
    if user is None or not user.is_active:
        return None
    return user


async def verify_csrf(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    if token_from(request)[1] == "bearer":
        return  # bearer tokens are never attached automatically by browsers
    submitted = request.headers.get(CSRF_HEADER)
    if not submitted:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            value = (await request.form()).get("csrf_token")
            submitted = value if isinstance(value, str) else None
    if not csrf_matches(request.cookies.get(CSRF_COOKIE), submitted):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing or invalid")


OptionalUser = Annotated[models.User | None, Depends(optional_user)]
Csrf = Annotated[None, Depends(verify_csrf)]


def api_user(user: OptionalUser, _csrf: Csrf) -> models.User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


def page_user(user: OptionalUser, _csrf: Csrf) -> models.User:
    if user is None:
        raise LoginRequired()
    return user


def admin_api_user(user: Annotated[models.User, Depends(api_user)]) -> models.User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def admin_page_user(user: Annotated[models.User, Depends(page_user)]) -> models.User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


ApiUser = Annotated[models.User, Depends(api_user)]
AdminApiUser = Annotated[models.User, Depends(admin_api_user)]
PageUser = Annotated[models.User, Depends(page_user)]
AdminPageUser = Annotated[models.User, Depends(admin_page_user)]
