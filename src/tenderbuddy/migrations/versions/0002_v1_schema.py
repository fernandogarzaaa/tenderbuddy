"""v1.0 schema: multi-currency amounts, live-source identity, AI fields, bid drafts.

Converts beta data in place: lakh/crore amounts become absolute INR values,
``state`` becomes ``region``, JSON-in-text lists become JSON columns, and the
beta ``source_records`` table is replaced by ``sources`` (one row per connector).

Requires SQLite >= 3.35 (DROP COLUMN) or PostgreSQL >= 12.

Revision ID: 0002_v1
Revises: 0001_beta
Create Date: 2026-10-11
"""

from __future__ import annotations

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "0002_v1"
down_revision = "0001_beta"
branch_labels = None
depends_on = None

LAKH = 100_000
CRORE = 10_000_000


def _json_list(raw: object) -> list[str]:
    try:
        val = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(v) for v in val] if isinstance(val, list) else []


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_") or "manual"


def _json_col(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.JSON, nullable=False, server_default=sa.text("'[]'"))


def _upgrade_profiles(conn: sa.Connection) -> None:
    op.add_column("company_profiles", sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"))
    op.add_column("company_profiles", sa.Column("annual_turnover", sa.Float, nullable=False, server_default="0"))
    for col in ("certifications", "categories", "cpv_prefixes", "keywords", "regions"):
        op.add_column("company_profiles", _json_col(col))
    op.add_column("company_profiles", sa.Column("capability_statement", sa.Text, nullable=False, server_default=""))
    op.add_column("company_profiles", sa.Column("updated_at", sa.DateTime, nullable=False,
                                                server_default=sa.func.current_timestamp()))
    op.execute("ALTER TABLE company_profiles RENAME COLUMN years_exp TO years_experience")
    profiles = sa.table(
        "company_profiles", sa.column("id", sa.Integer), sa.column("currency", sa.String),
        sa.column("annual_turnover", sa.Float), sa.column("certifications", sa.JSON),
        sa.column("categories", sa.JSON), sa.column("regions", sa.JSON),
    )
    rows = conn.execute(sa.text(
        "SELECT id, turnover_cr, certifications_json, categories_json, states_json FROM company_profiles"
    )).mappings().all()
    for row in rows:
        conn.execute(profiles.update().where(profiles.c.id == row["id"]).values(
            currency="INR",
            annual_turnover=float(row["turnover_cr"] or 0) * CRORE,
            certifications=_json_list(row["certifications_json"]),
            categories=_json_list(row["categories_json"]),
            regions=_json_list(row["states_json"]),
        ))
    for col in ("turnover_cr", "certifications_json", "categories_json", "states_json"):
        op.execute(f"ALTER TABLE company_profiles DROP COLUMN {col}")


def _upgrade_tenders(conn: sa.Connection) -> None:
    op.drop_index("ix_tenders_ref_no", table_name="tenders")
    op.execute("ALTER TABLE tenders RENAME COLUMN state TO region")
    op.execute("ALTER TABLE tenders RENAME COLUMN published_date TO published_at")
    for col in (
        sa.Column("external_id", sa.String(255), nullable=False, server_default=""),
        _json_col("cpv_codes"),
        sa.Column("country", sa.String(64), nullable=False, server_default=""),
        sa.Column("currency", sa.String(3), nullable=False, server_default=""),
        sa.Column("estimated_value", sa.Float, nullable=True),
        sa.Column("bid_security", sa.Float, nullable=True),
        sa.Column("min_turnover", sa.Float, nullable=True),
        sa.Column("min_experience_years", sa.Integer, nullable=True),
        _json_col("certs_required"),
        sa.Column("url", sa.String(1000), nullable=False, server_default=""),
        sa.Column("ai_summary", sa.Text, nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
    ):
        op.add_column("tenders", col)
    tenders = sa.table(
        "tenders", sa.column("id", sa.Integer), sa.column("external_id", sa.String), sa.column("source", sa.String),
        sa.column("country", sa.String), sa.column("currency", sa.String), sa.column("estimated_value", sa.Float),
        sa.column("bid_security", sa.Float), sa.column("min_turnover", sa.Float),
        sa.column("min_experience_years", sa.Integer), sa.column("certs_required", sa.JSON),
    )
    rows = conn.execute(sa.text(
        "SELECT id, ref_no, source, est_value_lakh, emd_lakh, turnover_req_cr, exp_req_years, certs_req_json "
        "FROM tenders"
    )).mappings().all()
    for row in rows:
        conn.execute(tenders.update().where(tenders.c.id == row["id"]).values(
            external_id=row["ref_no"],
            source=_slug(row["source"]),
            country="IND",
            currency="INR",
            estimated_value=float(row["est_value_lakh"] or 0) * LAKH or None,
            bid_security=float(row["emd_lakh"] or 0) * LAKH or None,
            min_turnover=float(row["turnover_req_cr"]) * CRORE if row["turnover_req_cr"] else None,
            min_experience_years=row["exp_req_years"],
            certs_required=_json_list(row["certs_req_json"]),
        ))
    if conn.dialect.name != "sqlite":  # SQLite does not enforce VARCHAR lengths
        for col, length in (("title", 1000), ("ref_no", 255), ("authority", 500), ("category", 255),
                            ("region", 255)):
            op.alter_column("tenders", col, type_=sa.String(length), existing_nullable=False)
        op.alter_column("tender_documents", "path", type_=sa.String(1024), existing_nullable=False)
    for col in ("est_value_lakh", "emd_lakh", "turnover_req_cr", "exp_req_years", "certs_req_json"):
        op.execute(f"ALTER TABLE tenders DROP COLUMN {col}")
    op.create_index("ix_tenders_ref_no", "tenders", ["ref_no"])
    op.create_index("ix_tenders_source", "tenders", ["source"])
    op.create_index("ix_tenders_deadline", "tenders", ["deadline"])
    op.create_index("ix_tenders_status", "tenders", ["status"])
    op.create_index("uq_tender_source_ext", "tenders", ["source", "external_id"], unique=True)


def upgrade() -> None:
    conn = op.get_bind()
    op.add_column("users", sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()))
    _upgrade_profiles(conn)
    _upgrade_tenders(conn)

    op.add_column("tender_documents", sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"))
    for table in ("tender_documents", "requirements", "bid_notes"):
        op.create_index(f"ix_{table}_tender_id", table, ["tender_id"])
    op.create_table(
        "bid_drafts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tender_id", sa.Integer, sa.ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("generator", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_bid_drafts_tender_id", "bid_drafts", ["tender_id"])

    op.drop_table("source_records")
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_sync_at", sa.DateTime, nullable=True),
        sa.Column("last_status", sa.String(32), nullable=False, server_default="never"),
        sa.Column("last_error", sa.Text, nullable=False, server_default=""),
        sa.Column("last_created", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_updated", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_skipped", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    raise RuntimeError("Downgrading from the v1 schema is not supported; restore a database backup instead.")
