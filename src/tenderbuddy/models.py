"""ORM models. Schema changes go through Alembic migrations (tenderbuddy/migrations)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tenderbuddy.db import Base

STATUS_PIPELINE = ("New", "Reviewing", "Preparing", "Submitted", "Won", "Lost", "Dismissed")
OPEN_STATUSES = ("New", "Reviewing", "Preparing")


def utcnow() -> datetime:
    """Naive UTC timestamp (stored naive for SQLite/Postgres parity)."""
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CompanyProfile(Base):
    """Singleton row (id=1): the bidding organisation's profile used by the matcher."""

    __tablename__ = "company_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    annual_turnover: Mapped[float] = mapped_column(Float, default=0.0)
    years_experience: Mapped[int] = mapped_column(Integer, default=0)
    certifications: Mapped[list[str]] = mapped_column(JSON, default=list)
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    cpv_prefixes: Mapped[list[str]] = mapped_column(JSON, default=list)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    regions: Mapped[list[str]] = mapped_column(JSON, default=list)
    capability_statement: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Tender(Base):
    __tablename__ = "tenders"
    __table_args__ = (Index("uq_tender_source_ext", "source", "external_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), default="manual", index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    ref_no: Mapped[str] = mapped_column(String(255), default="", index=True)
    title: Mapped[str] = mapped_column(String(1000))
    authority: Mapped[str] = mapped_column(String(500), default="")
    category: Mapped[str] = mapped_column(String(255), default="")
    cpv_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    country: Mapped[str] = mapped_column(String(64), default="")
    region: Mapped[str] = mapped_column(String(255), default="")
    currency: Mapped[str] = mapped_column(String(3), default="")
    estimated_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_security: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_turnover: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_experience_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    certs_required: Mapped[list[str]] = mapped_column(JSON, default=list)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline: Mapped[datetime] = mapped_column(DateTime, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[str] = mapped_column(String(32), default="New", index=True)
    ai_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    requirements: Mapped[list[Requirement]] = relationship(
        back_populates="tender", cascade="all, delete-orphan", order_by="Requirement.id"
    )
    notes: Mapped[list[BidNote]] = relationship(
        back_populates="tender", cascade="all, delete-orphan", order_by="BidNote.id.desc()"
    )
    documents: Mapped[list[TenderDocument]] = relationship(
        back_populates="tender", cascade="all, delete-orphan", order_by="TenderDocument.id.desc()"
    )
    drafts: Mapped[list[BidDraft]] = relationship(
        back_populates="tender", cascade="all, delete-orphan", order_by="BidDraft.id.desc()"
    )

    def match_input(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "min_turnover": self.min_turnover,
            "min_experience_years": self.min_experience_years,
            "certs_required": list(self.certs_required or []),
            "category": self.category,
            "cpv_codes": list(self.cpv_codes or []),
            "country": self.country,
            "region": self.region,
            "title": self.title,
            "description": self.description,
        }


class TenderDocument(Base):
    __tablename__ = "tender_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[str] = mapped_column(String(32), default="RFP")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tender: Mapped[Tender] = relationship(back_populates="documents")


class Requirement(Base):
    """Bid-workspace checklist item. kind is 'requirement' or 'document'."""

    __tablename__ = "requirements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32), default="requirement")
    is_met: Mapped[bool] = mapped_column(Boolean, default=False)
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tender: Mapped[Tender] = relationship(back_populates="requirements")


class BidNote(Base):
    __tablename__ = "bid_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tender: Mapped[Tender] = relationship(back_populates="notes")


class BidDraft(Base):
    __tablename__ = "bid_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    generator: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tender: Mapped[Tender] = relationship(back_populates="drafts")


class Source(Base):
    """A configured tender source (one per connector) with its last sync outcome."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(32), default="never")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_created: Mapped[int] = mapped_column(Integer, default=0)
    last_updated: Mapped[int] = mapped_column(Integer, default=0)
    last_skipped: Mapped[int] = mapped_column(Integer, default=0)
