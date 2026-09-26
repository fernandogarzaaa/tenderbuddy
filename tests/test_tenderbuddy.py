"""Tenderbuddy product tests: matcher, RFP analysis, CSV import, bid workspace."""

import io

from fastapi.testclient import TestClient

from app.main import app
from app.matcher import match_score
from app.rfp import analyze_rfp

client = TestClient(app)


def _login():
    r = client.post(
        "/api/auth/login",
        json={"email": "admin@clusterx.local", "password": "ChangeMe123!"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


PROFILE = {
    "turnover_cr": 25.0,
    "years_exp": 8,
    "certifications": ["ISO 9001:2015", "MSME Registered", "GeM Registered"],
    "categories": ["IT Services", "Supply"],
    "states": ["Karnataka", "Maharashtra", "Delhi"],
}

BASE_TENDER = {
    "turnover_req_cr": 10.0,
    "exp_req_years": 5,
    "certs_req": ["ISO 9001:2015", "MSME Registered"],
    "category": "IT Services",
    "state": "Karnataka",
}


# ------------------------------------------------------------ matcher

def test_matcher_full_score():
    score, breakdown = match_score(PROFILE, BASE_TENDER)
    assert score == 100, breakdown
    assert [b["weight"] for b in breakdown] == [30, 20, 25, 15, 10]
    assert all(b["met"] for b in breakdown)
    for b in breakdown:
        assert set(b) >= {"criterion", "required", "actual", "met", "weight", "points"}


def test_matcher_missing_requirements_treated_as_met():
    score, breakdown = match_score(PROFILE, {})
    assert score == 100, breakdown
    assert all(b["met"] for b in breakdown)


def test_matcher_partial_turnover_credit():
    t = dict(BASE_TENDER, turnover_req_cr=50.0)
    score, breakdown = match_score(PROFILE, t)
    turnover = next(b for b in breakdown if b["criterion"] == "Annual turnover")
    assert turnover["met"] is False
    assert turnover["points"] == 15  # 25/50 of 30
    assert score == 85


def test_matcher_cert_fraction():
    t = dict(BASE_TENDER, certs_req=["ISO 9001:2015", "NSIC Registered", "GeM Registered"])
    score, breakdown = match_score(PROFILE, t)
    certs = next(b for b in breakdown if b["criterion"] == "Certifications")
    assert certs["met"] is False
    assert certs["points"] == 17  # round(25 * 2/3)
    assert score == 92


def test_matcher_category_mismatch_zeroes_weight():
    t = dict(BASE_TENDER, category="Consultancy")
    score, breakdown = match_score(PROFILE, t)
    cat = next(b for b in breakdown if b["criterion"] == "Business category")
    assert cat["met"] is False and cat["points"] == 0
    assert score == 85


def test_matcher_state_all_india_profile():
    p = dict(PROFILE, states=["All India"])
    t = dict(BASE_TENDER, state="Tamil Nadu")
    score, breakdown = match_score(p, t)
    state = next(b for b in breakdown if b["criterion"] == "Operating state")
    assert state["met"] is True and state["points"] == 10
    assert score == 100


# ------------------------------------------------------------ RFP analysis

RFP_TEXT = """NOTICE INVITING TENDER
Published on 01/10/2026. Last date for bid submission: 20/10/2026.
Bid opening on 21 October 2026.
Estimated value: Rs 95 lakh. Earnest money deposit of Rs 1.9 lakh.

1. The bidder shall have minimum 5 years of experience in similar works.
2. The bidder must submit OEM authorization letters for all equipment.
3. Bids must be submitted online through the portal only.

Liquidated damages of 0.5% per week shall apply for delays.
A bank guarantee of 10% is required as performance security.
Corrigendum 2 has been issued extending the submission deadline.
"""


def test_rfp_extracts_requirements_dates_amounts():
    out = analyze_rfp(RFP_TEXT)
    assert len(out["requirements"]) >= 3
    assert any("5 years of experience" in r for r in out["requirements"])
    assert any("OEM authorization" in r for r in out["requirements"])
    date_strs = [d["date"] for d in out["dates"]]
    assert "01/10/2026" in date_strs
    assert "20/10/2026" in date_strs
    assert any("95 lakh" in a for a in out["amounts"])
    assert any("1.9 lakh" in a for a in out["amounts"])


def test_rfp_detects_risks():
    out = analyze_rfp(RFP_TEXT)
    types = [r["type"] for r in out["risks"]]
    assert "Penalty clause" in types
    assert "Corrigendum issued" in types
    assert "Security deposit" in types


def test_rfp_short_timeline_risk():
    text = "Published on 01/10/2026. Last date for bid submission: 08/10/2026."
    out = analyze_rfp(text)
    short = [r for r in out["risks"] if r["type"] == "Short timeline"]
    assert short and "7 days" in short[0]["detail"]


def test_rfp_no_short_timeline_when_generous():
    out = analyze_rfp(RFP_TEXT)  # 19 days between published and due
    assert not [r for r in out["risks"] if r["type"] == "Short timeline"]


# ------------------------------------------------------------ API flows

def test_dashboard_renders():
    _login()
    r = client.get("/")
    assert r.status_code == 200, r.text
    assert "Overview" in r.text
    assert "Tenders by category" in r.text


def test_csv_import_creates_tender():
    _login()
    csv_text = (
        "title,ref_no,authority,category,state,est_value_lakh,emd_lakh,"
        "deadline,published_date,turnover_req_cr,exp_req_years,certs_req,"
        "description,source\n"
        "Supply of Test Laptops,TEST/2026/CSV/001,Test Authority,IT Services,"
        "Karnataka,50.0,1.0,2026-11-15,2026-09-25,5.0,3,"
        "ISO 9001:2015|GeM Registered,CSV import test tender,GeM\n"
    )
    r = client.post(
        "/api/tenders/import-csv",
        files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert "Import complete: 1 imported" in r.text
    assert "TEST/2026/CSV/001" in r.text
    # Re-import is idempotent: same ref_no is skipped, not duplicated.
    r2 = client.post(
        "/api/tenders/import-csv",
        files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")},
    )
    assert "0 imported, 1 skipped" in r2.text


def test_tender_detail_shows_match_breakdown():
    _login()
    r = client.get("/tenders/1")
    assert r.status_code == 200, r.text
    assert "Eligibility match" in r.text
    assert "Annual turnover" in r.text
    assert "Requirements checklist" in r.text
    assert "Document checklist" in r.text
    assert "Bid notes" in r.text


def test_rfp_upload_creates_requirements():
    _login()
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in [
        "NOTICE INVITING TENDER",
        "Published on 02/10/2026. Last date for bid submission: 22/10/2026.",
        "Estimated value: Rs 60 lakh.",
        "1. The bidder shall have minimum 3 years of experience.",
        "2. The bidder must submit PAN and GST registration copies.",
        "Liquidated damages of 1% per week shall apply for delays.",
    ]:
        c.drawString(50, y, line)
        y -= 20
    c.save()
    buf.seek(0)

    r = client.post(
        "/api/tenders/1/documents",
        data={"kind": "RFP"},
        files={"file": ("rfp.pdf", buf, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    assert "requirement(s) extracted" in r.text
    assert "3 years of experience" in r.text
    # Risk from the PDF landed in the notes as a RISK entry.
    assert "RISK [Penalty clause]" in r.text


def test_status_pipeline_move():
    _login()
    r = client.post("/api/tenders/1/status", data={"status": "Preparing"})
    assert r.status_code == 200, r.text
    assert 'value="Preparing" selected' in r.text


def test_sources_page_and_sync():
    _login()
    r = client.get("/sources")
    assert r.status_code == 200, r.text
    assert "GeM" in r.text and "CPP Portal" in r.text and "eProcure" in r.text
    assert "Sync now" in r.text


def test_profile_save():
    _login()
    r = client.post(
        "/api/profile",
        data={
            "name": "Vertex Systems Pvt. Ltd.",
            "turnover_cr": "30",
            "years_exp": "9",
            "certifications": "ISO 9001:2015, MSME Registered",
            "categories": "IT Services, Supply",
            "states": "Karnataka, Delhi",
        },
    )
    assert r.status_code == 200, r.text
    assert "Profile saved" in r.text
    assert "Vertex Systems Pvt. Ltd." in r.text
