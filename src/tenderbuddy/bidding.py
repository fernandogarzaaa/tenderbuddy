"""Bid-workspace operations shared by the HTML UI and the JSON API."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderbuddy import models
from tenderbuddy.ai import LLMClient, draft_bid, extract_requirements, summarize_tender
from tenderbuddy.config import Settings
from tenderbuddy.connectors import TenderRecord
from tenderbuddy.models import utcnow
from tenderbuddy.rfp import analyze_rfp, extract_pdf_text
from tenderbuddy.schemas import TenderIn
from tenderbuddy.services import get_profile, profile_match_input, score_tender, upsert_records

log = logging.getLogger(__name__)

ALLOWED_DOC_KINDS = ("RFP", "BOQ", "Corrigendum", "Other")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class UploadRejected(ValueError):
    pass


@dataclass
class DocumentOutcome:
    document: models.TenderDocument
    requirements_created: int
    risks: list[dict[str, str]]
    generator: str
    text_found: bool


def safe_filename(name: str | None) -> str:
    base = Path(name or "").name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "document"
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned[-120:]


def create_manual_tender(db: Session, data: TenderIn) -> models.Tender:
    fields = data.model_dump(exclude={"ref_no"})
    rec = TenderRecord(source="manual", external_id=data.ref_no or f"manual-{uuid.uuid4().hex[:12]}",
                       ref_no=data.ref_no, **fields)
    upsert_records(db, [rec])
    tender = db.scalar(select(models.Tender).where(models.Tender.source == "manual",
                                                   models.Tender.external_id == rec.external_id))
    assert tender is not None
    return tender


def add_document(db: Session, settings: Settings, llm: LLMClient, tender: models.Tender,
                 filename: str | None, data: bytes, kind: str = "RFP") -> DocumentOutcome:
    if kind not in ALLOWED_DOC_KINDS:
        raise UploadRejected("kind must be one of " + ", ".join(ALLOWED_DOC_KINDS))
    if not data:
        raise UploadRejected("the uploaded file is empty")
    if len(data) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise UploadRejected(f"file is larger than {settings.MAX_UPLOAD_MB} MB")
    if not data.startswith(b"%PDF-"):
        raise UploadRejected("only PDF files are accepted")
    name = safe_filename(filename)
    dest_dir = settings.upload_dir / f"tender_{tender.id}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex[:8]}_{name}"
    dest.write_bytes(data)
    doc = models.TenderDocument(tender_id=tender.id, filename=name, path=str(dest), kind=kind, size_bytes=len(data))
    db.add(doc)

    text = extract_pdf_text(dest)
    analysis = analyze_rfp(text)
    reqs = extract_requirements(llm, text, analysis["requirements"], max_chars=settings.LLM_MAX_INPUT_CHARS)
    existing = {r.text.lower() for r in tender.requirements}
    created = 0
    for req_text in reqs.value:
        if req_text.lower() in existing:
            continue
        db.add(models.Requirement(tender_id=tender.id, text=req_text, kind="requirement", auto=True))
        existing.add(req_text.lower())
        created += 1
    for risk in analysis["risks"]:
        db.add(models.BidNote(tender_id=tender.id, text=f"RISK [{risk['type']}]: {risk['detail']}"))
    if analysis["dates"]:
        found = "; ".join(f"{d['label']} {d['date']}" for d in analysis["dates"][:10])
        db.add(models.BidNote(tender_id=tender.id, text=f"Dates found in {name}: {found}"))
    if not text.strip():
        db.add(models.Requirement(tender_id=tender.id, kind="requirement", auto=True,
                                  text=f"Manually review {name}: no machine-readable text (scanned PDF?)."))
        created += 1
    db.commit()
    log.info("document analysed", extra={"tender_id": tender.id, "requirements": created,
                                          "risks": len(analysis["risks"]), "generator": reqs.generator})
    risks = [{"type": r["type"], "detail": r["detail"]} for r in analysis["risks"]]
    return DocumentOutcome(doc, created, risks, reqs.generator, bool(text.strip()))


def generate_summary(db: Session, settings: Settings, llm: LLMClient, tender: models.Tender) -> str:
    result = summarize_tender(llm, tender.title, tender.description, max_chars=settings.LLM_MAX_INPUT_CHARS)
    tender.ai_summary = str(result.value)
    db.commit()
    return result.generator


def tender_context(tender: models.Tender) -> dict[str, Any]:
    return {"title": tender.title, "ref_no": tender.ref_no, "authority": tender.authority,
            "deadline": tender.deadline, "description": tender.description, "summary": tender.ai_summary}


def generate_draft(db: Session, settings: Settings, llm: LLMClient, tender: models.Tender) -> models.BidDraft:
    profile = get_profile(db)
    match = score_tender(profile, tender)
    pdict = profile_match_input(profile) | {"name": profile.name, "capability_statement": profile.capability_statement}
    reqs = [{"text": r.text, "is_met": r.is_met} for r in tender.requirements if r.kind == "requirement"]
    result = draft_bid(llm, pdict, tender_context(tender), match, reqs, utcnow(),
                       max_chars=settings.LLM_MAX_INPUT_CHARS)
    draft = models.BidDraft(tender_id=tender.id, content=str(result.value), generator=result.generator[:255])
    db.add(draft)
    db.commit()
    return draft
