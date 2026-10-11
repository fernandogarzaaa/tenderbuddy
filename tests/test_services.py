"""Integration tests for ingest, sync, listing, bidding workflow and migrations (real SQLite DB)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from tenderbuddy import models
from tenderbuddy.ai import MockLLM
from tenderbuddy.bidding import UploadRejected, add_document, create_manual_tender, generate_draft, generate_summary, \
    safe_filename
from tenderbuddy.config import Settings
from tenderbuddy.connectors import TenderRecord
from tenderbuddy.db import Base, init_engine
from tenderbuddy.demo import demo_records, seed_demo
from tenderbuddy.migrate import alembic_config, current_revision, head_revision, is_legacy_beta, is_up_to_date, upgrade
from tenderbuddy.models import utcnow
from tenderbuddy.schemas import TenderIn
from tenderbuddy.services import (
    IngestStats,
    default_checklist,
    ensure_sources,
    get_profile,
    list_tenders,
    sync_all,
    sync_source,
    upsert_records,
)
from tenderbuddy.users import UserExists, authenticate, bootstrap_admin, create_user
from tests.conftest import load_fixture, make_pdf, make_settings, mock_client


def rec(ext: str = "E1", **kw: Any) -> TenderRecord:
    base: dict[str, Any] = {"source": "test", "external_id": ext, "title": f"Tender {ext}",
                            "deadline": utcnow() + timedelta(days=10)}
    base.update(kw)
    return TenderRecord(**base)


# ---------------------------------------------------------------- ingest

def test_upsert_create_update_unchanged(db: Session) -> None:
    stats = upsert_records(db, [rec("A", bid_security=5000, currency="EUR", certs_required=["ISO 9001"]),
                                rec("B"), rec("A")])
    assert (stats.created, stats.updated, stats.unchanged, stats.skipped) == (2, 0, 0, 1)
    a = db.scalar(select(models.Tender).where(models.Tender.external_id == "A"))
    assert a is not None
    assert [r.text for r in a.requirements] == ["Bid security / EMD of EUR 5,000 (bank guarantee or deposit)",
                                                "ISO 9001 certificate copy"]
    assert all(r.kind == "document" and r.auto for r in a.requirements)
    a.status = "Preparing"
    a.ai_summary = "kept"
    db.commit()
    stats2 = upsert_records(db, [rec("A", title="Renamed", bid_security=5000, currency="EUR",
                                     certs_required=["ISO 9001"]), rec("B")])
    assert (stats2.created, stats2.updated, stats2.unchanged) == (0, 1, 1)
    db.refresh(a)
    assert (a.title, a.status, a.ai_summary) == ("Renamed", "Preparing", "kept")
    assert len(a.requirements) == 2


def test_same_external_id_different_sources(db: Session) -> None:
    upsert_records(db, [rec("X", source="s1"), rec("X", source="s2")])
    assert db.scalar(select(models.Tender).where(models.Tender.external_id == "X").limit(1)) is not None
    assert len(list(db.scalars(select(models.Tender)))) == 2


def test_default_checklist() -> None:
    assert default_checklist(rec()) == []
    assert default_checklist(rec(bid_security=1, certs_required=["A"])) == [
        "Bid security / EMD of 1 (bank guarantee or deposit)", "A certificate copy"]


def test_ingest_stats_failed_flag() -> None:
    assert not IngestStats().failed
    assert IngestStats(errors=["x"]).failed
    assert not IngestStats(errors=["x"], unchanged=1).failed
    assert IngestStats(created=1, errors=["e"] * 60).as_dict()["errors"] == ["e"] * 50


# ---------------------------------------------------------------- sync

def _ted_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, CONNECTORS="ted,sam_gov,csv", TED_API_URL="https://ted.test/search")


def test_sync_source_success_and_idempotent(tmp_path: Path, db: Session) -> None:
    s = _ted_settings(tmp_path)
    data = load_fixture("ted_search.json")
    client = mock_client(lambda r: httpx.Response(200, json=data))
    stats = sync_source(db, s, "ted", client)
    assert stats.created == len(data["notices"])
    src = db.scalar(select(models.Source).where(models.Source.name == "ted"))
    assert src is not None
    assert (src.last_status, src.last_error, src.last_created) == ("ok", "", stats.created)
    assert src.last_sync_at is not None
    again = sync_source(db, s, "ted", client)
    assert (again.created, again.unchanged) == (0, len(data["notices"]))


def test_sync_source_uses_lookback_since_last_sync(tmp_path: Path, db: Session) -> None:
    s = make_settings(tmp_path, CONNECTORS="ted", TED_API_URL="https://ted.test", SYNC_LOOKBACK_DAYS=3)
    queries: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        queries.append(json.loads(r.content)["query"])
        return httpx.Response(200, json={"notices": [], "totalNoticeCount": 0})

    db.add(models.Source(name="ted", last_sync_at=utcnow().replace(year=2026, month=9, day=20)))
    db.commit()
    sync_source(db, s, "ted", mock_client(handler))
    assert "publication-date>=20260917" in queries[0]


def test_sync_source_records_errors(tmp_path: Path, db: Session) -> None:
    s = _ted_settings(tmp_path)
    stats = sync_source(db, s, "ted", mock_client(lambda r: httpx.Response(500)))
    assert stats.failed
    src = db.scalar(select(models.Source).where(models.Source.name == "ted"))
    assert src is not None
    assert src.last_status == "error"
    assert "HTTP 500" in src.last_error
    cfg = sync_source(db, s, "sam_gov")
    assert cfg.errors == ["sam_gov: SAM_API_KEY is not set"]


def test_sync_partial_status_on_row_errors(tmp_path: Path, db: Session) -> None:
    s = _ted_settings(tmp_path)
    staging = s.staging_dir / "philgeps"
    staging.mkdir(parents=True)
    (staging / "x.csv").write_text("title,deadline\nGood,2030-01-01\nBad,\n", encoding="utf-8")
    stats = sync_source(db, s, "csv")
    assert (stats.created, stats.skipped) == (1, 1)
    src = db.scalar(select(models.Source).where(models.Source.name == "csv"))
    assert src is not None
    assert src.last_status == "partial"
    assert "row 3" in src.last_error


def test_sync_all_respects_enabled_and_config(tmp_path: Path, db: Session) -> None:
    s = make_settings(tmp_path, CONNECTORS="csv,sam_gov")
    ensure_sources(db, s)
    sam = db.scalar(select(models.Source).where(models.Source.name == "sam_gov"))
    assert sam is not None
    sam.enabled = False
    db.add(models.Source(name="ted", enabled=True))  # present in DB but not configured
    db.commit()
    out = sync_all(db, s)
    assert list(out) == ["csv"]


def test_ensure_sources_idempotent(tmp_path: Path, db: Session) -> None:
    s = make_settings(tmp_path, CONNECTORS="ted,csv")
    assert [x.name for x in ensure_sources(db, s)] == ["ted", "csv"]
    assert [x.name for x in ensure_sources(db, s)] == ["ted", "csv"]


# ---------------------------------------------------------------- listing

def test_list_tenders_filters_sort_paginate(db: Session) -> None:
    now = utcnow()
    p = get_profile(db)
    p.categories = ["IT"]
    db.commit()
    upsert_records(db, [
        rec("1", title="Laptop supply", category="IT", estimated_value=10, deadline=now + timedelta(days=5),
            published_at=now - timedelta(days=1)),
        rec("2", title="Road works", category="Works", estimated_value=99, deadline=now + timedelta(days=2),
            published_at=now - timedelta(days=5), authority="Roads Dept"),
        rec("3", title="Old one", category="IT", deadline=now - timedelta(days=1), source="other"),
    ])
    t2 = db.scalar(select(models.Tender).where(models.Tender.external_id == "2"))
    assert t2 is not None
    t2.status = "Won"
    db.commit()
    assert [t.external_id for t, _ in list_tenders(db).rows] == ["3", "2", "1"]
    assert [t.external_id for t, _ in list_tenders(db, open_only=True).rows] == ["1"]
    assert [t.external_id for t, _ in list_tenders(db, q="ROADS").rows] == ["2"]
    assert [t.external_id for t, _ in list_tenders(db, status="Won").rows] == ["2"]
    assert [t.external_id for t, _ in list_tenders(db, source="other").rows] == ["3"]
    assert [t.external_id for t, _ in list_tenders(db, min_score=100).rows] == ["3", "1"]
    assert [t.external_id for t, _ in list_tenders(db, sort="score").rows] == ["3", "1", "2"]
    assert [t.external_id for t, _ in list_tenders(db, sort="value").rows] == ["2", "1", "3"]
    assert [t.external_id for t, _ in list_tenders(db, sort="published").rows][:2] == ["3", "1"]
    page = list_tenders(db, per_page=2, page=2)
    assert (page.total, page.pages, page.page, len(page.rows)) == (3, 2, 2, 1)
    assert list_tenders(db, per_page=0, page=0).per_page == 1
    assert list_tenders(db, per_page=999).per_page == 200


# ---------------------------------------------------------------- bidding workflow

def test_manual_tender_and_document_analysis(settings: Settings, db: Session) -> None:
    t = create_manual_tender(db, TenderIn(title="Library refurbishment", deadline=utcnow() + timedelta(days=20),
                                          currency="eur", certs_required="ISO 9001, Safe Contractor"))
    assert t.source == "manual"
    assert t.external_id.startswith("manual-")
    assert t.currency == "EUR"
    assert len(t.requirements) == 2
    with_ref = create_manual_tender(db, TenderIn(title="Second", deadline=utcnow(), ref_no="REF-9"))
    assert (with_ref.external_id, with_ref.ref_no) == ("REF-9", "REF-9")

    pdf = make_pdf(["Published on 01/10/2026. Last date for submission: 05/10/2026.",
                    "1. The contractor shall hold public liability insurance.",
                    "Liquidated damages apply."])
    out = add_document(db, settings, MockLLM(), t, "../../etc/RFP final.pdf", pdf, "RFP")
    assert out.text_found
    assert out.generator == "rules"
    assert out.requirements_created >= 1
    assert [r["type"] for r in out.risks] == ["Penalty clause", "Short timeline"]
    db.refresh(t)
    assert out.document.filename == "RFP_final.pdf"
    assert Path(out.document.path).parent == settings.upload_dir / f"tender_{t.id}"
    assert out.document.size_bytes == len(pdf)
    notes = [n.text for n in t.notes]
    assert any(n.startswith("RISK [Penalty clause]") for n in notes)
    assert any(n.startswith("Dates found in RFP_final.pdf: published 01/10/2026") for n in notes)
    again = add_document(db, settings, MockLLM(), t, "copy.pdf", pdf, "BOQ")
    assert again.requirements_created == 0  # duplicates are not re-added


def test_document_rejections(settings: Settings, db: Session) -> None:
    t = create_manual_tender(db, TenderIn(title="Upload target", deadline=utcnow()))
    with pytest.raises(UploadRejected, match="kind"):
        add_document(db, settings, MockLLM(), t, "a.pdf", b"%PDF-1", "Invoice")
    with pytest.raises(UploadRejected, match="empty"):
        add_document(db, settings, MockLLM(), t, "a.pdf", b"", "RFP")
    with pytest.raises(UploadRejected, match="only PDF"):
        add_document(db, settings, MockLLM(), t, "a.pdf", b"MZ\x90\x00", "RFP")
    small = make_settings(settings.DATA_DIR.parent, MAX_UPLOAD_MB=1)
    with pytest.raises(UploadRejected, match="larger than 1 MB"):
        add_document(db, small, MockLLM(), t, "a.pdf", b"%PDF-" + b"0" * (1024 * 1024), "RFP")


def test_scanned_pdf_adds_manual_review(settings: Settings, db: Session) -> None:
    t = create_manual_tender(db, TenderIn(title="Scanned", deadline=utcnow()))
    out = add_document(db, settings, MockLLM(), t, "scan.pdf", make_pdf([]), "RFP")
    assert not out.text_found
    assert out.requirements_created == 1
    db.refresh(t)
    assert t.requirements[-1].text == "Manually review scan.pdf: no machine-readable text (scanned PDF?)."


def test_safe_filename() -> None:
    assert safe_filename(None) == "document.pdf"
    assert safe_filename("../x/../../evil name.PDF") == "evil_name.PDF"
    assert safe_filename("report") == "report.pdf"
    assert safe_filename("...") == "document.pdf"
    assert len(safe_filename("a" * 300 + ".pdf")) == 120


def test_summary_and_draft(settings: Settings, db: Session) -> None:
    t = create_manual_tender(db, TenderIn(title="Cloud hosting", deadline=utcnow() + timedelta(days=3),
                                          description="We need hosting. It must be in the EU. Budget is fixed."))
    assert generate_summary(db, settings, MockLLM(), t) == "rules"
    assert t.ai_summary == "We need hosting. It must be in the EU. Budget is fixed."
    p = get_profile(db)
    p.name = "Hostco"
    db.add(models.Requirement(tender_id=t.id, text="EU data residency", kind="requirement"))
    db.commit()
    db.refresh(t)
    d = generate_draft(db, settings, MockLLM(), t)
    assert d.generator == "template"
    assert "# Bid response: Cloud hosting" in d.content
    assert "| 1 | EU data residency | To confirm | |" in d.content
    assert "Hostco" in d.content


# ---------------------------------------------------------------- users & demo

def test_users(settings: Settings, db: Session) -> None:
    admin = bootstrap_admin(db, settings)
    assert admin is not None
    assert (admin.email, admin.role) == ("admin@example.com", "admin")
    assert bootstrap_admin(db, settings) is None
    u = create_user(db, " Member@Example.com ", "member-password-1", name="M")
    assert (u.email, u.role) == ("member@example.com", "member")
    with pytest.raises(UserExists):
        create_user(db, "member@example.com", "member-password-1")
    with pytest.raises(ValueError, match="role"):
        create_user(db, "x@example.com", "member-password-1", role="root")
    assert authenticate(db, "MEMBER@example.com", "member-password-1") is not None
    assert authenticate(db, "member@example.com", "wrong-password") is None
    assert authenticate(db, "ghost@example.com", "whatever-pass") is None
    u.is_active = False
    db.commit()
    assert authenticate(db, "member@example.com", "member-password-1") is None


def test_demo_seed_idempotent(db: Session) -> None:
    recs = demo_records()
    assert len(recs) == 12
    assert all(r.currency == "INR" and r.deadline > utcnow() for r in recs)
    first = seed_demo(db)
    assert first.created == 12
    assert get_profile(db).name.startswith("Vertex Systems")
    second = seed_demo(db)
    assert second.created == 0
    t = db.scalar(select(models.Tender).where(models.Tender.external_id == "GEM/2026/B/6123451"))
    assert t is not None
    assert len(t.notes) == 1


# ---------------------------------------------------------------- migrations

def test_migrated_schema_matches_models(settings: Settings) -> None:
    engine = init_engine(settings.DATABASE_URL)
    assert upgrade(engine) == head_revision() == "0002_v1"
    assert is_up_to_date(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
    assert upgrade(engine) == "0002_v1"  # no-op when current
    engine.dispose()


def test_legacy_beta_database_is_converted(tmp_path: Path) -> None:
    from alembic import command

    url = f"sqlite:///{tmp_path / 'beta.db'}"
    engine = create_engine(url)
    cfg = alembic_config(url)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001_beta")
        conn.execute(text("DROP TABLE alembic_version"))  # beta DBs were created with create_all
        conn.execute(text(
            "INSERT INTO company_profiles (id, name, turnover_cr, years_exp, certifications_json, categories_json, "
            "states_json) VALUES (1, 'Vertex', 25.0, 8, '[\"ISO 9001\"]', '[\"IT Services\"]', 'not json')"))
        conn.execute(text(
            "INSERT INTO tenders (id, title, ref_no, authority, category, state, est_value_lakh, emd_lakh, "
            "published_date, deadline, turnover_req_cr, exp_req_years, certs_req_json, description, source, status, "
            "created_at) VALUES (1, 'Desktops', 'GEM/1', 'MeitY', 'IT Services', 'Karnataka', 120.0, 2.4, "
            "'2026-09-01 00:00:00', '2026-12-01 00:00:00', 8.0, 3, '[\"ISO 9001:2015\"]', 'd', 'CPP Portal', "
            "'Reviewing', '2026-09-01 00:00:00')"))
        conn.execute(text(
            "INSERT INTO tenders (id, title, ref_no, authority, category, state, est_value_lakh, emd_lakh, deadline, "
            "certs_req_json, description, source, status, created_at) VALUES (2, 'No EMD', 'GEM/2', '', '', '', "
            "0, 0, '2026-12-01 00:00:00', '[]', '', 'GeM', 'New', '2026-09-01 00:00:00')"))
        conn.execute(text("INSERT INTO requirements (tender_id, text, kind, is_met, auto, created_at) "
                          "VALUES (1, 'EMD proof', 'document', 1, 1, '2026-09-01 00:00:00')"))
        conn.execute(text("INSERT INTO source_records (name) VALUES ('GeM')"))
    assert is_legacy_beta(engine)
    assert upgrade(engine) == "0002_v1"
    assert not is_legacy_beta(engine)
    with engine.connect() as conn:
        p = conn.execute(text("SELECT * FROM company_profiles")).mappings().one()
        assert (p["currency"], p["annual_turnover"], p["years_experience"]) == ("INR", 250_000_000.0, 8)
        assert json.loads(p["certifications"]) == ["ISO 9001"]
        assert json.loads(p["regions"]) == []
        t1, t2 = conn.execute(text("SELECT * FROM tenders ORDER BY id")).mappings().all()
        assert (t1["external_id"], t1["source"], t1["region"], t1["currency"], t1["country"]) == (
            "GEM/1", "cpp_portal", "Karnataka", "INR", "IND")
        assert (t1["estimated_value"], t1["bid_security"], t1["min_turnover"], t1["min_experience_years"]) == (
            12_000_000.0, 240_000.0, 80_000_000.0, 3)
        assert json.loads(t1["certs_required"]) == ["ISO 9001:2015"]
        assert t1["status"] == "Reviewing"
        assert (t2["estimated_value"], t2["bid_security"], t2["min_turnover"]) == (None, None, None)
        assert conn.execute(text("SELECT count(*) FROM requirements")).scalar() == 1
        tables = set(inspect(conn).get_table_names())
        assert "source_records" not in tables
        assert {"sources", "bid_drafts"} <= tables
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
    assert current_revision(engine) == "0002_v1"
    engine.dispose()


def test_downgrade_refused(tmp_path: Path) -> None:
    from alembic import command

    url = f"sqlite:///{tmp_path / 'd.db'}"
    engine = init_engine(url)
    upgrade(engine)
    cfg = alembic_config(url)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="not supported"):
            command.downgrade(cfg, "0001_beta")
    engine.dispose()
