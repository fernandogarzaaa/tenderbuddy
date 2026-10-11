"""Shared fixtures: isolated settings + SQLite DB per test, app client, auth helpers."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tenderbuddy.app import create_app
from tenderbuddy.config import Settings
from tenderbuddy.db import init_engine, session_factory
from tenderbuddy.migrate import upgrade

FIXTURES = Path(__file__).parent / "fixtures"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Admin-Password-123"
SECRET = "test-secret-key-that-is-long-enough-123456"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'test.db'}",
        "DATA_DIR": tmp_path / "data",
        "SECRET_KEY": SECRET,
        "ADMIN_EMAIL": ADMIN_EMAIL,
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "LOG_FORMAT": "text",
        "LOG_LEVEL": "WARNING",
        "CONNECTORS": ["csv"],
        "LLM_PROVIDER": "mock",
        "HTTP_MAX_RETRIES": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def db(settings: Settings) -> Iterator[Any]:
    engine = init_engine(settings.DATABASE_URL)
    upgrade(engine)
    session = session_factory()()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def app_factory(tmp_path: Path) -> Callable[..., TestClient]:
    clients: list[TestClient] = []

    def factory(**overrides: Any) -> TestClient:
        client = TestClient(create_app(make_settings(tmp_path, **overrides)))
        client.__enter__()
        clients.append(client)
        return client

    yield factory  # type: ignore[misc]
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(app_factory: Callable[..., TestClient]) -> TestClient:
    return app_factory()


def csrf(client: TestClient) -> str:
    if not client.cookies.get("csrf_token"):
        client.get("/login")
    token = client.cookies.get("csrf_token")
    assert token
    return token


def login(client: TestClient, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD) -> TestClient:
    r = client.post("/login", data={"email": email, "password": password, "csrf_token": csrf(client)},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/", r.headers["location"]
    return client


@pytest.fixture
def auth_client(client: TestClient) -> TestClient:
    return login(client)


def bearer(client: TestClient, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD) -> dict[str, str]:
    r = client.post("/api/v1/auth/token", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def api_headers(client: TestClient) -> dict[str, str]:
    return bearer(client)


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def make_pdf(lines: list[str]) -> bytes:
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in lines:
        c.drawString(50, y, line)
        y -= 18
    c.save()
    return buf.getvalue()
