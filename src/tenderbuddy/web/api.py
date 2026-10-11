"""JSON REST API (v1). Authenticate with ``POST /api/v1/auth/token`` then send
``Authorization: Bearer <token>``. Interactive docs at ``/docs``."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderbuddy import models
from tenderbuddy.bidding import UploadRejected, add_document, create_manual_tender, generate_draft, generate_summary
from tenderbuddy.config import KNOWN_CONNECTORS
from tenderbuddy.connectors import LABELS
from tenderbuddy.connectors.csv_import import MAX_CSV_BYTES, CsvFormatError, parse_csv_bytes
from tenderbuddy.rfp import analyze_rfp
from tenderbuddy.schemas import (
    AnalyzeIn,
    NoteIn,
    ProfileIn,
    ProfileOut,
    RequirementIn,
    RequirementPatch,
    StatusIn,
    TenderIn,
)
from tenderbuddy.services import (
    SORTS,
    ensure_sources,
    get_profile,
    list_tenders,
    score_tender,
    sync_all,
    sync_source,
    upsert_records,
)
from tenderbuddy.web.deps import AdminApiUser, ApiUser, DbDep, LLMDep, SettingsDep

router = APIRouter(prefix="/api/v1")


def iso(value: Any) -> str | None:
    return value.isoformat() + "Z" if value else None


def tender_or_404(db: Session, tender_id: int) -> models.Tender:
    t = db.get(models.Tender, tender_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Tender not found")
    return t


def tender_summary(t: models.Tender, score: int) -> dict[str, Any]:
    return {
        "id": t.id, "source": t.source, "external_id": t.external_id, "ref_no": t.ref_no, "title": t.title,
        "authority": t.authority, "category": t.category, "cpv_codes": t.cpv_codes, "country": t.country,
        "region": t.region, "currency": t.currency, "estimated_value": t.estimated_value,
        "deadline": iso(t.deadline), "published_at": iso(t.published_at), "status": t.status, "url": t.url,
        "score": score,
    }


def tender_detail(db: Session, t: models.Tender) -> dict[str, Any]:
    match = score_tender(get_profile(db), t)
    out = tender_summary(t, match.score)
    out.update({
        "description": t.description, "ai_summary": t.ai_summary, "bid_security": t.bid_security,
        "min_turnover": t.min_turnover, "min_experience_years": t.min_experience_years,
        "certs_required": t.certs_required, "match": match.as_dict(),
        "requirements": [{"id": r.id, "text": r.text, "kind": r.kind, "is_met": r.is_met, "auto": r.auto}
                         for r in t.requirements],
        "notes": [{"id": n.id, "text": n.text, "created_at": iso(n.created_at)} for n in t.notes],
        "documents": [{"id": d.id, "filename": d.filename, "kind": d.kind, "size_bytes": d.size_bytes}
                      for d in t.documents],
        "drafts": [{"id": d.id, "generator": d.generator, "created_at": iso(d.created_at)} for d in t.drafts],
    })
    return out


# ---------------------------------------------------------------- tenders

@router.get("/tenders", tags=["tenders"])
def api_list_tenders(
    db: DbDep, _user: ApiUser,
    q: Annotated[str, Query(max_length=200)] = "",
    status: Annotated[str, Query(max_length=32)] = "",
    source: Annotated[str, Query(max_length=64)] = "",
    open_only: bool = False,
    min_score: Annotated[int, Query(ge=0, le=100)] = 0,
    sort: Annotated[str, Query(pattern="^(" + "|".join(SORTS) + ")$")] = "deadline",
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    res = list_tenders(db, q=q, status=status, source=source, open_only=open_only, min_score=min_score,
                       sort=sort, page=page, per_page=per_page)
    return {"items": [tender_summary(t, s) for t, s in res.rows], "total": res.total, "page": res.page,
            "per_page": res.per_page, "pages": res.pages}


@router.post("/tenders", status_code=201, tags=["tenders"])
def api_create_tender(payload: TenderIn, db: DbDep, _user: ApiUser) -> dict[str, Any]:
    return tender_detail(db, create_manual_tender(db, payload))


@router.post("/tenders/import", tags=["tenders"])
async def api_import_csv(db: DbDep, _user: ApiUser, file: Annotated[UploadFile, File()],
                         source: Annotated[str, Form(max_length=64)] = "csv") -> dict[str, Any]:
    data = await file.read(MAX_CSV_BYTES + 1)
    try:
        parsed = parse_csv_bytes(data, source)
    except CsvFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stats = upsert_records(db, parsed.records)
    stats.skipped += parsed.skipped
    stats.errors.extend(parsed.errors)
    return stats.as_dict()


@router.get("/tenders/{tender_id}", tags=["tenders"])
def api_get_tender(tender_id: int, db: DbDep, _user: ApiUser) -> dict[str, Any]:
    return tender_detail(db, tender_or_404(db, tender_id))


@router.patch("/tenders/{tender_id}", tags=["tenders"])
def api_set_status(tender_id: int, payload: StatusIn, db: DbDep, _user: ApiUser) -> dict[str, Any]:
    t = tender_or_404(db, tender_id)
    t.status = payload.status
    db.commit()
    return tender_detail(db, t)


@router.delete("/tenders/{tender_id}", status_code=204, tags=["tenders"])
def api_delete_tender(tender_id: int, db: DbDep, _admin: AdminApiUser) -> None:
    db.delete(tender_or_404(db, tender_id))
    db.commit()


@router.post("/tenders/{tender_id}/requirements", status_code=201, tags=["bid workspace"])
def api_add_requirement(tender_id: int, payload: RequirementIn, db: DbDep, _user: ApiUser) -> dict[str, Any]:
    t = tender_or_404(db, tender_id)
    r = models.Requirement(tender_id=t.id, text=payload.text.strip(), kind=payload.kind)
    db.add(r)
    db.commit()
    return {"id": r.id, "text": r.text, "kind": r.kind, "is_met": r.is_met}


def requirement_or_404(db: Session, req_id: int) -> models.Requirement:
    r = db.get(models.Requirement, req_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return r


@router.patch("/requirements/{req_id}", tags=["bid workspace"])
def api_patch_requirement(req_id: int, payload: RequirementPatch, db: DbDep, _user: ApiUser) -> dict[str, Any]:
    r = requirement_or_404(db, req_id)
    r.is_met = payload.is_met
    db.commit()
    return {"id": r.id, "text": r.text, "kind": r.kind, "is_met": r.is_met}


@router.delete("/requirements/{req_id}", status_code=204, tags=["bid workspace"])
def api_delete_requirement(req_id: int, db: DbDep, _user: ApiUser) -> None:
    db.delete(requirement_or_404(db, req_id))
    db.commit()


@router.post("/tenders/{tender_id}/notes", status_code=201, tags=["bid workspace"])
def api_add_note(tender_id: int, payload: NoteIn, db: DbDep, _user: ApiUser) -> dict[str, Any]:
    t = tender_or_404(db, tender_id)
    n = models.BidNote(tender_id=t.id, text=payload.text.strip())
    db.add(n)
    db.commit()
    return {"id": n.id, "text": n.text}


@router.post("/tenders/{tender_id}/documents", status_code=201, tags=["bid workspace"])
async def api_upload_document(tender_id: int, db: DbDep, settings: SettingsDep, llm: LLMDep, _user: ApiUser,
                              file: Annotated[UploadFile, File()],
                              kind: Annotated[str, Form()] = "RFP") -> dict[str, Any]:
    t = tender_or_404(db, tender_id)
    data = await file.read(settings.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    try:
        out = add_document(db, settings, llm, t, file.filename, data, kind)
    except UploadRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"document_id": out.document.id, "requirements_created": out.requirements_created,
            "risks": out.risks, "generator": out.generator, "text_found": out.text_found}


@router.post("/tenders/{tender_id}/summary", tags=["ai"])
def api_summarize(tender_id: int, db: DbDep, settings: SettingsDep, llm: LLMDep, _user: ApiUser) -> dict[str, str]:
    t = tender_or_404(db, tender_id)
    generator = generate_summary(db, settings, llm, t)
    return {"summary": t.ai_summary, "generator": generator}


@router.post("/tenders/{tender_id}/drafts", status_code=201, tags=["ai"])
def api_draft(tender_id: int, db: DbDep, settings: SettingsDep, llm: LLMDep, _user: ApiUser) -> dict[str, Any]:
    d = generate_draft(db, settings, llm, tender_or_404(db, tender_id))
    return {"id": d.id, "generator": d.generator, "content": d.content}


def draft_or_404(db: Session, draft_id: int) -> models.BidDraft:
    d = db.get(models.BidDraft, draft_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return d


@router.get("/drafts/{draft_id}", tags=["ai"])
def api_get_draft(draft_id: int, db: DbDep, _user: ApiUser) -> dict[str, Any]:
    d = draft_or_404(db, draft_id)
    return {"id": d.id, "tender_id": d.tender_id, "generator": d.generator, "content": d.content}


@router.get("/drafts/{draft_id}/markdown", response_class=PlainTextResponse, tags=["ai"])
def api_draft_markdown(draft_id: int, db: DbDep, _user: ApiUser) -> str:
    return draft_or_404(db, draft_id).content


@router.post("/rfp/analyze", tags=["ai"])
def api_analyze(payload: AnalyzeIn, _user: ApiUser) -> dict[str, Any]:
    return dict(analyze_rfp(payload.text))


# ---------------------------------------------------------------- profile

@router.get("/profile", response_model=ProfileOut, tags=["profile"])
def api_get_profile(db: DbDep, _user: ApiUser) -> models.CompanyProfile:
    return get_profile(db)


@router.put("/profile", response_model=ProfileOut, tags=["profile"])
def api_put_profile(payload: ProfileIn, db: DbDep, _user: ApiUser) -> models.CompanyProfile:
    p = get_profile(db)
    for k, v in payload.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return p


# ---------------------------------------------------------------- sources

def source_dict(s: models.Source) -> dict[str, Any]:
    return {"name": s.name, "label": LABELS.get(s.name, s.name), "enabled": s.enabled,
            "last_sync_at": iso(s.last_sync_at), "last_status": s.last_status, "last_error": s.last_error,
            "last_created": s.last_created, "last_updated": s.last_updated, "last_skipped": s.last_skipped}


@router.get("/sources", tags=["sources"])
def api_sources(db: DbDep, settings: SettingsDep, _user: ApiUser) -> list[dict[str, Any]]:
    return [source_dict(s) for s in ensure_sources(db, settings)]


@router.post("/sources/sync", tags=["sources"])
def api_sync_all(db: DbDep, settings: SettingsDep, _admin: AdminApiUser) -> dict[str, Any]:
    return {name: st.as_dict() for name, st in sync_all(db, settings).items()}


@router.post("/sources/{name}/sync", tags=["sources"])
def api_sync_one(name: str, db: DbDep, settings: SettingsDep, _admin: AdminApiUser) -> dict[str, Any]:
    if name not in KNOWN_CONNECTORS:
        raise HTTPException(status_code=404, detail="Unknown source")
    if name not in settings.CONNECTORS:
        raise HTTPException(status_code=409, detail="Source is not enabled (see CONNECTORS)")
    return sync_source(db, settings, name).as_dict()


@router.patch("/sources/{name}", tags=["sources"])
def api_toggle_source(name: str, enabled: bool, db: DbDep, settings: SettingsDep,
                      _admin: AdminApiUser) -> dict[str, Any]:
    ensure_sources(db, settings)
    s = db.scalar(select(models.Source).where(models.Source.name == name))
    if s is None:
        raise HTTPException(status_code=404, detail="Unknown source")
    s.enabled = enabled
    db.commit()
    return source_dict(s)
