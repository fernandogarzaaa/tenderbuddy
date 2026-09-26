from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def naive_utcnow() -> datetime:
    """Naive UTC now, used across product models for SQLite/Postgres parity."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), default="", nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(32), default="member", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


# --- Product models: Tenderbuddy ---


class CompanyProfile(Base):
    """Singleton row (id=1): the bidder's company profile used by the matcher."""

    __tablename__ = "company_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), default="", nullable=False)
    turnover_cr = Column(Float, default=0.0, nullable=False)
    years_exp = Column(Integer, default=0, nullable=False)
    certifications_json = Column(Text, default="[]", nullable=False)
    categories_json = Column(Text, default="[]", nullable=False)
    states_json = Column(Text, default="[]", nullable=False)


class Tender(Base):
    __tablename__ = "tenders"

    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    ref_no = Column(String(128), unique=True, index=True, nullable=False)
    authority = Column(String(255), default="", nullable=False)
    category = Column(String(128), default="", nullable=False)
    state = Column(String(128), default="", nullable=False)
    est_value_lakh = Column(Float, default=0.0, nullable=False)
    emd_lakh = Column(Float, default=0.0, nullable=False)
    published_date = Column(DateTime, nullable=True)
    deadline = Column(DateTime, nullable=False)
    turnover_req_cr = Column(Float, nullable=True)
    exp_req_years = Column(Integer, nullable=True)
    certs_req_json = Column(Text, default="[]", nullable=False)
    description = Column(Text, default="", nullable=False)
    source = Column(String(64), default="", nullable=False)
    status = Column(String(32), default="New", nullable=False)
    created_at = Column(DateTime, default=naive_utcnow, nullable=False)


class TenderDocument(Base):
    __tablename__ = "tender_documents"

    id = Column(Integer, primary_key=True)
    tender_id = Column(Integer, ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=False)
    path = Column(String(512), nullable=False)
    kind = Column(String(32), default="RFP", nullable=False)
    created_at = Column(DateTime, default=naive_utcnow, nullable=False)


class Requirement(Base):
    """Bid-workspace checklist item. kind is 'requirement' or 'document'."""

    __tablename__ = "requirements"

    id = Column(Integer, primary_key=True)
    tender_id = Column(Integer, ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False)
    text = Column(Text, nullable=False)
    kind = Column(String(32), default="requirement", nullable=False)
    is_met = Column(Boolean, default=False, nullable=False)
    auto = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=naive_utcnow, nullable=False)


class BidNote(Base):
    __tablename__ = "bid_notes"

    id = Column(Integer, primary_key=True)
    tender_id = Column(Integer, ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=naive_utcnow, nullable=False)


class SourceRecord(Base):
    """Configured tender source (GeM / CPP / eProcure) with last sync stamp."""

    __tablename__ = "source_records"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False)
    last_sync_at = Column(DateTime, nullable=True)
