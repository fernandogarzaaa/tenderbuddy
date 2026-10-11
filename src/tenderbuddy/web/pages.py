"""Server-rendered UI: dashboard, tenders, bid workspace, sources, profile, users."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from tenderbuddy import models
from tenderbuddy.bidding import (
    ALLOWED_DOC_KINDS,
    UploadRejected,
    add_document,
    create_manual_tender,
    generate_draft,
    generate_summary,
)
from tenderbuddy.connectors import LABELS
from tenderbuddy.connectors.csv_import import MAX_CSV_BYTES, CsvConnector, CsvFormatError, parse_csv_bytes
from tenderbuddy.export import markdown_to_pdf
from tenderbuddy.models import OPEN_STATUSES, STATUS_PIPELINE, utcnow
from tenderbuddy.schemas import ProfileIn, TenderIn, UserIn
from tenderbuddy.security import PasswordPolicyError
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
from tenderbuddy.users import UserExists, create_user
from tenderbuddy.web.deps import AdminPageUser, DbDep, LLMDep, PageUser, SettingsDep
from tenderbuddy.web.templating import render

router = APIRouter(include_in_schema=False)
MATCH_THRESHOLD = 60
OPTIONAL_NUMBERS = ("estimated_value", "bid_security", "min_turnover", "min_experience_years", "published_at")


def redirect(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg[:250])}"
    elif err:
        url += f"{sep}err={quote(err[:250])}"
    return RedirectResponse(url, status_code=303)


def tender_or_404(db: Session, tender_id: int) -> models.Tender:
    t = db.get(models.Tender, tender_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Tender not found")
    return t


def format_errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())


async def form_fields(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if k != "csrf_token" and isinstance(v, str)}


def int_param(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw or default)))
    except ValueError:
        return default


# ---------------------------------------------------------------- dashboard

@router.get("/")
def dashboard(request: Request, db: DbDep, user: PageUser) -> Response:
    now = utcnow()
    profile = get_profile(db)
    tenders = list(db.scalars(select(models.Tender)))
    open_t = [t for t in tenders if t.status in OPEN_STATUSES and t.deadline >= now]
    scored = sorted(((t, score_tender(profile, t).score) for t in open_t), key=lambda r: (-r[1], r[0].deadline))
    matched = [r for r in scored if r[1] >= MATCH_THRESHOLD]
    avg = round(sum(s for _, s in scored) / len(scored), 1) if scored else 0.0
    alerts = sorted((t for t in open_t if t.deadline <= now + timedelta(days=7)), key=lambda t: t.deadline)
    by_source: dict[str, int] = {}
    by_month: dict[str, int] = {}
    for t in tenders:
        label = LABELS.get(t.source, t.source)
        by_source[label] = by_source.get(label, 0) + 1
        month = (t.published_at or t.created_at).strftime("%Y-%m")
        by_month[month] = by_month.get(month, 0) + 1
    months = sorted(by_month)[-12:]
    chart = {"source": {"labels": list(by_source), "values": list(by_source.values())},
             "month": {"labels": months, "values": [by_month[m] for m in months]}}
    return render(request, "dashboard.html", user=user, nav="overview", open_count=len(open_t),
                  matched_count=len(matched), avg_score=avg, alerts=alerts, top=matched[:8], chart=chart,
                  threshold=MATCH_THRESHOLD, profile_incomplete=not profile.name)


# ---------------------------------------------------------------- tenders

@router.get("/tenders")
def tenders_page(request: Request, db: DbDep, user: PageUser) -> Response:
    qp = request.query_params
    sort = qp.get("sort", "deadline") if qp.get("sort") in SORTS else "deadline"
    status = qp.get("status", "") if qp.get("status") in STATUS_PIPELINE else ""
    filters = {"q": qp.get("q", "")[:200], "status": status, "source": qp.get("source", "")[:64],
               "min_score": int_param(qp.get("min_score"), 0, 0, 100), "sort": sort,
               "open": "0" if qp.get("open") == "0" else "1"}
    res = list_tenders(db, q=filters["q"], status=status, source=filters["source"], open_only=filters["open"] == "1",
                       min_score=filters["min_score"], sort=sort, page=int_param(qp.get("page"), 1, 1, 10**6))
    sources = sorted(s for (s,) in db.execute(select(models.Tender.source).distinct()))
    return render(request, "tenders.html", user=user, nav="tenders", res=res, filters=filters,
                  query=urlencode(filters), sources=sources, statuses=STATUS_PIPELINE, sorts=SORTS,
                  threshold=MATCH_THRESHOLD)


@router.get("/tenders/new")
def tender_new_page(request: Request, user: PageUser) -> Response:
    return render(request, "tender_new.html", user=user, nav="tenders", form={})


@router.post("/tenders/new")
async def tender_create(request: Request, db: DbDep, user: PageUser) -> Response:
    data = await form_fields(request)
    clean: dict[str, str | None] = {
        k: (v.strip() or None) if k in OPTIONAL_NUMBERS else v.strip() for k, v in data.items()
    }
    try:
        payload = TenderIn.model_validate(clean)
    except ValidationError as exc:
        return render(request, "tender_new.html", status_code=422, user=user, nav="tenders", form=data,
                      error=format_errors(exc))
    t = create_manual_tender(db, payload)
    return redirect(f"/tenders/{t.id}", msg="Tender created.")


@router.post("/tenders/import")
async def tenders_import_csv(db: DbDep, user: PageUser, file: Annotated[UploadFile, File()],
                             source: Annotated[str, Form(max_length=64)] = "csv") -> Response:
    data = await file.read(MAX_CSV_BYTES + 1)
    try:
        parsed = parse_csv_bytes(data, source or "csv")
    except CsvFormatError as exc:
        return redirect("/tenders", err=f"Import failed: {exc}")
    stats = upsert_records(db, parsed.records)
    msg = (f"Import complete: {stats.created} new, {stats.updated} updated, {stats.unchanged} unchanged, "
           f"{parsed.skipped + stats.skipped} skipped.")
    if parsed.errors:
        msg += " First problem: " + parsed.errors[0]
    return redirect("/tenders?open=0", msg=msg)


@router.get("/tenders/{tender_id}")
def tender_detail(request: Request, tender_id: int, db: DbDep, settings: SettingsDep, user: PageUser) -> Response:
    t = tender_or_404(db, tender_id)
    return render(request, "tender_detail.html", user=user, nav="tenders", t=t,
                  match=score_tender(get_profile(db), t),
                  reqs=[r for r in t.requirements if r.kind == "requirement"],
                  docs_check=[r for r in t.requirements if r.kind == "document"],
                  pipeline=STATUS_PIPELINE, threshold=MATCH_THRESHOLD, doc_kinds=ALLOWED_DOC_KINDS,
                  llm_mode=settings.llm_mode, max_upload_mb=settings.MAX_UPLOAD_MB)


@router.post("/tenders/{tender_id}/status")
def tender_status(tender_id: int, db: DbDep, user: PageUser, status: Annotated[str, Form()]) -> Response:
    t = tender_or_404(db, tender_id)
    if status not in STATUS_PIPELINE:
        return redirect(f"/tenders/{tender_id}", err="Unknown status.")
    t.status = status
    db.commit()
    return redirect(f"/tenders/{tender_id}", msg=f"Status set to {status}.")


@router.post("/tenders/{tender_id}/delete")
def tender_delete(tender_id: int, db: DbDep, user: AdminPageUser) -> Response:
    db.delete(tender_or_404(db, tender_id))
    db.commit()
    return redirect("/tenders", msg="Tender deleted.")


@router.post("/tenders/{tender_id}/documents")
async def tender_upload_doc(tender_id: int, db: DbDep, settings: SettingsDep, llm: LLMDep, user: PageUser,
                            file: Annotated[UploadFile, File()], kind: Annotated[str, Form()] = "RFP") -> Response:
    t = tender_or_404(db, tender_id)
    data = await file.read(settings.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    try:
        out = add_document(db, settings, llm, t, file.filename, data, kind)
    except UploadRejected as exc:
        return redirect(f"/tenders/{tender_id}", err=f"Upload rejected: {exc}")
    return redirect(f"/tenders/{tender_id}", msg=f"Document analysed: {out.requirements_created} requirement(s) "
                                                 f"extracted, {len(out.risks)} risk flag(s).")


@router.get("/documents/{doc_id}")
def document_download(doc_id: int, db: DbDep, settings: SettingsDep, user: PageUser) -> Response:
    d = db.get(models.TenderDocument, doc_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Document not found")
    path = Path(d.path).resolve()
    if settings.upload_dir.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Document file missing")
    return FileResponse(path, media_type="application/pdf", filename=d.filename)


@router.post("/requirements/{req_id}/toggle")
def requirement_toggle(req_id: int, db: DbDep, user: PageUser) -> Response:
    r = db.get(models.Requirement, req_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    r.is_met = not r.is_met
    db.commit()
    return RedirectResponse(f"/tenders/{r.tender_id}#checklists", status_code=303)


@router.post("/requirements/{req_id}/delete")
def requirement_delete(req_id: int, db: DbDep, user: PageUser) -> Response:
    r = db.get(models.Requirement, req_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    tender_id = r.tender_id
    db.delete(r)
    db.commit()
    return RedirectResponse(f"/tenders/{tender_id}#checklists", status_code=303)


@router.post("/tenders/{tender_id}/requirements")
def requirement_add(tender_id: int, db: DbDep, user: PageUser, text: Annotated[str, Form(max_length=2000)],
                    kind: Annotated[str, Form()] = "requirement") -> Response:
    t = tender_or_404(db, tender_id)
    if text.strip():
        db.add(models.Requirement(tender_id=t.id, text=text.strip(),
                                  kind=kind if kind in ("requirement", "document") else "requirement"))
        db.commit()
    return RedirectResponse(f"/tenders/{tender_id}#checklists", status_code=303)


@router.post("/tenders/{tender_id}/notes")
def note_add(tender_id: int, db: DbDep, user: PageUser, text: Annotated[str, Form(max_length=5000)]) -> Response:
    t = tender_or_404(db, tender_id)
    if text.strip():
        db.add(models.BidNote(tender_id=t.id, text=text.strip()))
        db.commit()
    return RedirectResponse(f"/tenders/{tender_id}#notes", status_code=303)


@router.post("/tenders/{tender_id}/summary")
def tender_summary(tender_id: int, db: DbDep, settings: SettingsDep, llm: LLMDep, user: PageUser) -> Response:
    generator = generate_summary(db, settings, llm, tender_or_404(db, tender_id))
    return redirect(f"/tenders/{tender_id}", msg=f"Summary generated ({generator}).")


@router.post("/tenders/{tender_id}/drafts")
def tender_draft(tender_id: int, db: DbDep, settings: SettingsDep, llm: LLMDep, user: PageUser) -> Response:
    d = generate_draft(db, settings, llm, tender_or_404(db, tender_id))
    return redirect(f"/drafts/{d.id}", msg=f"Bid draft generated ({d.generator}).")


def draft_or_404(db: Session, draft_id: int) -> models.BidDraft:
    d = db.get(models.BidDraft, draft_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return d


@router.get("/drafts/{draft_id}")
def draft_view(request: Request, draft_id: int, db: DbDep, user: PageUser) -> Response:
    d = draft_or_404(db, draft_id)
    return render(request, "draft.html", user=user, nav="tenders", d=d, t=d.tender)


@router.get("/drafts/{draft_id}/download.md")
def draft_md(draft_id: int, db: DbDep, user: PageUser) -> Response:
    d = draft_or_404(db, draft_id)
    return Response(d.content, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="bid-draft-{d.id}.md"'})


@router.get("/drafts/{draft_id}/download.pdf")
def draft_pdf(draft_id: int, db: DbDep, user: PageUser) -> Response:
    d = draft_or_404(db, draft_id)
    return Response(markdown_to_pdf(d.content, title=d.tender.title), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="bid-draft-{d.id}.pdf"'})


# ---------------------------------------------------------------- sources

@router.get("/sources")
def sources_page(request: Request, db: DbDep, settings: SettingsDep, user: PageUser) -> Response:
    staging = CsvConnector(staging_dir=settings.staging_dir)
    staged = [str(p.relative_to(staging.staging_dir)) for p in staging.staged_files()]
    return render(request, "sources.html", user=user, nav="sources", sources=ensure_sources(db, settings),
                  labels=LABELS, staged=staged, staging_dir=str(settings.staging_dir), enabled=settings.CONNECTORS,
                  interval=settings.SYNC_INTERVAL_MINUTES, sam_key=bool(settings.SAM_API_KEY))


@router.post("/sources/sync")
def sources_sync_all(db: DbDep, settings: SettingsDep, user: AdminPageUser) -> Response:
    out = sync_all(db, settings)
    msg = (f"Sync complete: {sum(s.created for s in out.values())} new, "
           f"{sum(s.updated for s in out.values())} updated.")
    failed = [LABELS.get(n, n) for n, s in out.items() if s.failed]
    if failed:
        msg += " Problems: " + ", ".join(failed) + " (see cards below)."
    return redirect("/sources", msg=msg)


@router.post("/sources/{name}/sync")
def source_sync(name: str, db: DbDep, settings: SettingsDep, user: AdminPageUser) -> Response:
    if name not in settings.CONNECTORS:
        return redirect("/sources", err="That source is not enabled.")
    st = sync_source(db, settings, name)
    label = LABELS.get(name, name)
    if st.failed:
        return redirect("/sources", err=f"{label}: {st.errors[0]}")
    return redirect("/sources", msg=f"{label}: {st.created} new, {st.updated} updated, {st.skipped} skipped.")


@router.post("/sources/{name}/toggle")
def source_toggle(name: str, db: DbDep, settings: SettingsDep, user: AdminPageUser) -> Response:
    ensure_sources(db, settings)
    s = db.scalar(select(models.Source).where(models.Source.name == name))
    if s is None:
        raise HTTPException(status_code=404, detail="Unknown source")
    s.enabled = not s.enabled
    db.commit()
    return redirect("/sources", msg=f"{LABELS.get(name, name)} {'resumed' if s.enabled else 'paused'}.")


# ---------------------------------------------------------------- profile

@router.get("/profile")
def profile_page(request: Request, db: DbDep, user: PageUser) -> Response:
    return render(request, "profile.html", user=user, nav="profile", p=get_profile(db))


@router.post("/profile")
async def profile_save(request: Request, db: DbDep, user: PageUser) -> Response:
    try:
        payload = ProfileIn.model_validate(await form_fields(request))
    except ValidationError as exc:
        return render(request, "profile.html", status_code=422, user=user, nav="profile", p=get_profile(db),
                      error=format_errors(exc))
    p = get_profile(db)
    for k, v in payload.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return redirect("/profile", msg="Profile saved.")


# ---------------------------------------------------------------- users (admin)

@router.get("/users")
def users_page(request: Request, db: DbDep, user: AdminPageUser) -> Response:
    users = list(db.scalars(select(models.User).order_by(models.User.id)))
    return render(request, "users.html", user=user, nav="users", users=users)


@router.post("/users")
async def users_create(request: Request, db: DbDep, user: AdminPageUser) -> Response:
    try:
        payload = UserIn.model_validate(await form_fields(request))
        create_user(db, payload.email, payload.password, name=payload.name, role=payload.role)
    except ValidationError as exc:
        return redirect("/users", err=format_errors(exc))
    except (UserExists, PasswordPolicyError) as exc:
        return redirect("/users", err=str(exc))
    return redirect("/users", msg=f"User {payload.email} created.")


@router.post("/users/{user_id}/toggle")
def users_toggle(user_id: int, db: DbDep, user: AdminPageUser) -> Response:
    target = db.get(models.User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == user.id:
        return redirect("/users", err="You cannot deactivate yourself.")
    target.is_active = not target.is_active
    db.commit()
    return redirect("/users", msg=f"{target.email} {'activated' if target.is_active else 'deactivated'}.")
