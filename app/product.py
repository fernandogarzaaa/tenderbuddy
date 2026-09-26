"""Tenderbuddy product router: dashboard, tenders, bid workspace, sources, profile."""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timedelta

import pypdf
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import models
from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user, page_or_login
from app.matcher import match_score
from app.models import naive_utcnow
from app.rfp import analyze_rfp

router = APIRouter()
templates = Jinja2Templates(directory="templates")

STATUS_PIPELINE = ["New", "Reviewing", "Preparing", "Submitted", "Won", "Lost"]
OPEN_STATUSES = ["New", "Reviewing", "Preparing"]
MATCH_THRESHOLD = 60
UPLOAD_ROOT = "./data/uploads"
STAGING_ROOT = "./data/staging"

CSV_COLUMNS = [
    "title", "ref_no", "authority", "category", "state", "est_value_lakh",
    "emd_lakh", "deadline", "published_date", "turnover_req_cr",
    "exp_req_years", "certs_req", "description", "source",
]


def _ctx(request: Request, user, **kw) -> dict:
    base = {"request": request, "app_name": settings.APP_NAME, "user": user}
    base.update(kw)
    return base


def _json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _profile_dict(db: Session) -> dict:
    p = db.get(models.CompanyProfile, 1)
    if not p:
        p = models.CompanyProfile(id=1, name="", turnover_cr=0.0, years_exp=0)
        db.add(p)
        db.commit()
        db.refresh(p)
    return {
        "row": p,
        "turnover_cr": p.turnover_cr or 0.0,
        "years_exp": p.years_exp or 0,
        "certifications": _json_list(p.certifications_json),
        "categories": _json_list(p.categories_json),
        "states": _json_list(p.states_json),
    }


def _tender_match_dict(t: models.Tender) -> dict:
    return {
        "turnover_req_cr": t.turnover_req_cr,
        "exp_req_years": t.exp_req_years,
        "certs_req": _json_list(t.certs_req_json),
        "category": t.category or "",
        "state": t.state or "",
    }


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_float(raw) -> float:
    try:
        return float(str(raw).strip().replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


def import_tender_rows(db: Session, rows: list[dict], default_source: str = "") -> tuple[int, int]:
    """Insert tender rows from CSV dicts. Returns (imported, skipped)."""
    imported, skipped = 0, 0
    for r in rows:
        ref = (r.get("ref_no") or "").strip()
        title = (r.get("title") or "").strip()
        deadline = _parse_dt(r.get("deadline"))
        if not ref or not title or not deadline:
            skipped += 1
            continue
        if db.query(models.Tender).filter_by(ref_no=ref).first():
            skipped += 1
            continue
        certs = [c.strip() for c in (r.get("certs_req") or "").replace(";", "|").split("|") if c.strip()]
        exp_raw = (r.get("exp_req_years") or "").strip()
        t = models.Tender(
            title=title,
            ref_no=ref,
            authority=(r.get("authority") or "").strip(),
            category=(r.get("category") or "").strip(),
            state=(r.get("state") or "").strip(),
            est_value_lakh=_parse_float(r.get("est_value_lakh")),
            emd_lakh=_parse_float(r.get("emd_lakh")),
            published_date=_parse_dt(r.get("published_date")) or naive_utcnow(),
            deadline=deadline,
            turnover_req_cr=_parse_float(r.get("turnover_req_cr")) or None,
            exp_req_years=int(_parse_float(exp_raw)) if exp_raw else None,
            certs_req_json=json.dumps(certs),
            description=(r.get("description") or "").strip(),
            source=(r.get("source") or default_source or "").strip(),
            status="New",
        )
        db.add(t)
        db.flush()
        if t.emd_lakh and t.emd_lakh > 0:
            db.add(models.Requirement(
                tender_id=t.id,
                text=f"EMD of Rs {t.emd_lakh} lakh (demand draft / bank guarantee)",
                kind="document", auto=True,
            ))
        for c in certs:
            db.add(models.Requirement(
                tender_id=t.id, text=f"{c} copy", kind="document", auto=True,
            ))
        imported += 1
    db.commit()
    return imported, skipped


def _days_left(t: models.Tender) -> int:
    return (t.deadline - naive_utcnow()).days


def _stage_slug(name: str) -> str:
    return name.lower().replace(" ", "_")


# ---------------------------------------------------------------- dashboard

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = page_or_login(request, db)
    if not isinstance(user, models.User):
        return user
    now = naive_utcnow()
    profile = _profile_dict(db)
    tenders = db.query(models.Tender).order_by(models.Tender.deadline).all()

    open_t = [t for t in tenders if t.status in OPEN_STATUSES]
    scored = [(t, match_score(profile, _tender_match_dict(t))[0]) for t in open_t]
    matched = [t for t, s in scored if s >= MATCH_THRESHOLD]
    avg = round(sum(s for _, s in scored) / len(scored), 1) if scored else 0.0
    alerts = [t for t in open_t if t.deadline <= now + timedelta(days=7)]
    alerts.sort(key=lambda t: t.deadline)

    by_cat: dict[str, int] = {}
    by_month: dict[str, int] = {}
    for t in tenders:
        by_cat[t.category or "Other"] = by_cat.get(t.category or "Other", 0) + 1
        key = (t.published_date or t.created_at or now).strftime("%Y-%m")
        by_month[key] = by_month.get(key, 0) + 1
    months = sorted(by_month)

    return templates.TemplateResponse(
        request, "dashboard.html",
        _ctx(
            request, user,
            open_count=len(open_t),
            matched_count=len(matched),
            avg_score=avg,
            alerts=alerts,
            days_left=_days_left,
            cat_labels=list(by_cat.keys()),
            cat_values=list(by_cat.values()),
            month_labels=months,
            month_values=[by_month[m] for m in months],
            threshold=MATCH_THRESHOLD,
        ),
    )


# ---------------------------------------------------------------- profile

@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    user = page_or_login(request, db)
    if not isinstance(user, models.User):
        return user
    p = _profile_dict(db)["row"]
    return templates.TemplateResponse(
        request, "profile.html",
        _ctx(
            request, user,
            profile=p,
            certifications=", ".join(_json_list(p.certifications_json)),
            categories=", ".join(_json_list(p.categories_json)),
            states=", ".join(_json_list(p.states_json)),
            saved=request.query_params.get("saved"),
        ),
    )


@router.post("/api/profile")
def profile_save(
    request: Request,
    name: str = Form(""),
    turnover_cr: str = Form("0"),
    years_exp: str = Form("0"),
    certifications: str = Form(""),
    categories: str = Form(""),
    states: str = Form(""),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    p = _profile_dict(db)["row"]
    p.name = name.strip()
    p.turnover_cr = _parse_float(turnover_cr)
    try:
        p.years_exp = int(_parse_float(years_exp))
    except ValueError:
        p.years_exp = 0
    p.certifications_json = json.dumps([c.strip() for c in certifications.split(",") if c.strip()])
    p.categories_json = json.dumps([c.strip() for c in categories.split(",") if c.strip()])
    p.states_json = json.dumps([c.strip() for c in states.split(",") if c.strip()])
    db.commit()
    return RedirectResponse("/profile?saved=1", status_code=303)


# ---------------------------------------------------------------- tenders

@router.get("/tenders", response_class=HTMLResponse)
def tenders_page(request: Request, db: Session = Depends(get_db)):
    user = page_or_login(request, db)
    if not isinstance(user, models.User):
        return user
    profile = _profile_dict(db)
    q = (request.query_params.get("q") or "").strip().lower()
    tenders = db.query(models.Tender).order_by(models.Tender.deadline).all()
    if q:
        tenders = [t for t in tenders
                   if q in (t.title or "").lower() or q in (t.ref_no or "").lower()
                   or q in (t.authority or "").lower()]
    rows = [(t, match_score(profile, _tender_match_dict(t))[0]) for t in tenders]
    return templates.TemplateResponse(
        request, "tenders.html",
        _ctx(
            request, user, rows=rows, q=request.query_params.get("q", ""),
            days_left=_days_left, threshold=MATCH_THRESHOLD,
            imported=request.query_params.get("imported"),
            skipped=request.query_params.get("skipped"),
        ),
    )


@router.post("/api/tenders/import-csv")
def tenders_import_csv(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    raw = file.file.read().decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(raw)))
    imported, skipped = import_tender_rows(db, rows)
    return RedirectResponse(
        f"/tenders?imported={imported}&skipped={skipped}", status_code=303
    )


@router.get("/tenders/{tender_id}", response_class=HTMLResponse)
def tender_detail(request: Request, tender_id: int, db: Session = Depends(get_db)):
    user = page_or_login(request, db)
    if not isinstance(user, models.User):
        return user
    t = db.get(models.Tender, tender_id)
    if not t:
        return HTMLResponse("Tender not found", status_code=404)
    profile = _profile_dict(db)
    score, breakdown = match_score(profile, _tender_match_dict(t))
    reqs = (
        db.query(models.Requirement)
        .filter_by(tender_id=t.id, kind="requirement")
        .order_by(models.Requirement.id).all()
    )
    docs_check = (
        db.query(models.Requirement)
        .filter_by(tender_id=t.id, kind="document")
        .order_by(models.Requirement.id).all()
    )
    docs = (
        db.query(models.TenderDocument)
        .filter_by(tender_id=t.id)
        .order_by(models.TenderDocument.id.desc()).all()
    )
    notes = (
        db.query(models.BidNote)
        .filter_by(tender_id=t.id)
        .order_by(models.BidNote.id.desc()).all()
    )
    return templates.TemplateResponse(
        request, "tender_detail.html",
        _ctx(
            request, user, t=t, score=score, breakdown=breakdown,
            reqs=reqs, docs_check=docs_check, docs=docs, notes=notes,
            days_left=_days_left(t), pipeline=STATUS_PIPELINE,
            threshold=MATCH_THRESHOLD,
            uploaded=request.query_params.get("uploaded"),
            analysis=request.query_params.get("analysis"),
        ),
    )


@router.post("/api/tenders/{tender_id}/documents")
def tender_upload_doc(
    tender_id: int,
    kind: str = Form("RFP"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    t = db.get(models.Tender, tender_id)
    if not t:
        return HTMLResponse("Tender not found", status_code=404)
    dest_dir = os.path.join(UPLOAD_ROOT, f"tender_{tender_id}")
    os.makedirs(dest_dir, exist_ok=True)
    safe_name = os.path.basename(file.filename or "document.pdf")
    dest = os.path.join(dest_dir, safe_name)
    with open(dest, "wb") as fh:
        fh.write(file.file.read())
    db.add(models.TenderDocument(
        tender_id=tender_id, filename=safe_name, path=dest, kind=kind.strip() or "RFP",
    ))
    db.commit()

    # Extract text and run RFP analysis; auto-create checklist requirements.
    text = ""
    try:
        reader = pypdf.PdfReader(dest)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        text = ""
    analysis = analyze_rfp(text)
    created = 0
    for req_text in analysis["requirements"][:25]:
        db.add(models.Requirement(
            tender_id=tender_id, text=req_text, kind="requirement",
            is_met=False, auto=True,
        ))
        created += 1
    for risk in analysis["risks"]:
        db.add(models.BidNote(
            tender_id=tender_id,
            text=f"RISK [{risk['type']}]: {risk['detail']}",
        ))
    if created == 0:
        db.add(models.Requirement(
            tender_id=tender_id,
            text="Manually review the uploaded document (no machine-readable requirements found).",
            kind="requirement", is_met=False, auto=True,
        ))
        created = 1
    db.commit()
    return RedirectResponse(
        f"/tenders/{tender_id}?uploaded=1&analysis={created}", status_code=303
    )


@router.post("/api/requirements/{req_id}/toggle")
def requirement_toggle(
    req_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    r = db.get(models.Requirement, req_id)
    if not r:
        return HTMLResponse("Not found", status_code=404)
    r.is_met = not r.is_met
    db.commit()
    return RedirectResponse(f"/tenders/{r.tender_id}", status_code=303)


@router.post("/api/tenders/{tender_id}/requirements")
def requirement_add(
    tender_id: int,
    text: str = Form(...),
    kind: str = Form("requirement"),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    t = db.get(models.Tender, tender_id)
    if not t:
        return HTMLResponse("Tender not found", status_code=404)
    kind = kind if kind in ("requirement", "document") else "requirement"
    if text.strip():
        db.add(models.Requirement(
            tender_id=tender_id, text=text.strip(), kind=kind,
            is_met=False, auto=False,
        ))
        db.commit()
    return RedirectResponse(f"/tenders/{tender_id}", status_code=303)


@router.post("/api/tenders/{tender_id}/notes")
def note_add(
    tender_id: int,
    text: str = Form(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    t = db.get(models.Tender, tender_id)
    if not t:
        return HTMLResponse("Tender not found", status_code=404)
    if text.strip():
        db.add(models.BidNote(tender_id=tender_id, text=text.strip()))
        db.commit()
    return RedirectResponse(f"/tenders/{tender_id}", status_code=303)


@router.post("/api/tenders/{tender_id}/status")
def tender_status(
    tender_id: int,
    status: str = Form(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    t = db.get(models.Tender, tender_id)
    if not t:
        return HTMLResponse("Tender not found", status_code=404)
    if status in STATUS_PIPELINE:
        t.status = status
        db.commit()
    return RedirectResponse(f"/tenders/{tender_id}", status_code=303)


# ---------------------------------------------------------------- sources

@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, db: Session = Depends(get_db)):
    user = page_or_login(request, db)
    if not isinstance(user, models.User):
        return user
    sources = db.query(models.SourceRecord).order_by(models.SourceRecord.id).all()
    staged: dict[str, list[str]] = {}
    for s in sources:
        d = os.path.join(STAGING_ROOT, _stage_slug(s.name))
        files = sorted(f for f in os.listdir(d) if f.lower().endswith(".csv")) if os.path.isdir(d) else []
        staged[s.name] = files
    return templates.TemplateResponse(
        request, "sources.html",
        _ctx(
            request, user, sources=sources, staged=staged,
            synced=request.query_params.get("synced"),
            slug=_stage_slug,
        ),
    )


@router.post("/api/sources/{source_id}/sync")
def source_sync(
    source_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    s = db.get(models.SourceRecord, source_id)
    if not s:
        return HTMLResponse("Source not found", status_code=404)
    d = os.path.join(STAGING_ROOT, _stage_slug(s.name))
    total_in, total_sk = 0, 0
    if os.path.isdir(d):
        for fname in sorted(os.listdir(d)):
            if not fname.lower().endswith(".csv"):
                continue
            with open(os.path.join(d, fname), encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
            imp, sk = import_tender_rows(db, rows, default_source=s.name)
            total_in += imp
            total_sk += sk
    s.last_sync_at = naive_utcnow()
    db.commit()
    return RedirectResponse(f"/sources?synced={total_in}", status_code=303)
