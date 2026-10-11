"""PostgreSQL integration (runs in CI against a postgres:16 service; skipped without TEST_POSTGRES_URL)."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import text

from tenderbuddy.app import create_app
from tenderbuddy.db import Base, init_engine
from tenderbuddy.migrate import upgrade
from tenderbuddy.models import utcnow
from tests.conftest import bearer, make_settings

PG_URL = os.getenv("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_POSTGRES_URL not set")


@pytest.fixture
def clean_pg() -> str:
    engine = init_engine(PG_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return PG_URL


def test_postgres_migrations_match_models(clean_pg: str) -> None:
    engine = init_engine(clean_pg)
    assert upgrade(engine) == "0002_v1"
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []
    engine.dispose()


def test_postgres_end_to_end(clean_pg: str, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, DATABASE_URL=clean_pg, SEED_DEMO_DATA=True)
    with TestClient(create_app(settings)) as client:
        h = bearer(client)
        assert client.get("/readyz").json()["database"] is True
        items = client.get("/api/v1/tenders?per_page=100", headers=h).json()
        assert items["total"] == 12
        deadline = (utcnow() + timedelta(days=5)).isoformat() + "Z"
        t = client.post("/api/v1/tenders", headers=h, json={"title": "PG tender", "deadline": deadline,
                                                            "certs_required": ["ISO 9001"]}).json()
        assert t["certs_required"] == ["ISO 9001"]
        d = client.post(f"/api/v1/tenders/{t['id']}/drafts", headers=h).json()
        assert d["content"].startswith("# Bid response: PG tender")
        assert client.delete(f"/api/v1/tenders/{t['id']}", headers=h).status_code == 204
