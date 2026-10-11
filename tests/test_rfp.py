from __future__ import annotations

from datetime import datetime
from pathlib import Path

from tenderbuddy.rfp import (
    MAX_REQUIREMENTS,
    analyze_rfp,
    detect_risks,
    extract_amounts,
    extract_dates,
    extract_pdf_text,
    extract_requirements,
    label_for,
    parse_date,
)
from tests.conftest import make_pdf

RFP = """NOTICE INVITING TENDER
Published on 01/10/2026. Last date for bid submission: 20/10/2026.
Bid opening on 21 October 2026.
Estimated value: Rs 95 lakh. Earnest money deposit of Rs 1.9 lakh. Budget EUR 1,200,000.

1. The bidder shall have minimum 5 years of experience in similar works.
2. The bidder must submit OEM authorization letters for all equipment.
- Bids must be submitted online through the portal only.
short line

Liquidated damages of 0.5% per week shall apply for delays.
A bank guarantee of 10% is required as performance security.
Corrigendum 2 has been issued extending the submission deadline.
"""


def test_requirements_extracted_and_cleaned() -> None:
    reqs = analyze_rfp(RFP)["requirements"]
    assert "The bidder shall have minimum 5 years of experience in similar works." in reqs
    assert "The bidder must submit OEM authorization letters for all equipment." in reqs
    assert "Bids must be submitted online through the portal only." in reqs
    assert all(not r.startswith(("1.", "2.", "-")) for r in reqs)
    assert "short line" not in reqs


def test_requirements_dedupe_length_and_cap() -> None:
    lines = [f"{i}. Item number {i} shall be provided." for i in range(1, 60)]
    lines += ["1. Item number 1 shall be provided.", "x" * 600, "must"]
    reqs = extract_requirements(lines)
    assert len(reqs) == MAX_REQUIREMENTS
    assert len(set(reqs)) == len(reqs)


def test_requirements_collapse_whitespace() -> None:
    assert extract_requirements(["a)   The   supplier   must   comply"]) == ["The supplier must comply"]


def test_dates_labelled() -> None:
    dates = analyze_rfp(RFP)["dates"]
    assert {"label": "published", "date": "01/10/2026"} in dates
    assert {"label": "bid due", "date": "20/10/2026"} in dates
    assert {"label": "bid opening", "date": "21 October 2026"} in dates


def test_dates_iso_and_month_first_and_dedupe() -> None:
    dates, pub, due = extract_dates([
        "Clarification queries by 2026-10-05; deadline October 30, 2026",
        "deadline October 30, 2026 again",
        "Not a date 99/99/2026",
    ])
    assert dates == [{"label": "clarification", "date": "2026-10-05"},
                     {"label": "bid due", "date": "October 30, 2026"}]
    assert pub is None
    assert due == datetime(2026, 10, 30)


def test_dates_mentioned_label_default() -> None:
    dates, _, _ = extract_dates(["Something on 05/11/2026"])
    assert dates == [{"label": "mentioned", "date": "05/11/2026"}]


def test_dates_first_published_and_due_win() -> None:
    _, pub, due = extract_dates(["published 01/01/2026 deadline 10/01/2026", "published 02/01/2026 deadline 11/01/2026"])
    assert pub == datetime(2026, 1, 1)
    assert due == datetime(2026, 1, 10)


def test_amounts_multi_currency_and_order() -> None:
    amounts = extract_amounts(RFP)
    assert amounts[:3] == ["Rs 95 lakh", "Rs 1.9 lakh", "EUR 1,200,000"]
    assert extract_amounts("Price 250,000 GBP and PHP 5 million, also 3 crores.") == [
        "250,000 GBP", "PHP 5 million", "3 crores"]
    assert extract_amounts("€ 10 and € 10") == ["€ 10"]


def test_risks_detected() -> None:
    types = [r["type"] for r in analyze_rfp(RFP)["risks"]]
    assert types == ["Penalty clause", "Corrigendum issued", "Security deposit"]


def test_risk_short_timeline_and_boundaries() -> None:
    risks = detect_risks("", datetime(2026, 10, 1), datetime(2026, 10, 8))
    assert risks == [{"type": "Short timeline", "detail": "Only 7 days between published date and bid due date."}]
    assert detect_risks("", datetime(2026, 10, 1), datetime(2026, 10, 15)) == []
    assert detect_risks("", datetime(2026, 10, 15), datetime(2026, 10, 1)) == []
    assert detect_risks("", datetime(2026, 10, 1), datetime(2026, 10, 14))[0]["detail"].startswith("Only 13 days")
    assert detect_risks("", None, datetime(2026, 10, 2)) == []


def test_other_risks() -> None:
    types = [r["type"] for r in detect_risks("Bidders may be debarred. Unlimited liability applies. "
                                             "A performance bond. Penalties apply. See addendum.", None, None)]
    assert types == ["Penalty clause", "Corrigendum issued", "Security deposit", "Blacklisting clause",
                     "Unlimited liability"]


def test_empty_text() -> None:
    assert analyze_rfp(None) == {"requirements": [], "dates": [], "amounts": [], "risks": []}


def test_parse_date_and_label() -> None:
    assert parse_date("5 Nov 2026,") == datetime(2026, 11, 5)
    assert parse_date("31/02/2026") is None
    assert label_for("Last date for submission") == "bid due"
    assert label_for("nothing here") is None


def test_pdf_extraction_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "a.pdf"
    p.write_bytes(make_pdf(["Hello tender world", "The bidder shall comply."]))
    text = extract_pdf_text(p)
    assert "Hello tender world" in text
    assert "shall comply" in text
    assert extract_pdf_text(p, max_pages=0) == ""


def test_pdf_extraction_garbage_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"%PDF-1.4 not really a pdf")
    assert extract_pdf_text(p) == ""
