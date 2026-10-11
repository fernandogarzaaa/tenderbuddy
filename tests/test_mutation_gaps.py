"""Boundary tests added to kill surviving mutants found by testteeth (v1.0.0)."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

from tenderbuddy import rfp
from tenderbuddy.matcher import match_score, proportional
from tenderbuddy.rfp import (
    MAX_AMOUNTS,
    MAX_DATES,
    MAX_REQUIREMENTS,
    detect_risks,
    extract_amounts,
    extract_dates,
    extract_pdf_text,
    extract_requirements,
)
from tests.conftest import make_pdf

# --- matcher -----------------------------------------------------------------


def by_key(result: object, key: str):  # type: ignore[no-untyped-def]
    return next(c for c in result.breakdown if c.key == key)  # type: ignore[attr-defined]


def test_proportional_required_of_one_is_a_real_threshold() -> None:
    assert proportional(0.5, 1.0, 20) == (10, "partial")


def test_proportional_single_point_is_partial() -> None:
    assert proportional(1.0, 20.0, 20) == (1, "partial")


def test_turnover_missing_on_profile_counts_as_zero() -> None:
    c = by_key(match_score({"currency": "EUR"}, {"currency": "EUR", "min_turnover": 2}), "turnover")
    assert (c.status, c.points) == ("unmet", 0)
    assert c.actual == "EUR 0"


def test_turnover_minimum_of_one_is_enforced() -> None:
    c = by_key(match_score({"currency": "EUR", "annual_turnover": 0}, {"currency": "EUR", "min_turnover": 1}),
               "turnover")
    assert (c.status, c.points, c.required) == ("unmet", 0, ">= EUR 1")


def test_experience_zero_minimum_reads_no_minimum() -> None:
    c = by_key(match_score({"years_experience": 3}, {"min_experience_years": 0}), "experience")
    assert (c.required, c.status, c.points) == ("No minimum", "met", 20)


def test_category_required_lists_first_three_cpvs_only() -> None:
    c = by_key(match_score({}, {"cpv_codes": ["11", "22", "33", "44"]}), "category")
    assert c.required == "11, 22, 33"
    assert c.status == "unmet"


def test_keyword_equal_to_whole_text_matches() -> None:
    c = by_key(match_score({"keywords": ["software"]}, {"category": "X", "title": "Software", "description": ""}),
               "category")
    assert c.status == "met"
    assert "keyword 'software'" in c.actual


def test_score_floor_is_zero_when_everything_fails() -> None:
    profile = {"currency": "EUR", "annual_turnover": 0, "years_experience": 0, "regions": ["FRA"]}
    tender = {"currency": "EUR", "min_turnover": 1000, "min_experience_years": 5, "certs_required": ["ISO 9001"],
              "category": "Works", "country": "IRL"}
    assert match_score(profile, tender).score == 0


# --- rfp ---------------------------------------------------------------------


def test_requirement_length_bounds() -> None:
    ten = "1. abcdefg"  # exactly 10 chars after strip
    nine = "1. abcdef"
    long_ok = "- " + "x" * 498  # 500 chars
    too_long = "- " + "x" * 499  # 501 chars
    assert len(ten) == 10
    assert len(long_ok) == 500
    assert len(too_long) == 501
    out = extract_requirements([ten, nine, long_ok, too_long])
    assert out == ["abcdefg", "x" * 498]


def test_requirement_needs_bullet_or_modal_not_both() -> None:
    out = extract_requirements(["- Bids via the portal only please", "Bidders shall be registered locally"])
    assert out == ["Bids via the portal only please", "Bidders shall be registered locally"]


def test_requirement_cap() -> None:
    lines = [f"- requirement number {i} here" for i in range(MAX_REQUIREMENTS + 10)]
    assert len(extract_requirements(lines)) == MAX_REQUIREMENTS == 40


def test_dates_stop_after_cap_is_reached() -> None:
    lines = [f"Item on 2026-01-{d:02d}" for d in range(1, MAX_DATES + 1)]
    lines.append("Last date for bid submission: 2026-12-01")
    dates, _published, due = extract_dates(lines)
    assert len(dates) == MAX_DATES == 20
    assert due is None  # scanning stopped once the cap was hit


def test_amounts_cap() -> None:
    text = " ".join(f"Rs {i},000" for i in range(1, MAX_AMOUNTS + 10))
    assert len(extract_amounts(text)) == MAX_AMOUNTS == 20


def test_adjacent_amounts_are_not_treated_as_overlapping() -> None:
    assert extract_amounts("Fee 100 EUR€5 and Rs 500Rs 600") == ["100 EUR", "€5", "Rs 500", "Rs 600"]


def test_same_day_publish_and_due_is_short_timeline() -> None:
    d = datetime(2026, 10, 1)
    risks = detect_risks("", d, d)
    assert [r["type"] for r in risks] == ["Short timeline"]
    assert "Only 0 days" in risks[0]["detail"]


def test_pdf_page_cap_default() -> None:
    assert rfp.MAX_PDF_PAGES == 300
    assert extract_pdf_text.__defaults__ == (300,)


def test_encrypted_pdf_with_empty_password_is_read(tmp_path: Path) -> None:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(make_pdf(["Encrypted tender text"])))
    writer = pypdf.PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password="", owner_password="owner-secret", algorithm="RC4-128")
    path = tmp_path / "enc.pdf"
    with path.open("wb") as fh:
        writer.write(fh)
    assert "Encrypted tender text" in extract_pdf_text(path)


# --- connectors --------------------------------------------------------------

import json  # noqa: E402
import logging  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from tenderbuddy.connectors import csv_import  # noqa: E402
from tenderbuddy.connectors import ted as ted_mod  # noqa: E402
from tenderbuddy.connectors.base import Connector, FetchResult, parse_iso_datetime  # noqa: E402
from tenderbuddy.connectors.contracts_finder import parse_release  # noqa: E402
from tenderbuddy.connectors.csv_import import CsvConnector, CsvFormatError, parse_csv_bytes  # noqa: E402
from tenderbuddy.connectors.ted import TedConnector, notice_deadline, parse_notice  # noqa: E402
from tests.conftest import load_fixture, mock_client  # noqa: E402

SINCE = datetime(2026, 10, 5)


class _Dummy(Connector):
    name = "dummy"

    def fetch(self, since: datetime, max_records: int) -> FetchResult:
        return FetchResult()


def test_connector_defaults_and_owned_client() -> None:
    conn = _Dummy()
    assert conn.max_retries == 3
    assert conn.client.follow_redirects is True
    conn.close()


def test_retry_log_reports_one_based_attempt(caplog: pytest.LogCaptureFixture) -> None:
    calls = iter([httpx.Response(503), httpx.Response(200, json={"ok": True})])
    conn = _Dummy(client=mock_client(lambda r: next(calls)), sleep=lambda _s: None)
    with caplog.at_level(logging.WARNING, logger="tenderbuddy.connectors.base"):
        assert conn.request_json("GET", "https://x.test") == {"ok": True}
    assert [getattr(r, "attempt", None) for r in caplog.records] == [1]


def test_two_digit_positive_offset() -> None:
    assert parse_iso_datetime("2026-11-06+10:00") == datetime(2026, 11, 6, 13, 59, 59)


def test_invalid_full_datetime_is_none() -> None:
    assert parse_iso_datetime("2026-11-06T25:00:00") is None


def test_contracts_finder_keeps_description() -> None:
    rec = parse_release({"ocid": "ocds-1", "tender": {"title": "Roads", "description": "Resurfacing works",
                                                      "tenderPeriod": {"endDate": "2026-12-01T12:00:00Z"}}})
    assert rec is not None
    assert rec.description == "Resurfacing works"


def test_csv_size_limit_boundary() -> None:
    assert csv_import.MAX_CSV_BYTES == 10 * 1024 * 1024
    head = b"title,deadline\n"
    row = b"Road works,2026-12-01\n"
    body = row * ((csv_import.MAX_CSV_BYTES - len(head)) // len(row))
    exact = head + body + b"#" * (csv_import.MAX_CSV_BYTES - len(head) - len(body))
    assert len(exact) == csv_import.MAX_CSV_BYTES
    assert len(parse_csv_bytes(exact, "csv").records) >= 1  # exactly at the limit is accepted
    with pytest.raises(CsvFormatError, match="larger than 10 MB"):
        parse_csv_bytes(exact + b"x", "csv")


def test_csv_oversized_field_is_a_format_error() -> None:
    with pytest.raises(CsvFormatError, match="malformed CSV"):
        parse_csv_bytes(b"title,deadline\n" + b"x" * 200_000 + b",2026-12-01\n", "csv")


def test_csv_connector_stops_reading_files_at_cap(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("title,deadline\nRoad works,2026-12-01\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("foo,bar\n1,2\n", encoding="utf-8")
    conn = CsvConnector(staging_dir=tmp_path)
    assert [p.name for p in conn.staged_files()] == ["a.csv", "b.csv"]
    res = conn.fetch(SINCE, 1)
    assert len(res.records) == 1
    assert res.errors == []  # b.csv is never read once the cap is reached


def test_ted_deadline_combines_exact_ten_char_date_with_time() -> None:
    notice = {"deadline-receipt-tender-date-lot": ["2026-11-06"],
              "deadline-receipt-tender-time-lot": ["12:00:00+01:00"]}
    assert notice_deadline(notice) == datetime(2026, 11, 6, 11, 0)


def test_ted_three_letter_place_is_not_a_region() -> None:
    notice = {"publication-number": "1-2026", "title-proc": {"eng": "Works"},
              "deadline-receipt-tender-date-lot": ["2026-11-06Z"], "place-of-performance": ["IRL", "IE061"]}
    rec = parse_notice(notice)
    assert rec is not None
    assert rec.region == "IE061"


def test_ted_unparseable_notice_is_recorded_as_error() -> None:
    bad = {"publication-number": "bad-2", "title-proc": {"eng": "X"},
           "deadline-receipt-tender-date-lot": ["2026-11-06Z"], "estimated-value-lot": 5}
    conn = TedConnector(api_url="https://ted.test", client=mock_client(
        lambda r: httpx.Response(200, json={"notices": [bad], "totalNoticeCount": 1})))
    res = conn.fetch(SINCE, 10)
    assert res.records == []
    assert res.skipped == 1
    assert len(res.errors) == 1
    assert res.errors[0].startswith("bad-2: ")


def test_ted_second_page_respects_remaining_quota_and_total(monkeypatch: pytest.MonkeyPatch) -> None:
    notices = load_fixture("ted_search.json")["notices"][:4]
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        pages.append(body["page"])
        return httpx.Response(200, json={"notices": notices[:2] if body["page"] % 2 else notices[2:4],
                                         "totalNoticeCount": 4})

    monkeypatch.setattr(ted_mod, "PAGE_SIZE", 2)
    res = TedConnector(api_url="https://ted.test", client=mock_client(handler)).fetch(SINCE, 3)
    assert len(res.records) + res.skipped == 3
    assert pages == [1, 2]
    pages.clear()
    res = TedConnector(api_url="https://ted.test", client=mock_client(handler)).fetch(SINCE, 100)
    assert pages == [1, 2]  # stops when page * page_size reaches the total
