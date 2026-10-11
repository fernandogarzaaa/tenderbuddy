"""Connector tests against recorded API responses (tests/fixtures) via httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from tenderbuddy.config import Settings
from tenderbuddy.connectors import LABELS, REGISTRY, build_connector
from tenderbuddy.connectors.base import (
    Connector,
    ConnectorConfigError,
    ConnectorError,
    FetchResult,
    TenderRecord,
    dedupe,
    parse_float,
    parse_iso_datetime,
    pick_lang,
    to_naive_utc,
)
from tenderbuddy.connectors.contracts_finder import ContractsFinderConnector, parse_release
from tenderbuddy.connectors.csv_import import (
    CsvConnector,
    CsvFormatError,
    parse_csv_bytes,
    parse_csv_date,
    row_to_record,
    slug,
    split_list,
)
from tenderbuddy.connectors.sam_gov import SamGovConnector, clean, parse_opportunity
from tenderbuddy.connectors.ted import TedConnector, build_query, notice_deadline, notice_title, parse_notice
from tests.conftest import load_fixture, make_settings, mock_client

SINCE = datetime(2026, 10, 5)


# ---------------------------------------------------------------- base helpers

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-04T12:00:00Z", datetime(2026, 11, 4, 12, 0)),
        ("2026-11-14T17:00:00-05:00", datetime(2026, 11, 14, 22, 0)),
        ("2026-11-06+01:00", datetime(2026, 11, 6, 22, 59, 59)),
        ("2026-11-06-02:00", datetime(2026, 11, 7, 1, 59, 59)),
        ("2026-10-31Z", datetime(2026, 10, 31, 23, 59, 59)),
        ("2026-10-31", datetime(2026, 10, 31, 23, 59, 59)),
        ("2026-11-04 08:00:00", datetime(2026, 11, 4, 8, 0)),
        ("20261104", None),
        ("2026-11-06*01:00", None),
        ("2026-11-06+0100", None),
        ("2026-13-01+01:00", None),
        ("garbage", None),
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_parse_iso_datetime(raw: str | None, expected: datetime | None) -> None:
    assert parse_iso_datetime(raw) == expected


def test_parse_iso_datetime_start_of_day() -> None:
    assert parse_iso_datetime("2026-11-06+01:00", end_of_day=False) == datetime(2026, 11, 5, 23, 0)
    assert parse_iso_datetime("2026-11-06T10:00:00Z", end_of_day=False) == datetime(2026, 11, 6, 10, 0)


def test_small_helpers() -> None:
    assert parse_float("1,234.5") == 1234.5
    assert parse_float(7) == 7.0
    assert parse_float("") is None
    assert parse_float(None) is None
    assert parse_float("n/a") is None
    assert pick_lang({"fra": "Bonjour", "eng": ["Hello", "x"]}) == "Hello"
    assert pick_lang({"fra": "Bonjour"}) == "Bonjour"
    assert pick_lang({"en": "Hi"}) == "Hi"
    assert pick_lang({}) == ""
    assert pick_lang([]) == ""
    assert pick_lang(None) == ""
    assert pick_lang(42) == "42"
    assert dedupe([" a", "a", "", "b", "a "]) == ["a", "b"]
    aware = datetime.fromisoformat("2026-01-01T10:00:00+02:00")
    assert to_naive_utc(aware) == datetime(2026, 1, 1, 8, 0)
    assert to_naive_utc(datetime(2026, 1, 1)) == datetime(2026, 1, 1)


def test_tender_record_normalises() -> None:
    rec = TenderRecord(source="x", external_id=" 42 ", title="  A   title \n here ", currency="eur",
                       deadline=datetime.fromisoformat("2026-01-01T12:00:00+01:00"),
                       published_at=datetime.fromisoformat("2026-01-01T00:00:00-01:00"))
    assert rec.external_id == "42"
    assert rec.ref_no == "42"
    assert rec.title == "A title here"
    assert rec.currency == "EUR"
    assert rec.deadline == datetime(2026, 1, 1, 11, 0)
    assert rec.published_at == datetime(2026, 1, 1, 1, 0)
    with pytest.raises(ValueError, match="external_id"):
        TenderRecord(source="x", external_id=" ", title="t", deadline=SINCE)
    with pytest.raises(ValueError, match="title"):
        TenderRecord(source="x", external_id="1", title=" ", deadline=SINCE)
    assert len(TenderRecord(source="x", external_id="1", title="t" * 2000, deadline=SINCE).title) == 1000


class _Dummy(Connector):
    name = "dummy"

    def fetch(self, since: datetime, max_records: int) -> FetchResult:
        return FetchResult()


def test_request_json_retries_then_succeeds() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    c = _Dummy(client=mock_client(handler), max_retries=3, sleep=sleeps.append)
    assert c.request_json("GET", "https://x.test/") == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_request_json_gives_up_on_persistent_failure() -> None:
    sleeps: list[float] = []
    c = _Dummy(client=mock_client(lambda r: httpx.Response(429)), max_retries=2, sleep=sleeps.append)
    with pytest.raises(ConnectorError, match=r"dummy: request failed \(HTTP 429\)"):
        c.request_json("GET", "https://x.test/")
    assert sleeps == [1.0, 2.0]


def test_request_json_no_retry_on_client_error() -> None:
    sleeps: list[float] = []
    c = _Dummy(client=mock_client(lambda r: httpx.Response(404)), max_retries=3, sleep=sleeps.append)
    with pytest.raises(ConnectorError, match="HTTP 404"):
        c.request_json("GET", "https://x.test/")
    assert sleeps == []


def test_request_json_transport_error_and_bad_json() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    sleeps: list[float] = []
    c = _Dummy(client=mock_client(boom), max_retries=1, sleep=sleeps.append)
    with pytest.raises(ConnectorError, match="ConnectError"):
        c.request_json("GET", "https://x.test/")
    assert sleeps == [1.0]
    c2 = _Dummy(client=mock_client(lambda r: httpx.Response(200, text="<html>")), max_retries=0)
    with pytest.raises(ConnectorError, match="invalid JSON"):
        c2.request_json("GET", "https://x.test/")


def test_backoff_is_capped() -> None:
    sleeps: list[float] = []
    c = _Dummy(client=mock_client(lambda r: httpx.Response(500)), max_retries=7, sleep=sleeps.append)
    with pytest.raises(ConnectorError):
        c.request_json("GET", "https://x.test/")
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_owned_client_lifecycle() -> None:
    c = _Dummy()
    first = c.client
    assert c.client is first
    assert first.headers["User-Agent"].startswith("Tenderbuddy/")
    c.close()
    assert c._client is None
    external = httpx.Client()
    c2 = _Dummy(client=external)
    c2.close()
    assert c2._client is external
    external.close()


def test_registry_and_labels(tmp_path: Path) -> None:
    s = make_settings(tmp_path, SAM_API_KEY="k", TED_COUNTRIES="irl")
    assert set(REGISTRY) == {"ted", "contracts_finder", "sam_gov", "csv"}
    ted = build_connector("ted", s)
    assert isinstance(ted, TedConnector)
    assert ted.countries == ["IRL"]
    assert isinstance(build_connector("contracts_finder", s), ContractsFinderConnector)
    sam = build_connector("sam_gov", s)
    assert isinstance(sam, SamGovConnector)
    assert sam.api_key == "k"
    csv_c = build_connector("csv", s)
    assert isinstance(csv_c, CsvConnector)
    assert csv_c.staging_dir == s.staging_dir
    with pytest.raises(ConnectorError, match="unknown connector"):
        build_connector("nope", s)
    assert LABELS["ted"] == "TED (EU)"


# ---------------------------------------------------------------- TED

def test_ted_query() -> None:
    q = build_query(SINCE, ["IRL", "MLT"], ["72", "48*"])
    assert q == ("notice-type IN (cn-standard cn-social cn-desg) AND publication-date>=20261005 AND "
                 "buyer-country IN (IRL MLT) AND classification-cpv IN (72* 48*)")
    assert build_query(SINCE, [], []) == "notice-type IN (cn-standard cn-social cn-desg) AND publication-date>=20261005"
    with pytest.raises(ConnectorError, match="invalid filter"):
        build_query(SINCE, ["IRL) OR (x"], [])


def test_ted_fixture_parsing() -> None:
    data = load_fixture("ted_search.json")
    recs = [parse_notice(n) for n in data["notices"]]
    assert all(r is not None for r in recs)
    by_id = {r.external_id: r for r in recs if r}
    malta = by_id["681657-2026"]
    assert malta.source == "ted"
    assert malta.country == "MLT"
    assert malta.region == "MT001"
    assert malta.deadline == datetime(2026, 11, 12, 7, 30)  # 09:30 +02:00
    assert malta.currency == "EUR"
    assert malta.estimated_value
    assert malta.estimated_value > 0
    assert malta.url == "https://ted.europa.eu/en/notice/-/detail/681657-2026"
    assert malta.published_at == datetime(2026, 10, 4, 22, 0)
    assert malta.title.startswith("CT2280/2026")
    restricted = by_id["681481-2026"]  # request-to-participate deadline only
    assert restricted.deadline == datetime(2026, 10, 31, 23, 59, 59)
    assert restricted.estimated_value is None


def test_ted_deadline_and_title_fallbacks() -> None:
    assert notice_deadline({}) is None
    assert notice_deadline({"deadline-receipt-tender-date-lot": ["2026-11-10+01:00", "2026-11-03+01:00"]}) == \
        datetime(2026, 11, 3, 22, 59, 59)
    assert notice_deadline({"deadline-receipt-tender-date-lot": ["bad"]}) is None
    assert notice_title({"notice-title": {"eng": "Ireland – Software – Payroll system"}}) == "Payroll system"
    assert notice_title({"notice-title": {"eng": "Plain title"}}) == "Plain title"
    assert parse_notice({"publication-number": "1-2026", "title-proc": {"eng": "T"}}) is None
    mixed = parse_notice({"publication-number": "2-2026", "title-proc": {"eng": "T"},
                          "deadline-receipt-tender-date-lot": ["2026-11-03Z"],
                          "estimated-value-lot": ["10", "20"], "estimated-value-cur-lot": ["EUR", "GBP"]})
    assert mixed is not None
    assert mixed.estimated_value is None
    assert mixed.currency == ""


def test_ted_fetch_paginates_and_counts() -> None:
    data = load_fixture("ted_search.json")
    notices = data["notices"]
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        page, limit = body["page"], body["limit"]
        chunk = notices[(page - 1) * limit : page * limit]
        if page == 1:
            chunk = [*chunk[:-1], {"publication-number": "bad-1"}]  # one unparseable notice
        return httpx.Response(200, json={"notices": chunk, "totalNoticeCount": len(notices)})

    conn = TedConnector(api_url="https://ted.test/search", client=mock_client(handler), max_retries=0)
    res = conn.fetch(SINCE, max_records=4)
    assert len(bodies) == 1
    assert bodies[0]["limit"] == 4
    assert bodies[0]["scope"] == "ACTIVE"
    assert len(res.records) == 3
    assert res.skipped == 1
    res2 = TedConnector(api_url="https://ted.test/search", client=mock_client(handler), max_retries=0).fetch(
        SINCE, max_records=200)
    assert len(res2.records) == len(notices) - 1
    assert bodies[-1]["page"] == 1


def test_ted_fetch_multiple_pages() -> None:
    notices = load_fixture("ted_search.json")["notices"][:5]
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        pages.append(body["page"])
        start = (body["page"] - 1) * 2
        return httpx.Response(200, json={"notices": notices[start : start + 2], "totalNoticeCount": 5})

    import tenderbuddy.connectors.ted as ted_mod

    old = ted_mod.PAGE_SIZE
    ted_mod.PAGE_SIZE = 2
    try:
        res = TedConnector(api_url="https://ted.test", client=mock_client(handler)).fetch(SINCE, 100)
    finally:
        ted_mod.PAGE_SIZE = old
    assert pages == [1, 2, 3]
    assert len(res.records) == 5


def test_ted_bad_shape() -> None:
    conn = TedConnector(api_url="https://ted.test", client=mock_client(lambda r: httpx.Response(200, json={"x": 1})))
    with pytest.raises(ConnectorError, match="unexpected response shape"):
        conn.fetch(SINCE, 10)


# ---------------------------------------------------------------- Contracts Finder

def test_contracts_finder_fixture_parsing() -> None:
    data = load_fixture("contracts_finder_search.json")
    recs = [parse_release(r) for r in data["releases"]]
    assert all(recs)
    first = recs[0]
    assert first is not None
    assert first.source == "contracts_finder"
    assert first.external_id.startswith("ocds-b5fd17-")
    assert first.title.startswith("CA18543")
    assert first.currency == "GBP"
    assert first.estimated_value == 250000
    assert first.country == "GBR"
    assert first.deadline == datetime(2026, 11, 4, 12, 0)
    assert first.cpv_codes[0] == "79340000"
    assert first.category == "Advertising and marketing services"
    assert first.authority == "Northampton College"
    assert first.url.startswith("https://www.contractsfinder.service.gov.uk/Notice/")
    assert any(r and r.region == "North West" for r in recs)


def test_contracts_finder_skips_inactive_and_incomplete() -> None:
    base = load_fixture("contracts_finder_search.json")["releases"][0]
    assert parse_release({**base, "tender": {**base["tender"], "status": "cancelled"}}) is None
    assert parse_release({**base, "tender": {**base["tender"], "tenderPeriod": {}}}) is None
    assert parse_release({**base, "ocid": ""}) is None
    minimal = parse_release({"ocid": "o-1", "tender": {"title": "T", "tenderPeriod": {"endDate": "2026-12-01"}}})
    assert minimal is not None
    assert (minimal.currency, minimal.region, minimal.url, minimal.ref_no) == ("GBP", "", "", "o-1")


def test_contracts_finder_follows_cursor() -> None:
    data = load_fixture("contracts_finder_search.json")
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "cursor=" in str(request.url):
            return httpx.Response(200, json={"releases": data["releases"][3:], "links": {}})
        return httpx.Response(200, json={"releases": data["releases"][:3],
                                         "links": {"next": "https://cf.test/search?cursor=abc"}})

    res = ContractsFinderConnector(api_url="https://cf.test/search", client=mock_client(handler)).fetch(SINCE, 100)
    assert len(res.records) == 5
    assert "publishedFrom=2026-10-05T00%3A00%3A00" in seen_urls[0]
    assert "stages=tender" in seen_urls[0]
    assert seen_urls[1] == "https://cf.test/search?cursor=abc"


def test_contracts_finder_respects_max_records() -> None:
    data = load_fixture("contracts_finder_search.json")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"releases": data["releases"], "links": {"next": "https://cf.test/n"}})

    res = ContractsFinderConnector(api_url="https://cf.test/s", client=mock_client(handler)).fetch(SINCE, 2)
    assert len(res.records) == 2
    assert len(calls) == 1
    assert "limit=2" in calls[0]


def test_contracts_finder_bad_shape() -> None:
    conn = ContractsFinderConnector(api_url="https://cf.test", client=mock_client(lambda r: httpx.Response(200, json=[])))
    with pytest.raises(ConnectorError, match="unexpected response shape"):
        conn.fetch(SINCE, 10)


# ---------------------------------------------------------------- SAM.gov

def test_sam_parsing() -> None:
    opps = load_fixture("sam_gov_search.json")["opportunitiesData"]
    rec = parse_opportunity(opps[0])
    assert rec is not None
    assert rec.source == "sam_gov"
    assert rec.ref_no == "47QTCA-26-R-0042"
    assert rec.deadline == datetime(2026, 11, 14, 22, 0)
    assert rec.authority == "GENERAL SERVICES ADMINISTRATION"
    assert rec.category == "NAICS 518210"
    assert (rec.country, rec.region, rec.currency) == ("USA", "DC", "USD")
    assert rec.published_at == datetime(2026, 10, 6, 0, 0)
    assert "Set-aside: Total Small Business Set-Aside (FAR 19.5)" in rec.description
    assert rec.url == "https://sam.gov/opp/9f0c1a2b3c4d5e6f7a8b9c0d1e2f3a4b/view"
    assert parse_opportunity(opps[1]) is None  # archived
    assert parse_opportunity(opps[2]) is None  # no deadline
    minimal = parse_opportunity({"noticeId": "n1", "title": "T", "responseDeadLine": "2026-12-01T00:00:00Z",
                                 "uiLink": "null", "solicitationNumber": "null"})
    assert minimal is not None
    assert (minimal.url, minimal.ref_no, minimal.country, minimal.category) == (
        "https://sam.gov/opp/n1/view", "n1", "USA", "")
    assert clean(" null ") == ""
    assert clean(None) == ""


def test_sam_requires_key() -> None:
    conn = SamGovConnector(api_url="https://sam.test", api_key="")
    with pytest.raises(ConnectorConfigError, match="SAM_API_KEY"):
        conn.check_config()
    with pytest.raises(ConnectorConfigError):
        conn.fetch(SINCE, 10)


def test_sam_fetch_params_and_window() -> None:
    data = load_fixture("sam_gov_search.json")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=data)

    conn = SamGovConnector(api_url="https://sam.test/search", api_key="KEY", client=mock_client(handler),
                           now=lambda: datetime(2026, 10, 11))
    res = conn.fetch(datetime(2020, 1, 1), 50)
    params = dict(seen[0].url.params)
    assert params["api_key"] == "KEY"
    assert params["postedTo"] == "10/11/2026"
    assert params["postedFrom"] == "10/12/2025"  # clamped to the API's one-year window
    assert params["ptype"] == "o,k"
    assert params["limit"] == "50"
    assert params["offset"] == "0"
    assert len(res.records) == 1
    assert res.skipped == 2


def test_sam_fetch_offsets() -> None:
    opp = load_fixture("sam_gov_search.json")["opportunitiesData"][0]
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        off = int(request.url.params["offset"])
        offsets.append(str(off))
        items = [dict(opp, noticeId=f"n{off + i}") for i in range(2)] if off < 4 else []
        return httpx.Response(200, json={"totalRecords": 4, "opportunitiesData": items})

    import tenderbuddy.connectors.sam_gov as sam_mod

    old = sam_mod.PAGE_SIZE
    sam_mod.PAGE_SIZE = 2
    try:
        res = SamGovConnector(api_url="https://sam.test", api_key="k", client=mock_client(handler)).fetch(SINCE, 10)
    finally:
        sam_mod.PAGE_SIZE = old
    assert offsets == ["0", "2"]
    assert [r.external_id for r in res.records] == ["n0", "n1", "n2", "n3"]


def test_sam_bad_shape() -> None:
    conn = SamGovConnector(api_url="https://s", api_key="k", client=mock_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ConnectorError, match="unexpected response shape"):
        conn.fetch(SINCE, 1)


# ---------------------------------------------------------------- CSV

V1_CSV = (
    "title,deadline,ref_no,authority,category,cpv_codes,country,region,currency,estimated_value,bid_security,"
    "min_turnover,min_experience_years,certs_required,published_date,description,url,source\n"
    "Road resurfacing,2026-12-01,RD-1,City Hall,Works,45233000|45233100,PHL,Camarines Sur,php,5000000,100000,"
    "20000000,3,PCAB License;ISO 9001,2026-10-01,Resurface 4km,https://example.org/rd1,PhilGEPS\n"
    "Missing deadline,,RD-2,,,,,,,,,,,,,,,\n"
    ",2026-12-01,RD-3,,,,,,,,,,,,,,,\n"
)
LEGACY_CSV = (
    "title,ref_no,authority,category,state,est_value_lakh,emd_lakh,deadline,published_date,turnover_req_cr,"
    "exp_req_years,certs_req,description,source\n"
    "Printers,GEM/1,MoF,Supply,Maharashtra,22.0,0.44,2026-10-10,2026-09-20,1.5,2,GeM Registered,Desc,GeM\n"
    "Free EMD,GEM/2,MoF,Supply,Delhi,10,0,10/12/2026,,,,,,\n"
)


def test_csv_v1_rows() -> None:
    res = parse_csv_bytes(V1_CSV.encode(), "upload")
    assert res.skipped == 2
    assert res.errors == ["row 3: missing or unparseable deadline ''", "row 4: missing title"]
    rec = res.records[0]
    assert rec.source == "philgeps"
    assert (rec.external_id, rec.ref_no, rec.currency, rec.country, rec.region) == (
        "RD-1", "RD-1", "PHP", "PHL", "Camarines Sur")
    assert rec.cpv_codes == ["45233000", "45233100"]
    assert rec.certs_required == ["PCAB License", "ISO 9001"]
    assert (rec.estimated_value, rec.bid_security, rec.min_turnover, rec.min_experience_years) == (
        5_000_000, 100_000, 20_000_000, 3)
    assert rec.deadline == datetime(2026, 12, 1)
    assert rec.published_at == datetime(2026, 10, 1)
    assert rec.url == "https://example.org/rd1"


def test_csv_legacy_rows_convert_to_inr() -> None:
    res = parse_csv_bytes(LEGACY_CSV.encode("utf-8-sig"), "gem")
    a, b = res.records
    assert (a.currency, a.country, a.region) == ("INR", "IND", "Maharashtra")
    assert a.estimated_value == pytest.approx(2_200_000)
    assert a.bid_security == pytest.approx(44_000)
    assert a.min_turnover == pytest.approx(15_000_000)
    assert a.min_experience_years == 2
    assert a.certs_required == ["GeM Registered"]
    assert a.source == "gem"
    assert b.bid_security is None
    assert b.min_turnover is None
    assert b.min_experience_years is None
    assert b.deadline == datetime(2026, 12, 10)
    assert b.source == "gem"


def test_csv_required_columns_and_size() -> None:
    with pytest.raises(CsvFormatError, match="deadline, title"):
        parse_csv_bytes(b"name,date\nx,y\n", "x")
    import tenderbuddy.connectors.csv_import as csv_mod

    with pytest.raises(CsvFormatError, match="10 MB"):
        parse_csv_bytes(b"x" * (csv_mod.MAX_CSV_BYTES + 1), "x")


def test_csv_helpers() -> None:
    assert slug(" CPP Portal! ") == "cpp_portal"
    assert slug("***") == "csv"
    assert split_list(" a | b ;c||") == ["a", "b", "c"]
    assert split_list(None) == []
    assert parse_csv_date("2026-10-01T09:30") == datetime(2026, 10, 1, 9, 30)
    assert parse_csv_date("Oct 5, 2026") == datetime(2026, 10, 5)
    assert parse_csv_date("tomorrow") is None
    assert parse_csv_date("  ") is None
    rec = row_to_record({"Title": "No ref", "Deadline": "2026-11-01", None: "extra"}, "Upload")
    assert rec.external_id == "No ref|2026-11-01"
    assert rec.source == "upload"


def test_csv_connector_reads_staging(tmp_path: Path) -> None:
    (tmp_path / "philgeps").mkdir()
    (tmp_path / "philgeps" / "a.csv").write_text(V1_CSV.replace("PhilGEPS", ""), encoding="utf-8")
    (tmp_path / "root.csv").write_text(LEGACY_CSV, encoding="utf-8")
    (tmp_path / "broken.csv").write_text("foo,bar\n1,2\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    conn = CsvConnector(staging_dir=tmp_path)
    assert [p.name for p in conn.staged_files()] == ["broken.csv", "a.csv", "root.csv"]
    res = conn.fetch(SINCE, 100)
    assert {r.source for r in res.records} == {"philgeps", "gem", "csv"}  # root files default to "csv"
    assert any(e.startswith("broken.csv: CSV is missing") for e in res.errors)
    assert any(e.startswith("philgeps/a.csv: row 3") for e in res.errors)
    assert res.skipped == 2
    assert len(conn.fetch(SINCE, 1).records) == 1
    assert CsvConnector(staging_dir=tmp_path / "missing").fetch(SINCE, 10).records == []


def test_settings_used_by_registry(settings: Settings) -> None:
    assert settings.staging_dir == settings.DATA_DIR / "staging"
