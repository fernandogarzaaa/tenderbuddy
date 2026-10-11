"""End-to-end tests of the HTML UI and the JSON API through the ASGI app."""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from tenderbuddy.models import utcnow
from tests.conftest import ADMIN_EMAIL, bearer, csrf, load_fixture, login, make_pdf

FUTURE = (utcnow() + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------- platform

def test_health_and_readiness(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok", "version": "1.0.0"}
    assert client.get("/readyz").json() == {"status": "ok", "database": True, "migrations": True}
    assert client.get("/api/health").json()["status"] == "ok"


def test_security_headers_and_request_id(client: TestClient) -> None:
    r = client.get("/login", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in r.headers
    assert "csrf_token" in r.cookies
    assert "Content-Security-Policy" not in client.get("/docs").headers
    assert len(client.get("/healthz").headers["X-Request-ID"]) == 32


def test_hsts_when_secure(app_factory: Callable[..., TestClient]) -> None:
    c = app_factory(COOKIE_SECURE=True)
    assert c.get("/healthz").headers["Strict-Transport-Security"].startswith("max-age=")


def test_static_assets(client: TestClient) -> None:
    assert client.get("/static/css/app.css").status_code == 200
    assert client.get("/static/js/dashboard.js").status_code == 200


def test_404_html_and_json(client: TestClient) -> None:
    assert client.get("/missing", headers={"accept": "text/html"}).status_code == 404
    assert "Error 404" in client.get("/missing", headers={"accept": "text/html"}).text
    assert client.get("/missing").json() == {"detail": "Not Found"}


# ---------------------------------------------------------------- auth

def test_pages_require_login(client: TestClient) -> None:
    for path in ("/", "/tenders", "/sources", "/profile", "/users", "/tenders/1"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/login"


def test_login_logout_cycle(client: TestClient) -> None:
    login(client)
    assert client.get("/").status_code == 200
    assert "access_token" in client.cookies
    r = client.post("/logout", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    client.cookies.delete("access_token")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_failures_and_rate_limit(app_factory: Callable[..., TestClient]) -> None:
    c = app_factory(LOGIN_RATE_LIMIT=2)
    token = csrf(c)
    bad = {"email": ADMIN_EMAIL, "password": "wrong-password", "csrf_token": token}
    assert c.post("/login", data=bad, follow_redirects=False).headers["location"] == "/login?error=invalid"
    assert "Invalid email or password" in c.get("/login?error=invalid").text
    c.post("/login", data=bad, follow_redirects=False)
    good = dict(bad, password="Admin-Password-123")
    assert c.post("/login", data=good, follow_redirects=False).headers["location"] == "/login?error=locked"
    assert "Too many failed attempts" in c.get("/login?error=locked").text
    assert c.post("/api/v1/auth/token", json={"email": ADMIN_EMAIL, "password": "x"}).status_code == 429


def test_login_requires_csrf(client: TestClient) -> None:
    r = client.post("/login", data={"email": ADMIN_EMAIL, "password": "Admin-Password-123"})
    assert r.status_code == 403
    r2 = client.post("/login", data={"email": ADMIN_EMAIL, "password": "Admin-Password-123", "csrf_token": "forged"})
    assert r2.status_code == 403


def test_csrf_enforced_for_cookie_auth_but_not_bearer(auth_client: TestClient) -> None:
    assert auth_client.post("/tenders/new", data={"title": "x"}).status_code == 403
    headers = {"X-CSRF-Token": csrf(auth_client)}
    assert auth_client.post("/api/v1/tenders/import", headers=headers,
                            files={"file": ("a.csv", b"title,deadline\nA,2030-01-01\n")}).status_code == 200
    assert auth_client.post("/api/v1/rfp/analyze", json={"text": "x"}).status_code == 403
    fresh = TestClient(auth_client.app)
    h = bearer(fresh)
    assert fresh.post("/api/v1/rfp/analyze", json={"text": "The bidder shall comply fully."}, headers=h).status_code == 200


def test_api_token_flow(client: TestClient) -> None:
    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me").headers["WWW-Authenticate"] == "Bearer"
    assert client.post("/api/v1/auth/token", json={"email": ADMIN_EMAIL, "password": "nope"}).status_code == 401
    r = client.post("/api/v1/auth/token", json={"email": ADMIN_EMAIL, "password": "Admin-Password-123"})
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 720 * 60
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.json() == {"email": ADMIN_EMAIL, "name": "Admin", "role": "admin"}
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_member_cannot_admin(auth_client: TestClient) -> None:
    r = auth_client.post("/users", data={"email": "m@example.com", "password": "member-password-1", "role": "member",
                                         "csrf_token": csrf(auth_client)})
    assert r.status_code == 200
    assert "User m@example.com created." in r.text
    member = TestClient(auth_client.app)
    login(member, "m@example.com", "member-password-1")
    assert member.get("/users", headers={"accept": "text/html"}).status_code == 403
    assert member.post("/sources/sync", data={"csrf_token": csrf(member)}).status_code == 403
    h = bearer(member, "m@example.com", "member-password-1")
    assert member.post("/api/v1/sources/sync", headers=h).status_code == 403
    assert member.delete("/api/v1/tenders/1", headers=h).status_code == 403
    assert member.get("/api/v1/me", headers=h).json()["role"] == "member"


def test_user_admin_page(auth_client: TestClient) -> None:
    token = csrf(auth_client)
    r = auth_client.post("/users", data={"email": "bad", "password": "x", "csrf_token": token})
    assert "invalid email address" in r.text or "at least 10" in r.text
    auth_client.post("/users", data={"email": "u@example.com", "password": "user-password-1", "csrf_token": token})
    dup = auth_client.post("/users", data={"email": "u@example.com", "password": "user-password-1",
                                           "csrf_token": token})
    assert "already exists" in dup.text
    page = auth_client.get("/users")
    assert "u@example.com" in page.text
    r = auth_client.post("/users/2/toggle", data={"csrf_token": token})
    assert "u@example.com deactivated." in r.text
    assert "You cannot deactivate yourself." in auth_client.post("/users/1/toggle", data={"csrf_token": token}).text
    assert auth_client.post("/users/99/toggle", data={"csrf_token": token}).status_code == 404
    blocked = TestClient(auth_client.app)
    r = blocked.post("/login", data={"email": "u@example.com", "password": "user-password-1",
                                     "csrf_token": csrf(blocked)}, follow_redirects=False)
    assert r.headers["location"] == "/login?error=invalid"


# ---------------------------------------------------------------- UI flows

@pytest.fixture
def demo_client(app_factory: Callable[..., TestClient]) -> TestClient:
    return login(app_factory(SEED_DEMO_DATA=True))


def test_dashboard_and_lists(demo_client: TestClient) -> None:
    r = demo_client.get("/")
    assert "Overview" in r.text
    assert "Best matches" in r.text
    assert "Demo data" in r.text
    assert 'id="chart-data"' in r.text
    lst = demo_client.get("/tenders")
    assert "Supply of Desktop Computers" in lst.text
    assert "INR 12.00M" in lst.text
    filtered = demo_client.get("/tenders?q=solar&open=0&sort=score&min_score=10&status=New&source=demo")
    assert "Rooftop Solar" in filtered.text
    assert "Desktop Computers" not in filtered.text
    junk = demo_client.get("/tenders?page=abc&min_score=xyz&sort=evil&status=Hacked")
    assert junk.status_code == 200


def test_empty_profile_banner(auth_client: TestClient) -> None:
    assert "Your company profile is empty" in auth_client.get("/").text


def test_tender_detail_workspace(demo_client: TestClient) -> None:
    token = csrf(demo_client)
    page = demo_client.get("/tenders/1")
    assert "Eligibility match" in page.text
    assert "Annual turnover" in page.text
    assert "Requirements checklist" in page.text
    assert "Good fit: we already supply" in page.text
    assert demo_client.get("/tenders/999", headers={"accept": "text/html"}).status_code == 404

    r = demo_client.post("/tenders/1/status", data={"status": "Preparing", "csrf_token": token})
    assert "Status set to Preparing." in r.text
    assert 'value="Preparing" selected' in r.text
    assert "Unknown status." in demo_client.post("/tenders/1/status", data={"status": "Bogus", "csrf_token": token}).text

    demo_client.post("/tenders/1/requirements", data={"text": "Bring 3 references", "csrf_token": token})
    demo_client.post("/tenders/1/requirements", data={"text": "Signed form", "kind": "document", "csrf_token": token})
    demo_client.post("/tenders/1/requirements", data={"text": "   ", "csrf_token": token})
    demo_client.post("/tenders/1/notes", data={"text": "Call the buyer", "csrf_token": token})
    page = demo_client.get("/tenders/1")
    assert "Bring 3 references" in page.text
    assert "Signed form" in page.text
    assert "Call the buyer" in page.text

    api = demo_client.get("/api/v1/tenders/1", headers={"X-CSRF-Token": token}).json()
    req = next(r for r in api["requirements"] if r["text"] == "Bring 3 references")
    demo_client.post(f"/requirements/{req['id']}/toggle", data={"csrf_token": token})
    assert next(r for r in demo_client.get("/api/v1/tenders/1").json()["requirements"]
                if r["id"] == req["id"])["is_met"] is True
    demo_client.post(f"/requirements/{req['id']}/delete", data={"csrf_token": token})
    assert all(r["id"] != req["id"] for r in demo_client.get("/api/v1/tenders/1").json()["requirements"])
    assert demo_client.post("/requirements/9999/toggle", data={"csrf_token": token}).status_code == 404
    assert demo_client.post("/requirements/9999/delete", data={"csrf_token": token}).status_code == 404


def test_upload_document_ui(demo_client: TestClient) -> None:
    token = csrf(demo_client)
    pdf = make_pdf(["1. The bidder shall have 3 years of experience.", "Liquidated damages of 1% apply."])
    r = demo_client.post("/tenders/2/documents", data={"kind": "RFP", "csrf_token": token},
                         files={"file": ("rfp.pdf", io.BytesIO(pdf), "application/pdf")})
    assert "Document analysed:" in r.text
    assert "3 years of experience" in r.text
    assert "RISK [Penalty clause]" in r.text
    doc_id = demo_client.get("/api/v1/tenders/2").json()["documents"][0]["id"]
    dl = demo_client.get(f"/documents/{doc_id}")
    assert dl.status_code == 200
    assert dl.content.startswith(b"%PDF")
    bad = demo_client.post("/tenders/2/documents", data={"kind": "RFP", "csrf_token": token},
                           files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert "Upload rejected: only PDF files are accepted" in bad.text
    assert demo_client.get("/documents/999").status_code == 404


def test_summary_and_draft_ui(demo_client: TestClient) -> None:
    token = csrf(demo_client)
    r = demo_client.post("/tenders/1/summary", data={"csrf_token": token})
    assert "Summary generated (rules)." in r.text
    r = demo_client.post("/tenders/1/drafts", data={"csrf_token": token})
    assert "Bid draft generated (template)." in r.text
    assert "# Bid response: Supply of Desktop Computers" in r.text
    md = demo_client.get("/drafts/1/download.md")
    assert md.headers["content-type"].startswith("text/markdown")
    assert "attachment" in md.headers["content-disposition"]
    pdf = demo_client.get("/drafts/1/download.pdf")
    assert pdf.content.startswith(b"%PDF")
    assert demo_client.get("/drafts/99", headers={"accept": "text/html"}).status_code == 404


def test_manual_tender_create_ui(auth_client: TestClient) -> None:
    token = csrf(auth_client)
    assert "Add a tender manually" in auth_client.get("/tenders/new").text
    bad = auth_client.post("/tenders/new", data={"title": "x", "deadline": "", "currency": "EURO",
                                                 "csrf_token": token})
    assert bad.status_code == 422
    assert "currency must be a 3-letter ISO code" in bad.text
    ok = auth_client.post("/tenders/new", data={
        "title": "Barangay hall repair", "deadline": FUTURE, "currency": "php", "estimated_value": "2500000",
        "bid_security": "", "country": "PHL", "region": "Camarines Sur", "certs_required": "PCAB License",
        "url": "https://example.org/n", "csrf_token": token})
    assert "Tender created." in ok.text
    assert "Barangay hall repair" in ok.text
    assert "PHP 2.50M" in ok.text
    assert "PCAB License certificate copy" in ok.text


def test_csv_import_ui(auth_client: TestClient) -> None:
    token = csrf(auth_client)
    csv_text = "title,deadline,ref_no\nWater pumps,2030-05-01,WP-1\nNo deadline,,X\n"
    r = auth_client.post("/tenders/import", data={"source": "PhilGEPS", "csrf_token": token},
                         files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")})
    assert "Import complete: 1 new, 0 updated, 0 unchanged, 1 skipped." in r.text
    assert "First problem: row 3" in r.text
    r2 = auth_client.post("/tenders/import", data={"csrf_token": token},
                          files={"file": ("t.csv", io.BytesIO(csv_text.encode()), "text/csv")})
    assert "0 new" in r2.text
    bad = auth_client.post("/tenders/import", data={"csrf_token": token},
                           files={"file": ("t.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")})
    assert "Import failed: CSV is missing required column(s)" in bad.text


def test_profile_ui(auth_client: TestClient) -> None:
    token = csrf(auth_client)
    r = auth_client.post("/profile", data={"name": "Acme", "currency": "gbp", "annual_turnover": "1500000",
                                           "years_experience": "4", "certifications": "ISO 9001, ISO 9001",
                                           "regions": "GBR", "cpv_prefixes": "72, 48", "csrf_token": token})
    assert "Profile saved." in r.text
    assert 'value="GBP"' in r.text
    assert 'value="ISO 9001"' in r.text
    bad = auth_client.post("/profile", data={"annual_turnover": "-5", "cpv_prefixes": "IT", "csrf_token": token})
    assert bad.status_code == 422
    assert "CPV prefixes must be 1-8 digits" in bad.text


def test_admin_deletes_tender(demo_client: TestClient) -> None:
    r = demo_client.post("/tenders/3/delete", data={"csrf_token": csrf(demo_client)})
    assert "Tender deleted." in r.text
    assert demo_client.get("/api/v1/tenders/3").status_code == 404


# ---------------------------------------------------------------- sources

def test_sources_page_and_sync(app_factory: Callable[..., TestClient], tmp_path) -> None:  # type: ignore[no-untyped-def]
    c = login(app_factory(CONNECTORS="csv,sam_gov"))
    token = csrf(c)
    staging = tmp_path / "data" / "staging" / "gem"
    staging.mkdir(parents=True)
    (staging / "export.csv").write_text("title,deadline\nPrinters,2030-01-01\n", encoding="utf-8")
    page = c.get("/sources")
    assert "CSV staging folder" in page.text
    assert "gem/export.csv" in page.text
    assert "Needs SAM_API_KEY" in page.text
    r = c.post("/sources/csv/sync", data={"csrf_token": token})
    assert "CSV staging folder: 1 new, 0 updated, 0 skipped." in r.text
    r = c.post("/sources/sam_gov/sync", data={"csrf_token": token})
    assert "SAM_API_KEY is not set" in r.text
    r = c.post("/sources/sync", data={"csrf_token": token})
    assert "Sync complete: 0 new, 0 updated. Problems: SAM.gov (US)" in r.text
    assert "That source is not enabled." in c.post("/sources/ted/sync", data={"csrf_token": token}).text
    r = c.post("/sources/csv/toggle", data={"csrf_token": token})
    assert "CSV staging folder paused." in r.text
    assert "CSV staging folder resumed." in c.post("/sources/csv/toggle", data={"csrf_token": token}).text
    assert c.post("/sources/ted/toggle", data={"csrf_token": token}).status_code == 404


# ---------------------------------------------------------------- JSON API

def test_api_tender_crud(client: TestClient, api_headers: dict[str, str]) -> None:
    h = api_headers
    assert client.post("/api/v1/tenders", json={"title": "x"}, headers=h).status_code == 422
    created = client.post("/api/v1/tenders", headers=h, json={
        "title": "Data centre cooling", "deadline": FUTURE + ":00Z", "currency": "EUR", "estimated_value": 900000,
        "min_turnover": 500000, "certs_required": ["ISO 9001"], "cpv_codes": "42500000", "country": "IRL"})
    assert created.status_code == 201
    t = created.json()
    assert t["source"] == "manual"
    assert t["deadline"].endswith("Z")
    assert t["match"]["breakdown"][0]["key"] == "turnover"
    tid = t["id"]
    lst = client.get("/api/v1/tenders?open_only=true&sort=score&per_page=5", headers=h).json()
    assert lst["total"] == 1
    assert lst["items"][0]["id"] == tid
    assert client.get("/api/v1/tenders?sort=evil", headers=h).status_code == 422
    assert client.patch(f"/api/v1/tenders/{tid}", json={"status": "Reviewing"}, headers=h).json()["status"] == "Reviewing"
    assert client.patch(f"/api/v1/tenders/{tid}", json={"status": "Nope"}, headers=h).status_code == 422
    req = client.post(f"/api/v1/tenders/{tid}/requirements", json={"text": "Site visit", "kind": "requirement"},
                      headers=h).json()
    assert client.patch(f"/api/v1/requirements/{req['id']}", json={"is_met": True}, headers=h).json()["is_met"]
    assert client.patch("/api/v1/requirements/999", json={"is_met": True}, headers=h).status_code == 404
    assert client.delete(f"/api/v1/requirements/{req['id']}", headers=h).status_code == 204
    assert client.post(f"/api/v1/tenders/{tid}/notes", json={"text": "hello"}, headers=h).status_code == 201
    pdf = make_pdf(["The supplier must provide 24x7 support."])
    up = client.post(f"/api/v1/tenders/{tid}/documents", headers=h, data={"kind": "RFP"},
                     files={"file": ("r.pdf", pdf, "application/pdf")})
    assert up.status_code == 201
    assert up.json()["requirements_created"] == 1
    assert client.post(f"/api/v1/tenders/{tid}/documents", headers=h, data={"kind": "Nope"},
                       files={"file": ("r.pdf", pdf, "application/pdf")}).status_code == 422
    s = client.post(f"/api/v1/tenders/{tid}/summary", headers=h).json()
    assert s["generator"] == "rules"
    d = client.post(f"/api/v1/tenders/{tid}/drafts", headers=h).json()
    assert d["content"].startswith("# Bid response: Data centre cooling")
    assert client.get(f"/api/v1/drafts/{d['id']}", headers=h).json()["tender_id"] == tid
    assert client.get(f"/api/v1/drafts/{d['id']}/markdown", headers=h).text == d["content"]
    assert client.get("/api/v1/drafts/999", headers=h).status_code == 404
    detail = client.get(f"/api/v1/tenders/{tid}", headers=h).json()
    assert [n["text"] for n in detail["notes"]][-1] == "hello"
    assert len(detail["drafts"]) == 1
    assert client.delete(f"/api/v1/tenders/{tid}", headers=h).status_code == 204
    assert client.get(f"/api/v1/tenders/{tid}", headers=h).status_code == 404
    assert client.post("/api/v1/tenders/999/notes", json={"text": "x"}, headers=h).status_code == 404


def test_api_profile_and_import(client: TestClient, api_headers: dict[str, str]) -> None:
    h = api_headers
    assert client.get("/api/v1/profile", headers=h).json()["currency"] == "EUR"
    put = client.put("/api/v1/profile", headers=h, json={"name": "Acme", "currency": "usd", "annual_turnover": 5,
                                                          "regions": ["USA"], "keywords": "cloud, hosting"})
    assert put.json()["currency"] == "USD"
    assert put.json()["keywords"] == ["cloud", "hosting"]
    assert client.put("/api/v1/profile", headers=h, json={"years_experience": -1}).status_code == 422
    r = client.post("/api/v1/tenders/import", headers=h, data={"source": "upload"},
                    files={"file": ("a.csv", b"title,deadline\nA,2030-01-01\nB,bad\n")})
    assert r.json()["created"] == 1
    assert r.json()["skipped"] == 1
    assert client.post("/api/v1/tenders/import", headers=h, files={"file": ("a.csv", b"x\n1\n")}).status_code == 422
    analysis = client.post("/api/v1/rfp/analyze", headers=h, json={"text": "Penalty applies. Rs 5 lakh."}).json()
    assert analysis["amounts"] == ["Rs 5 lakh"]
    assert client.post("/api/v1/rfp/analyze", headers=h, json={"text": ""}).json()["detail"] == "Validation failed"


def test_api_sources(app_factory: Callable[..., TestClient]) -> None:
    c = app_factory(CONNECTORS="ted,csv", TED_API_URL="https://ted.invalid/search")
    h = bearer(c)
    names = [s["name"] for s in c.get("/api/v1/sources", headers=h).json()]
    assert names == ["ted", "csv"]
    assert c.post("/api/v1/sources/nope/sync", headers=h).status_code == 404
    assert c.post("/api/v1/sources/sam_gov/sync", headers=h).status_code == 409
    assert c.post("/api/v1/sources/csv/sync", headers=h).json()["created"] == 0
    toggled = c.patch("/api/v1/sources/csv?enabled=false", headers=h).json()
    assert toggled["enabled"] is False
    assert c.patch("/api/v1/sources/zzz?enabled=false", headers=h).status_code == 404
    out = c.post("/api/v1/sources/sync", headers=h).json()
    assert list(out) == ["ted"]  # csv paused
    assert out["ted"]["errors"]


def test_api_sync_with_live_shaped_ted(app_factory: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> None:
    data = load_fixture("ted_search.json")
    real_client = httpx.Client

    def fake_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, json=data))
        return real_client(*args, **kwargs)

    monkeypatch.setattr("tenderbuddy.connectors.base.httpx.Client", fake_client)
    c = app_factory(CONNECTORS="ted")
    h = bearer(c)
    out = c.post("/api/v1/sources/ted/sync", headers=h).json()
    assert out["created"] == len(data["notices"])
    items = c.get("/api/v1/tenders?source=ted", headers=h).json()["items"]
    assert {i["country"] for i in items} <= {"IRL", "MLT"}
