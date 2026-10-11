"""Application services: profile access, tender ingest (upsert), source sync and listing."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from tenderbuddy import models
from tenderbuddy.config import Settings
from tenderbuddy.connectors import ConnectorError, TenderRecord, build_connector
from tenderbuddy.matcher import MatchResult, fmt_amount, match_score
from tenderbuddy.models import utcnow

log = logging.getLogger(__name__)

# Fields copied from a fresh record onto an existing tender. User-owned state
# (status, checklist, notes, documents, AI summary) is never overwritten.
SYNCED_FIELDS = (
    "ref_no", "title", "authority", "category", "cpv_codes", "country", "region",
    "currency", "estimated_value", "bid_security", "min_turnover",
    "min_experience_years", "certs_required", "published_at", "deadline",
    "description", "url",
)
SORTS = ("deadline", "score", "published", "value")

_sync_lock = threading.Lock()


@dataclass
class IngestStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """True when the run produced errors and nothing usable."""
        return bool(self.errors) and not (self.created or self.updated or self.unchanged)

    def as_dict(self) -> dict[str, Any]:
        return {"created": self.created, "updated": self.updated, "unchanged": self.unchanged,
                "skipped": self.skipped, "errors": self.errors[:50]}


# ---------------------------------------------------------------- profile

def get_profile(db: Session) -> models.CompanyProfile:
    profile = db.get(models.CompanyProfile, 1)
    if profile is None:
        profile = models.CompanyProfile(id=1)
        db.add(profile)
        db.commit()
    return profile


def profile_match_input(p: models.CompanyProfile) -> dict[str, Any]:
    return {
        "currency": p.currency,
        "annual_turnover": p.annual_turnover,
        "years_experience": p.years_experience,
        "certifications": list(p.certifications or []),
        "categories": list(p.categories or []),
        "cpv_prefixes": list(p.cpv_prefixes or []),
        "keywords": list(p.keywords or []),
        "regions": list(p.regions or []),
    }


def score_tender(profile: models.CompanyProfile, tender: models.Tender) -> MatchResult:
    return match_score(profile_match_input(profile), tender.match_input())


# ---------------------------------------------------------------- ingest

def default_checklist(rec: TenderRecord) -> list[str]:
    items: list[str] = []
    if rec.bid_security:
        items.append(f"Bid security / EMD of {fmt_amount(rec.bid_security, rec.currency)} (bank guarantee or deposit)")
    items.extend(f"{c} certificate copy" for c in rec.certs_required)
    return items


def upsert_records(db: Session, records: list[TenderRecord]) -> IngestStats:
    """Insert new tenders, refresh changed ones (keyed on source + external_id)."""
    stats = IngestStats()
    seen: set[tuple[str, str]] = set()
    for rec in records:
        key = (rec.source, rec.external_id)
        if key in seen:
            stats.skipped += 1
            continue
        seen.add(key)
        existing = db.scalar(
            select(models.Tender).where(models.Tender.source == rec.source,
                                        models.Tender.external_id == rec.external_id)
        )
        if existing is None:
            tender = models.Tender(source=rec.source, external_id=rec.external_id, status="New",
                                   **{f: getattr(rec, f) for f in SYNCED_FIELDS})
            db.add(tender)
            db.flush()
            for text in default_checklist(rec):
                db.add(models.Requirement(tender_id=tender.id, text=text, kind="document", auto=True))
            stats.created += 1
            continue
        changed = False
        for f in SYNCED_FIELDS:
            new = getattr(rec, f)
            if getattr(existing, f) != new:
                setattr(existing, f, new)
                changed = True
        if changed:
            stats.updated += 1
        else:
            stats.unchanged += 1
    db.commit()
    return stats


def ensure_sources(db: Session, settings: Settings) -> list[models.Source]:
    for name in settings.CONNECTORS:
        if db.scalar(select(models.Source).where(models.Source.name == name)) is None:
            db.add(models.Source(name=name, enabled=True))
    db.commit()
    return list(db.scalars(select(models.Source).order_by(models.Source.id)))


def sync_source(db: Session, settings: Settings, name: str, client: httpx.Client | None = None) -> IngestStats:
    """Fetch one source and upsert its notices. Records the outcome on the Source row."""
    source = db.scalar(select(models.Source).where(models.Source.name == name))
    if source is None:
        source = models.Source(name=name, enabled=True)
        db.add(source)
        db.commit()
    since = (source.last_sync_at or utcnow()) - timedelta(days=settings.SYNC_LOOKBACK_DAYS)
    stats = IngestStats()
    connector = build_connector(name, settings, client)
    try:
        connector.check_config()
        fetched = connector.fetch(since=since, max_records=settings.SYNC_MAX_RECORDS)
        stats = upsert_records(db, fetched.records)
        stats.skipped += fetched.skipped
        stats.errors.extend(fetched.errors)
        source.last_status = "ok" if not fetched.errors else "partial"
        source.last_error = "; ".join(fetched.errors[:5])[:2000]
    except ConnectorError as exc:
        db.rollback()
        source.last_status = "error"
        source.last_error = str(exc)[:2000]
        stats.errors.append(str(exc))
        log.warning("source sync failed", extra={"source": name, "error": str(exc)})
    finally:
        connector.close()
    source.last_sync_at = utcnow()
    source.last_created = stats.created
    source.last_updated = stats.updated
    source.last_skipped = stats.skipped
    db.commit()
    log.info("source synced", extra={"source": name, "created": stats.created, "updated": stats.updated,
                                     "skipped": stats.skipped, "errors": len(stats.errors)})
    return stats


def sync_all(db: Session, settings: Settings, client: httpx.Client | None = None) -> dict[str, IngestStats]:
    """Sync every enabled configured source. Concurrent calls in one process are serialised."""
    with _sync_lock:
        out: dict[str, IngestStats] = {}
        for source in ensure_sources(db, settings):
            if source.enabled and source.name in settings.CONNECTORS:
                out[source.name] = sync_source(db, settings, source.name, client)
        return out


# ---------------------------------------------------------------- listing

@dataclass
class TenderPage:
    rows: list[tuple[models.Tender, int]]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))


def list_tenders(db: Session, *, q: str = "", status: str = "", source: str = "", open_only: bool = False,
                 min_score: int = 0, sort: str = "deadline", page: int = 1, per_page: int = 50) -> TenderPage:
    """Filter, score and paginate tenders. Scores are computed in Python (rule-based, cheap)."""
    stmt = select(models.Tender)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(
            func.lower(models.Tender.title).like(like),
            func.lower(models.Tender.ref_no).like(like),
            func.lower(models.Tender.authority).like(like),
            func.lower(models.Tender.description).like(like),
        ))
    if status:
        stmt = stmt.where(models.Tender.status == status)
    if source:
        stmt = stmt.where(models.Tender.source == source)
    if open_only:
        stmt = stmt.where(models.Tender.status.in_(models.OPEN_STATUSES), models.Tender.deadline >= utcnow())
    profile = get_profile(db)
    scored = [(t, score_tender(profile, t).score) for t in db.scalars(stmt)]
    if min_score:
        scored = [r for r in scored if r[1] >= min_score]
    if sort == "score":
        scored.sort(key=lambda r: (-r[1], r[0].deadline))
    elif sort == "published":
        scored.sort(key=lambda r: r[0].published_at or r[0].created_at, reverse=True)
    elif sort == "value":
        scored.sort(key=lambda r: r[0].estimated_value or 0.0, reverse=True)
    else:
        scored.sort(key=lambda r: r[0].deadline)
    page = max(1, page)
    per_page = max(1, min(200, per_page))
    start = (page - 1) * per_page
    return TenderPage(scored[start : start + per_page], len(scored), page, per_page)
