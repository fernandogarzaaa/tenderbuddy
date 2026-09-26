"""Pure eligibility matcher: company profile vs tender criteria.

match_score(profile, tender) -> (score 0-100, breakdown).
Both args are plain dicts so this stays testable without a DB:

    profile = {"turnover_cr": 25.0, "years_exp": 8,
               "certifications": ["ISO 9001", "MSME Registered"],
               "categories": ["IT Services"], "states": ["Karnataka"]}
    tender  = {"turnover_req_cr": 10.0, "exp_req_years": 5,
               "certs_req": ["ISO 9001"], "category": "IT Services",
               "state": "Karnataka"}

A missing/empty requirement is treated as met (no requirement to fail).
"""

from __future__ import annotations


def _norm_list(values) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        return [values.strip().lower()]
    return [str(v).strip().lower() for v in values if str(v).strip()]


def _threshold(actual: float, required: float | None, weight: int) -> tuple[int, bool]:
    if not required or required <= 0:
        return weight, True
    actual = actual or 0.0
    met = actual >= required
    if met:
        return weight, True
    return round(weight * min(1.0, actual / required)), False


def match_score(profile: dict, tender: dict) -> tuple[int, list[dict]]:
    breakdown: list[dict] = []

    # 1. Turnover (30)
    t_req = tender.get("turnover_req_cr")
    t_act = profile.get("turnover_cr") or 0.0
    pts, met = _threshold(t_act, t_req, 30)
    breakdown.append(
        {
            "criterion": "Annual turnover",
            "required": f">= {t_req} Cr" if t_req else "No minimum",
            "actual": f"{t_act} Cr",
            "met": met,
            "weight": 30,
            "points": pts,
        }
    )

    # 2. Experience (20)
    e_req = tender.get("exp_req_years")
    e_act = profile.get("years_exp") or 0
    pts, met = _threshold(float(e_act), e_req, 20)
    breakdown.append(
        {
            "criterion": "Years of experience",
            "required": f">= {e_req} yrs" if e_req else "No minimum",
            "actual": f"{e_act} yrs",
            "met": met,
            "weight": 20,
            "points": pts,
        }
    )

    # 3. Certifications (25) — fraction of required certs held
    certs_req = _norm_list(tender.get("certs_req"))
    certs_have = _norm_list(profile.get("certifications"))
    if not certs_req:
        pts, met = 25, True
        held = []
    else:
        held = [c for c in certs_req if c in certs_have]
        pts = round(25 * len(held) / len(certs_req))
        met = len(held) == len(certs_req)
    breakdown.append(
        {
            "criterion": "Certifications",
            "required": ", ".join(tender.get("certs_req") or []) or "None required",
            "actual": f"{len(held)}/{len(certs_req)} held" if certs_req else "None required",
            "met": met,
            "weight": 25,
            "points": pts,
        }
    )

    # 4. Category (15)
    cat_req = (tender.get("category") or "").strip()
    cats_have = _norm_list(profile.get("categories"))
    if not cat_req:
        pts, met = 15, True
    else:
        met = cat_req.lower() in cats_have
        pts = 15 if met else 0
    breakdown.append(
        {
            "criterion": "Business category",
            "required": cat_req or "Any",
            "actual": ", ".join(profile.get("categories") or []) or "None set",
            "met": met,
            "weight": 15,
            "points": pts,
        }
    )

    # 5. State (10)
    state_req = (tender.get("state") or "").strip()
    states_have = _norm_list(profile.get("states"))
    if not state_req:
        pts, met = 10, True
    else:
        met = state_req.lower() in states_have or "all india" in states_have
        pts = 10 if met else 0
    breakdown.append(
        {
            "criterion": "Operating state",
            "required": state_req or "Any",
            "actual": ", ".join(profile.get("states") or []) or "None set",
            "met": met,
            "weight": 10,
            "points": pts,
        }
    )

    score = int(round(sum(b["points"] for b in breakdown)))
    return max(0, min(100, score)), breakdown
