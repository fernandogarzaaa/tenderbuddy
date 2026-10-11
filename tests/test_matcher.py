from __future__ import annotations

import pytest

from tenderbuddy.matcher import (
    WEIGHTS,
    Criterion,
    certs_match,
    fmt_amount,
    match_score,
    norm_list,
    proportional,
    tokens,
)

PROFILE = {
    "currency": "EUR",
    "annual_turnover": 2_000_000.0,
    "years_experience": 8,
    "certifications": ["ISO 9001", "ISO 27001:2022"],
    "categories": ["IT Services"],
    "cpv_prefixes": ["72", "48"],
    "keywords": ["data centre"],
    "regions": ["IRL", "Leinster"],
}
TENDER = {
    "currency": "EUR",
    "min_turnover": 1_000_000.0,
    "min_experience_years": 5,
    "certs_required": ["ISO 9001:2015"],
    "category": "IT Services",
    "cpv_codes": ["72200000"],
    "country": "IRL",
    "region": "",
    "title": "Software maintenance",
    "description": "",
}


def crit(result, key: str) -> Criterion:
    return next(c for c in result.breakdown if c.key == key)


def test_weights_sum_to_100() -> None:
    assert sum(WEIGHTS.values()) == 100


def test_full_match_scores_100() -> None:
    r = match_score(PROFILE, TENDER)
    assert r.score == 100
    assert [c.key for c in r.breakdown] == ["turnover", "experience", "certifications", "category", "location"]
    assert all(c.met for c in r.breakdown)
    assert [c.weight for c in r.breakdown] == [30, 20, 25, 15, 10]


def test_empty_tender_is_fully_met() -> None:
    r = match_score({}, {})
    assert r.score == 100
    assert {c.required for c in r.breakdown} == {"No minimum", "None required", "Any"}


def test_as_dict_shape() -> None:
    d = match_score(PROFILE, TENDER).as_dict()
    assert d["score"] == 100
    first = d["breakdown"][0]
    assert first == {"key": "turnover", "label": "Annual turnover", "required": ">= EUR 1,000,000",
                     "actual": "EUR 2,000,000", "status": "met", "met": True, "weight": 30, "points": 30}


# ---------------------------------------------------------------- turnover

def test_turnover_partial_credit_rounds_down() -> None:
    r = match_score(dict(PROFILE, annual_turnover=500_000), TENDER)
    c = crit(r, "turnover")
    assert (c.status, c.points) == ("partial", 15)
    assert r.score == 85


def test_turnover_exactly_meets_requirement() -> None:
    c = crit(match_score(dict(PROFILE, annual_turnover=1_000_000), TENDER), "turnover")
    assert (c.status, c.points) == ("met", 30)


def test_turnover_tiny_is_unmet() -> None:
    c = crit(match_score(dict(PROFILE, annual_turnover=1000), TENDER), "turnover")
    assert (c.status, c.points) == ("unmet", 0)


def test_turnover_currency_mismatch_is_unknown_half_points() -> None:
    c = crit(match_score(PROFILE, dict(TENDER, currency="GBP")), "turnover")
    assert c.status == "unknown"
    assert c.points == 15
    assert "currency differs" in c.actual
    assert c.required == ">= GBP 1,000,000"


def test_turnover_missing_tender_currency_uses_profile_currency() -> None:
    c = crit(match_score(PROFILE, dict(TENDER, currency="")), "turnover")
    assert c.status == "met"
    assert c.required == ">= EUR 1,000,000"


def test_turnover_zero_requirement_is_no_minimum() -> None:
    c = crit(match_score(PROFILE, dict(TENDER, min_turnover=0)), "turnover")
    assert (c.required, c.points, c.status) == ("No minimum", 30, "met")


# ---------------------------------------------------------------- experience

def test_experience_partial() -> None:
    c = crit(match_score(dict(PROFILE, years_experience=2), dict(TENDER, min_experience_years=8)), "experience")
    assert (c.status, c.points, c.required, c.actual) == ("partial", 5, ">= 8 yrs", "2 yrs")


def test_experience_none_is_unmet() -> None:
    c = crit(match_score(dict(PROFILE, years_experience=0), TENDER), "experience")
    assert (c.status, c.points) == ("unmet", 0)


def test_experience_no_requirement() -> None:
    c = crit(match_score(PROFILE, dict(TENDER, min_experience_years=None)), "experience")
    assert (c.status, c.points, c.required) == ("met", 20, "No minimum")


# ---------------------------------------------------------------- certifications

def test_certs_fraction_and_missing_list() -> None:
    t = dict(TENDER, certs_required=["ISO 9001:2015", "ISO 14001", "Cyber Essentials"])
    c = crit(match_score(PROFILE, t), "certifications")
    assert c.status == "partial"
    assert c.points == 8  # round(25 * 1/3)
    assert c.actual == "1/3 held (missing: ISO 14001, Cyber Essentials)"


def test_certs_none_held_is_unmet() -> None:
    c = crit(match_score(dict(PROFILE, certifications=[]), TENDER), "certifications")
    assert (c.status, c.points) == ("unmet", 0)


def test_certs_all_held_is_met() -> None:
    c = crit(match_score(PROFILE, dict(TENDER, certs_required=["iso-27001", "ISO 9001"])), "certifications")
    assert (c.status, c.points, c.actual) == ("met", 25, "2/2 held")


def test_certs_blank_entries_ignored() -> None:
    c = crit(match_score(PROFILE, dict(TENDER, certs_required=["  ", ""])), "certifications")
    assert c.required == "None required"
    assert c.actual == "2 on file"


@pytest.mark.parametrize(
    ("held", "required", "expected"),
    [
        ("ISO 9001", "ISO 9001:2015", True),
        ("ISO 9001:2015", "iso 9001", True),
        ("ISO 9001", "ISO 14001", False),
        ("9001", "ISO 9001", False),
        ("", "ISO 9001", False),
        ("ISO 9001", "", False),
    ],
)
def test_certs_match(held: str, required: str, expected: bool) -> None:
    assert certs_match(held, required) is expected


# ---------------------------------------------------------------- category

def test_category_by_name() -> None:
    c = crit(match_score(dict(PROFILE, cpv_prefixes=[], keywords=[]), dict(TENDER, cpv_codes=[])), "category")
    assert c.status == "met"
    assert c.actual == "matched category 'IT Services'"


def test_category_by_cpv_prefix() -> None:
    p = dict(PROFILE, categories=[], keywords=[])
    c = crit(match_score(p, dict(TENDER, category="")), "category")
    assert c.actual == "matched CPV 72200000"
    assert c.required == "72200000"


def test_category_by_keyword_requires_all_words() -> None:
    p = dict(PROFILE, categories=[], cpv_prefixes=[])
    t = dict(TENDER, category="Works", cpv_codes=[], title="New data centre cooling", description="")
    c = crit(match_score(p, t), "category")
    assert c.actual == "matched keyword 'data centre'"
    t2 = dict(t, title="New data hall")
    assert crit(match_score(p, t2), "category").status == "unmet"


def test_category_unmet() -> None:
    c = crit(match_score(PROFILE, dict(TENDER, category="Catering", cpv_codes=["55500000"])), "category")
    assert (c.status, c.points, c.actual) == ("unmet", 0, "no category, CPV or keyword match")


def test_category_multiple_reasons_joined() -> None:
    c = crit(match_score(PROFILE, TENDER), "category")
    assert c.actual == "matched category 'IT Services'; CPV 72200000"


# ---------------------------------------------------------------- location

def test_location_by_country() -> None:
    c = crit(match_score(PROFILE, TENDER), "location")
    assert (c.status, c.required, c.actual) == ("met", "IRL", "IRL, Leinster")


def test_location_by_region_case_insensitive() -> None:
    c = crit(match_score(PROFILE, dict(TENDER, country="XXX", region="leinster")), "location")
    assert c.status == "met"
    assert c.required == "leinster, XXX"


def test_location_wildcard() -> None:
    c = crit(match_score(dict(PROFILE, regions=["All India"]), dict(TENDER, country="IND", region="Goa")),
             "location")
    assert (c.status, c.points) == ("met", 10)


def test_location_unmet_and_none_set() -> None:
    c = crit(match_score(dict(PROFILE, regions=[]), dict(TENDER, country="FRA")), "location")
    assert (c.status, c.points, c.actual) == ("unmet", 0, "None set")


# ---------------------------------------------------------------- helpers

def test_score_clamped_and_summed() -> None:
    r = match_score(dict(PROFILE, regions=[]), dict(TENDER, country="FRA", category="Food", cpv_codes=["1"]))
    assert r.score == 75


def test_tokens_and_norm_list() -> None:
    assert tokens("ISO-9001:2015 Cert") == ["iso", "9001", "2015", "cert"]
    assert norm_list(None) == []
    assert norm_list("  Foo ") == ["foo"]
    assert norm_list(["A", " ", "b "]) == ["a", "b"]


def test_fmt_amount() -> None:
    assert fmt_amount(None, "EUR") == "n/a"
    assert fmt_amount(1234567.4, "EUR") == "EUR 1,234,567"
    assert fmt_amount(10, "") == "10"


def test_proportional_edges() -> None:
    assert proportional(5, None, 20) == (20, "met")
    assert proportional(5, 0, 20) == (20, "met")
    assert proportional(10, 10, 20) == (20, "met")
    assert proportional(9.99, 10, 20) == (19, "partial")
    assert proportional(-5, 10, 20) == (0, "unmet")
