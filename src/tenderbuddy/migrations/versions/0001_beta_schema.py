"""Beta schema (as created by Tenderbuddy 0.x via create_all).

Existing beta databases have these tables without an alembic_version row;
``tenderbuddy migrate`` detects that and stamps this revision before upgrading.

Revision ID: 0001_beta
Revises:
Create Date: 2026-10-11
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0001_beta"
down_revision = None
branch_labels = None
depends_on = None


def _children() -> dict[str, list[sa.Column[Any]]]:
    return {
        "tender_documents": [
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("path", sa.String(512), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
        ],
        "requirements": [
            sa.Column("text", sa.Text, nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("is_met", sa.Boolean, nullable=False),
            sa.Column("auto", sa.Boolean, nullable=False),
        ],
        "bid_notes": [sa.Column("text", sa.Text, nullable=False)],
    }


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "company_profiles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("turnover_cr", sa.Float, nullable=False),
        sa.Column("years_exp", sa.Integer, nullable=False),
        sa.Column("certifications_json", sa.Text, nullable=False),
        sa.Column("categories_json", sa.Text, nullable=False),
        sa.Column("states_json", sa.Text, nullable=False),
    )
    op.create_table(
        "tenders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("ref_no", sa.String(128), nullable=False),
        sa.Column("authority", sa.String(255), nullable=False),
        sa.Column("category", sa.String(128), nullable=False),
        sa.Column("state", sa.String(128), nullable=False),
        sa.Column("est_value_lakh", sa.Float, nullable=False),
        sa.Column("emd_lakh", sa.Float, nullable=False),
        sa.Column("published_date", sa.DateTime, nullable=True),
        sa.Column("deadline", sa.DateTime, nullable=False),
        sa.Column("turnover_req_cr", sa.Float, nullable=True),
        sa.Column("exp_req_years", sa.Integer, nullable=True),
        sa.Column("certs_req_json", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_tenders_ref_no", "tenders", ["ref_no"], unique=True)
    for name, cols in _children().items():
        op.create_table(
            name,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("tender_id", sa.Integer, sa.ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False),
            *cols,
            sa.Column("created_at", sa.DateTime, nullable=False),
        )
    op.create_table(
        "source_records",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("last_sync_at", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    for name in ("source_records", "bid_notes", "requirements", "tender_documents", "tenders",
                 "company_profiles", "users"):
        op.drop_table(name)
