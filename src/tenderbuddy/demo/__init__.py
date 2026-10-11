"""Demo data (SEED_DEMO_DATA=true or ``tenderbuddy seed-demo``). Never enabled in production."""

from __future__ import annotations

import json
from datetime import timedelta
from importlib import resources

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderbuddy import models
from tenderbuddy.connectors import TenderRecord
from tenderbuddy.models import utcnow
from tenderbuddy.services import IngestStats, get_profile, upsert_records

DEMO_SOURCE = "demo"


def demo_records() -> list[TenderRecord]:
    raw = json.loads(resources.files(__package__).joinpath("tenders.json").read_text(encoding="utf-8"))
    now = utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    return [
        TenderRecord(
            source=DEMO_SOURCE,
            external_id=r["ref_no"],
            ref_no=r["ref_no"],
            title=r["title"],
            authority=r["authority"],
            category=r["category"],
            country=r["country"],
            region=r["region"],
            currency=r["currency"],
            estimated_value=float(r["estimated_value"]),
            bid_security=float(r["bid_security"]) if r["bid_security"] else None,
            min_turnover=float(r["min_turnover"]),
            min_experience_years=r["min_experience_years"],
            certs_required=list(r["certs_required"]),
            description=f"[{r['portal']}] {r['description']}",
            published_at=now - timedelta(days=r["published_days_ago"]),
            deadline=now + timedelta(days=r["deadline_in_days"]),
        )
        for r in raw
    ]


def seed_demo(db: Session) -> IngestStats:
    profile = get_profile(db)
    if not profile.name:
        profile.name = "Vertex Systems Pvt. Ltd. (demo)"
        profile.currency = "INR"
        profile.annual_turnover = 250_000_000.0
        profile.years_experience = 8
        profile.certifications = ["ISO 9001:2015", "MSME Registered", "GeM Registered", "NSIC Registered"]
        profile.categories = ["IT Services", "Supply", "Works"]
        profile.keywords = ["software", "network", "cctv"]
        profile.regions = ["Karnataka", "Maharashtra", "Delhi"]
        profile.capability_statement = (
            "Vertex Systems is an IT systems integrator delivering hardware supply, networking and software "
            "projects for central and state government departments."
        )
        db.commit()
    stats = upsert_records(db, demo_records())
    first = db.scalar(select(models.Tender).where(models.Tender.source == DEMO_SOURCE,
                                                  models.Tender.external_id == "GEM/2026/B/6123451"))
    if first is not None and not first.notes:
        db.add(models.BidNote(tender_id=first.id, text=(
            "Good fit: we already supply to two central ministries. Check OEM authorization letter expiry "
            "before pricing.")))
        db.commit()
    return stats
