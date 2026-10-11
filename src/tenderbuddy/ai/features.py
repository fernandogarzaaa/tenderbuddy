"""AI-assisted features. Each works fully offline (deterministic) and is enhanced
when an OpenAI-compatible LLM is configured. An LLM failure never breaks a
feature: it falls back to the deterministic result and says so in ``generator``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tenderbuddy.ai.llm import LLMClient, LLMError, MockLLM, extract_json, extractive_summary
from tenderbuddy.matcher import MatchResult

log = logging.getLogger(__name__)

MAX_REQUIREMENTS = 40


@dataclass(frozen=True)
class AIResult:
    value: Any
    generator: str


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[truncated]"


def _fallback(llm: LLMClient, exc: Exception, base: str = "rules") -> str:
    log.warning("LLM call failed; using deterministic fallback", extra={"llm": llm.name, "error": str(exc)})
    return f"{base} (LLM fallback: {exc})"


# ---------------------------------------------------------------- summary

def summarize_tender(llm: LLMClient, title: str, description: str, *, max_chars: int = 24000) -> AIResult:
    text = description.strip() or title
    if isinstance(llm, MockLLM):
        return AIResult(extractive_summary(text, 3), "rules")
    messages = [
        {"role": "system", "content": (
            "You summarise public procurement notices for a bid team. Reply with 2-4 plain sentences: what is "
            "being bought, scope/quantities, contract duration, and anything unusual. No preamble.")},
        {"role": "user", "content": clip(f"Title: {title}\n\n{text}", max_chars)},
    ]
    try:
        return AIResult(llm.complete(messages, max_tokens=400), llm.name)
    except LLMError as exc:
        return AIResult(extractive_summary(text, 3), _fallback(llm, exc))


# ---------------------------------------------------------------- requirements

def clean_items(items: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, str):
            continue
        s = " ".join(item.split())
        if 10 <= len(s) <= 500 and s not in out:
            out.append(s)
    return out


def extract_requirements(llm: LLMClient, text: str, rule_based: list[str], *, max_chars: int = 24000) -> AIResult:
    """Merge LLM-extracted requirements (first) with the rule-based ones, de-duplicated."""
    if isinstance(llm, MockLLM) or not text.strip():
        return AIResult(rule_based[:MAX_REQUIREMENTS], "rules")
    messages = [
        {"role": "system", "content": (
            "Extract every eligibility criterion, mandatory requirement and submission document from this tender "
            'document. Reply ONLY with JSON: {"requirements": ["...", ...]}. One short sentence each.')},
        {"role": "user", "content": clip(text, max_chars)},
    ]
    try:
        data = extract_json(llm.complete(messages, json_mode=True, max_tokens=1500))
    except LLMError as exc:
        return AIResult(rule_based[:MAX_REQUIREMENTS], _fallback(llm, exc))
    merged = clean_items(data.get("requirements") if isinstance(data, dict) else data)
    lowered = {i.lower() for i in merged}
    for r in rule_based:
        if r.lower() not in lowered:
            merged.append(r)
            lowered.add(r.lower())
    return AIResult(merged[:MAX_REQUIREMENTS], llm.name)


# ---------------------------------------------------------------- bid draft

def cell(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def fmt_money(value: float | None, currency: str) -> str:
    return f"{currency} {value:,.0f}".strip() if value else "not stated"


def template_bid_draft(profile: dict[str, Any], tender: dict[str, Any], match: MatchResult,
                       requirements: list[dict[str, Any]], today: datetime) -> str:
    company = profile.get("name") or "[Company name]"
    ref = tender.get("ref_no") or "-"
    authority = tender.get("authority") or ""
    categories = profile.get("categories") or []
    overview = (profile.get("capability_statement") or "").strip() or (
        f"{company} has {profile.get('years_experience', 0)} years of relevant experience"
        + (f" in {', '.join(categories)}" if categories else "") + "."
    )
    lines = [
        f"# Bid response: {tender['title']}",
        "",
        f"**Reference:** {ref}  ",
        f"**Contracting authority:** {authority or '-'}  ",
        f"**Submission deadline:** {tender['deadline']:%d %B %Y %H:%M} UTC  ",
        f"**Prepared by:** {company} on {today:%d %B %Y}",
        "",
        "## 1. Cover letter",
        "",
        f"Dear {authority or 'Sir/Madam'},",
        "",
        f"{company} is pleased to submit this response to *{tender['title']}* (ref. {ref}). We have reviewed the "
        "notice and its requirements and confirm our intention to deliver the full scope in accordance with the "
        "published terms.",
        "",
        "Yours faithfully,  ",
        company,
        "",
        "## 2. Company overview",
        "",
        overview,
        "",
        f"- Annual turnover: {fmt_money(profile.get('annual_turnover'), profile.get('currency', ''))}",
        f"- Years of experience: {profile.get('years_experience', 0)}",
        f"- Certifications: {', '.join(profile.get('certifications') or []) or 'none listed'}",
        f"- Operating regions: {', '.join(profile.get('regions') or []) or 'not specified'}",
        "",
        "## 3. Eligibility statement",
        "",
        f"Self-assessed eligibility score: **{match.score}/100**.",
        "",
        "| Criterion | Required | Our position | Status |",
        "|---|---|---|---|",
    ]
    lines += [f"| {cell(c.label)} | {cell(c.required)} | {cell(c.actual)} | {c.status} |" for c in match.breakdown]
    lines += ["", "## 4. Compliance matrix", ""]
    if requirements:
        lines += ["| # | Requirement | Compliance | Evidence |", "|---|---|---|---|"]
        for i, r in enumerate(requirements, 1):
            lines.append(f"| {i} | {cell(r['text'])} | {'Complied' if r.get('is_met') else 'To confirm'} | |")
    else:
        lines.append("No requirements captured yet. Upload the tender document to extract them.")
    scope = (tender.get("summary") or tender.get("description") or "").strip() or "See the published notice."
    lines += ["", "## 5. Scope understanding", "", scope, "", "## 6. Open items before submission", ""]
    gaps = [c for c in match.breakdown if c.status != "met"]
    lines += [f"- {c.label}: required {c.required}; ours {c.actual}." for c in gaps]
    open_reqs = [r["text"] for r in requirements if not r.get("is_met")]
    lines += [f"- Confirm: {t}" for t in open_reqs[:20]]
    if not gaps and not open_reqs:
        lines.append("- None identified. Final review and sign-off.")
    return "\n".join(lines) + "\n"


def draft_bid(llm: LLMClient, profile: dict[str, Any], tender: dict[str, Any], match: MatchResult,
              requirements: list[dict[str, Any]], today: datetime, *, max_chars: int = 24000) -> AIResult:
    base = template_bid_draft(profile, tender, match, requirements, today)
    if isinstance(llm, MockLLM):
        return AIResult(base, "template")
    messages = [
        {"role": "system", "content": (
            "You are a senior bid writer. Improve the draft bid response below into a persuasive, factual Markdown "
            "document. Keep every section, table and fact; never invent certifications, numbers, past projects or "
            "references. Where information is missing write [to complete]. Reply with Markdown only.")},
        {"role": "user", "content": clip(f"Tender description:\n{tender.get('description') or ''}\n\nDraft:\n{base}",
                                         max_chars)},
    ]
    try:
        return AIResult(llm.complete(messages, max_tokens=3000), llm.name)
    except LLMError as exc:
        return AIResult(base, _fallback(llm, exc, "template"))
