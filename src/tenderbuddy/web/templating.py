"""Jinja2 templates and filters."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from starlette.responses import Response

from tenderbuddy import __version__
from tenderbuddy.connectors import LABELS
from tenderbuddy.models import utcnow

PACKAGE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def money(value: float | None, currency: str = "") -> str:
    if value is None:
        return "—"
    text = f"{value / 1_000_000:,.2f}M" if abs(value) >= 1_000_000 else f"{value:,.0f}"
    return f"{currency} {text}".strip()


def dt(value: datetime | None, fmt: str = "%d %b %Y") -> str:
    return value.strftime(fmt) if value else "—"


def days_left(deadline: datetime) -> int:
    return (deadline - utcnow()).days


def nl2br(value: str) -> Markup:
    return Markup("<br>").join(escape(line) for line in (value or "").splitlines())


def source_label(name: str) -> str:
    return LABELS.get(name, name)


templates.env.filters.update(money=money, dt=dt, nl2br=nl2br)
templates.env.globals.update(days_left=days_left, version=__version__, source_label=source_label)


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> Response:
    settings = request.app.state.settings
    token = getattr(request.state, "csrf_token", "")
    base: dict[str, Any] = {
        "app_name": settings.APP_NAME,
        "csrf_token": token,
        "csrf_field": Markup('<input type="hidden" name="csrf_token" value="%s">') % token,
        "flash": request.query_params.get("msg", "")[:300],
        "flash_error": request.query_params.get("err", "")[:300],
        "user": None,
        "nav": "",
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base, status_code=status_code)
