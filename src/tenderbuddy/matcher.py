"""Pure eligibility matcher: company profile vs. tender criteria.

``match_score(profile, tender) -> MatchResult`` with a 0-100 score and a
per-criterion breakdown. Both inputs are plain mappings so the matcher is
testable without a database::

    profile = {"currency": "EUR", "annual_turnover": 2_500_000, "years_experience": 8,
               "certifications": ["ISO 9001"], "categories": ["IT Services"],
               "cpv_prefixes": ["72"], "keywords": ["software"], "regions": ["IRL"]}
    tender  = {"currency": "EUR", "min_turnover": 1_000_000, "min_experience_years": 5,
               "certs_required": ["ISO 9001:2015"], "category": "IT Services",
               "cpv_codes": ["72200000"], "country": "IRL", "region": "",
               "title": "Software maintenance", "description": "..."}

Rules
-----
* A missing/empty requirement is met (there is nothing to fail).
* Turnover and experience earn partial credit proportional to how close the
  profile gets to the requirement (rounded down).
* A turnover requirement in a different currency than the profile cannot be
  verified: it is reported as ``unknown`` and earns half the weight.
* Certifications earn the fraction of required certificates held. Names are
  normalised (case, punctuation) and a held certificate matches a required one
  when one token sequence is a prefix of the other ("ISO 9001" ~ "ISO 9001:2015").
* Category is met by an exact category name, a CPV prefix, or a profile keyword
  whose words all appear in the title/description.
* Location is met by the tender's region or country appearing in the profile's
  regions, or by a wildcard region ("all", "any", "worldwide", "global", "all india").
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["met", "partial", "unmet", "unknown"]

WEIGHTS: dict[str, int] = {"turnover": 30, "experience": 20, "certifications": 25, "category": 15, "location": 10}
WILDCARD_REGIONS = frozenset({"all", "any", "worldwide", "global", "all india"})
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str
    required: str
    actual: str
    status: Status
    weight: int
    points: int

    @property
    def met(self) -> bool:
        return self.status == "met"


@dataclass(frozen=True)
class MatchResult:
    score: int
    breakdown: list[Criterion] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "breakdown": [
                {"key": c.key, "label": c.label, "required": c.required, "actual": c.actual,
                 "status": c.status, "met": c.met, "weight": c.weight, "points": c.points}
                for c in self.breakdown
            ],
        }


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def norm_list(values: Iterable[Any] | str | None) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = [values]
    out = []
    for v in values:
        s = str(v).strip().lower()
        if s:
            out.append(s)
    return out


def certs_match(held: str, required: str) -> bool:
    a, b = tokens(held), tokens(required)
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return long_[: len(short)] == short


def fmt_amount(value: float | None, currency: str) -> str:
    if value is None:
        return "n/a"
    return f"{currency} {value:,.0f}".strip()


def proportional(actual: float, required: float | None, weight: int) -> tuple[int, Status]:
    """Points for a numeric threshold requirement."""
    if required is None or required <= 0:
        return weight, "met"
    if actual >= required:
        return weight, "met"
    points = int(weight * max(0.0, actual) / required)
    return points, ("partial" if points > 0 else "unmet")


def _turnover(profile: Mapping[str, Any], tender: Mapping[str, Any]) -> Criterion:
    weight = WEIGHTS["turnover"]
    req = tender.get("min_turnover")
    p_cur = str(profile.get("currency") or "").upper()
    t_cur = str(tender.get("currency") or p_cur).upper()
    actual = float(profile.get("annual_turnover") or 0.0)
    actual_s = fmt_amount(actual, p_cur)
    if req is None or float(req) <= 0:
        return Criterion("turnover", "Annual turnover", "No minimum", actual_s, "met", weight, weight)
    req_f = float(req)
    req_s = ">= " + fmt_amount(req_f, t_cur)
    if p_cur and t_cur and p_cur != t_cur:
        return Criterion("turnover", "Annual turnover", req_s, actual_s + " (currency differs)",
                         "unknown", weight, weight // 2)
    points, status = proportional(actual, req_f, weight)
    return Criterion("turnover", "Annual turnover", req_s, actual_s, status, weight, points)


def _experience(profile: Mapping[str, Any], tender: Mapping[str, Any]) -> Criterion:
    weight = WEIGHTS["experience"]
    req = tender.get("min_experience_years")
    actual = int(profile.get("years_experience") or 0)
    if req is None or int(req) <= 0:
        return Criterion("experience", "Years of experience", "No minimum", f"{actual} yrs", "met", weight, weight)
    points, status = proportional(float(actual), float(req), weight)
    return Criterion("experience", "Years of experience", f">= {int(req)} yrs", f"{actual} yrs", status, weight,
                     points)


def _certifications(profile: Mapping[str, Any], tender: Mapping[str, Any]) -> Criterion:
    weight = WEIGHTS["certifications"]
    required = [str(c).strip() for c in (tender.get("certs_required") or []) if str(c).strip()]
    held = [str(c) for c in (profile.get("certifications") or [])]
    if not required:
        return Criterion("certifications", "Certifications", "None required", f"{len(held)} on file", "met",
                         weight, weight)
    matched = [r for r in required if any(certs_match(h, r) for h in held)]
    points = round(weight * len(matched) / len(required))
    status: Status
    if len(matched) == len(required):
        status = "met"
    elif matched:
        status = "partial"
    else:
        status = "unmet"
    missing = [r for r in required if r not in matched]
    actual = f"{len(matched)}/{len(required)} held"
    if missing:
        actual += " (missing: " + ", ".join(missing) + ")"
    return Criterion("certifications", "Certifications", ", ".join(required), actual, status, weight, points)


def _category(profile: Mapping[str, Any], tender: Mapping[str, Any]) -> Criterion:
    weight = WEIGHTS["category"]
    cat = str(tender.get("category") or "").strip()
    cpvs = [str(c).strip() for c in (tender.get("cpv_codes") or []) if str(c).strip()]
    if not cat and not cpvs:
        return Criterion("category", "Business category", "Any", "n/a", "met", weight, weight)
    p_cats = norm_list(profile.get("categories"))
    p_cpv = [str(c).strip() for c in (profile.get("cpv_prefixes") or []) if str(c).strip()]
    p_kw = norm_list(profile.get("keywords"))
    required = cat or ", ".join(cpvs[:3])
    reasons: list[str] = []
    if cat and cat.lower() in p_cats:
        reasons.append(f"category '{cat}'")
    cpv_hit = next((c for c in cpvs for p in p_cpv if c.startswith(p)), None)
    if cpv_hit:
        reasons.append(f"CPV {cpv_hit}")
    text_tokens = set(tokens(f"{tender.get('title') or ''} {tender.get('description') or ''}"))
    kw_hit = next((k for k in p_kw if set(tokens(k)) and set(tokens(k)) <= text_tokens), None)
    if kw_hit:
        reasons.append(f"keyword '{kw_hit}'")
    if reasons:
        return Criterion("category", "Business category", required, "matched " + "; ".join(reasons), "met",
                         weight, weight)
    return Criterion("category", "Business category", required, "no category, CPV or keyword match", "unmet",
                     weight, 0)


def _location(profile: Mapping[str, Any], tender: Mapping[str, Any]) -> Criterion:
    weight = WEIGHTS["location"]
    region = str(tender.get("region") or "").strip()
    country = str(tender.get("country") or "").strip()
    regions = norm_list(profile.get("regions"))
    actual = ", ".join(profile.get("regions") or []) or "None set"
    if not region and not country:
        return Criterion("location", "Location", "Any", actual, "met", weight, weight)
    required = ", ".join(x for x in (region, country) if x)
    ok = bool(WILDCARD_REGIONS.intersection(regions)) or any(x and x.lower() in regions for x in (region, country))
    return Criterion("location", "Location", required, actual, "met" if ok else "unmet", weight, weight if ok else 0)


def match_score(profile: Mapping[str, Any], tender: Mapping[str, Any]) -> MatchResult:
    breakdown = [
        _turnover(profile, tender),
        _experience(profile, tender),
        _certifications(profile, tender),
        _category(profile, tender),
        _location(profile, tender),
    ]
    score = sum(c.points for c in breakdown)
    return MatchResult(score=max(0, min(100, score)), breakdown=breakdown)
