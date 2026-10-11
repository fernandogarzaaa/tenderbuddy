"""RFP / tender-document text analysis (deterministic regex + heuristics).

``analyze_rfp(text)`` returns requirements, labelled dates, money amounts and
risk flags. ``extract_pdf_text(path)`` pulls text out of a PDF safely. The LLM
layer (``tenderbuddy.ai``) can add to these results but never replaces them.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TypedDict

log = logging.getLogger(__name__)

MAX_REQUIREMENTS = 40
MAX_DATES = 20
MAX_AMOUNTS = 20
MAX_PDF_PAGES = 300

_BULLET_RE = re.compile(r"^\s*(?:\d{1,2}[\.\)]|[a-zA-Z][\.\)]|[-*•▪–])\s+\S")
_BULLET_STRIP = re.compile(r"^\s*(?:\d{1,2}[\.\)]|[a-zA-Z][\.\)]|[-*•▪–])\s+")
_MODAL_RE = re.compile(
    r"\b(shall|must|mandatory|required|should|will be|to be submitted|eligib\w+)\b", re.IGNORECASE
)
_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
_DATE_RES = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
    re.compile(rf"\b\d{{1,2}}\s+{_MONTHS}\s+\d{{4}}\b", re.IGNORECASE),
    re.compile(rf"\b{_MONTHS}\s+\d{{1,2}},?\s+\d{{4}}\b", re.IGNORECASE),
]
_CUR = r"(?:₹|Rs\.?|INR|€|EUR|£|GBP|US\$|\$|USD|₱|PHP)"
_SCALE = r"(?:lakhs?|lacs?|crores?|million|mn|bn|billion)\b"
_AMOUNT_RES = [
    re.compile(rf"{_CUR}\s*\d[\d,]*(?:\.\d+)?(?:\s*{_SCALE})?", re.IGNORECASE),
    re.compile(rf"\b\d[\d,]*(?:\.\d+)?\s*(?:{_CUR})", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:lakhs?|crores?)\b", re.IGNORECASE),
]
_DATE_FMTS = (
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
    "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%B %d, %Y", "%b %d, %Y",
)
_LABEL_HINTS: dict[str, tuple[str, ...]] = {
    "published": ("publish", "issue", "release", "advertise", "nit date"),
    "bid due": ("bid due", "last date", "deadline", "submission", "closing", "due date"),
    "bid opening": ("opening", "open date"),
    "clarification": ("clarification", "queries", "pre-bid", "prebid"),
}


class DateMention(TypedDict):
    label: str
    date: str


class Risk(TypedDict):
    type: str
    detail: str


class RfpAnalysis(TypedDict):
    requirements: list[str]
    dates: list[DateMention]
    amounts: list[str]
    risks: list[Risk]


def parse_date(raw: str) -> datetime | None:
    raw = raw.strip().rstrip(",")
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def label_for(context: str) -> str | None:
    low = context.lower()
    for label, hints in _LABEL_HINTS.items():
        if any(h in low for h in hints):
            return label
    return None


def extract_requirements(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if len(s) < 10 or len(s) > 500:
            continue
        if _BULLET_RE.match(s) or _MODAL_RE.search(s):
            cleaned = re.sub(r"\s+", " ", _BULLET_STRIP.sub("", s)).strip()
            if cleaned not in out:
                out.append(cleaned)
                if len(out) >= MAX_REQUIREMENTS:
                    break
    return out


def extract_dates(lines: list[str]) -> tuple[list[DateMention], datetime | None, datetime | None]:
    dates: list[DateMention] = []
    seen: set[str] = set()
    published: datetime | None = None
    due: datetime | None = None
    for line in lines:
        matches = sorted((m for rx in _DATE_RES for m in rx.finditer(line)), key=lambda m: m.start())
        prev_end = 0
        for m in matches:
            if m.start() < prev_end:
                continue  # overlaps a match from another pattern
            raw = m.group(0)
            context = line[prev_end : m.start()]
            prev_end = m.end()
            parsed = parse_date(raw)
            if parsed is None or raw in seen:
                continue
            seen.add(raw)
            label = label_for(context) or "mentioned"
            dates.append({"label": label, "date": raw})
            if label == "published" and published is None:
                published = parsed
            elif label == "bid due" and due is None:
                due = parsed
        if len(dates) >= MAX_DATES:
            break
    return dates[:MAX_DATES], published, due


def extract_amounts(text: str) -> list[str]:
    spans: list[tuple[int, int, str]] = []
    for rx in _AMOUNT_RES:
        for m in rx.finditer(text):
            if any(m.start() < e and s < m.end() for s, e, _ in spans):
                continue
            spans.append((m.start(), m.end(), re.sub(r"\s+", " ", m.group(0)).strip()))
    out: list[str] = []
    for _, _, amt in sorted(spans):
        if amt not in out:
            out.append(amt)
    return out[:MAX_AMOUNTS]


def detect_risks(text: str, published: datetime | None, due: datetime | None) -> list[Risk]:
    low = text.lower()
    risks: list[Risk] = []
    if "liquidated damage" in low or re.search(r"\bpenalt(?:y|ies)\b", low):
        risks.append({"type": "Penalty clause",
                      "detail": "Document mentions liquidated damages or penalties for delay/non-performance."})
    if "corrigendum" in low or "addendum" in low:
        risks.append({"type": "Corrigendum issued",
                      "detail": "A corrigendum/addendum is referenced; verify the latest amendment before bidding."})
    if "performance security" in low or "bank guarantee" in low or "performance bond" in low:
        risks.append({"type": "Security deposit",
                      "detail": "Performance security, bond or bank guarantee is required; factor in locked capital."})
    if published and due:
        days = (due - published).days
        if 0 <= days < 14:
            risks.append({"type": "Short timeline",
                          "detail": f"Only {days} days between published date and bid due date."})
    if re.search(r"\b(?:blacklist\w*|debar\w*)\b", low):
        risks.append({"type": "Blacklisting clause",
                      "detail": "Document references blacklisting/debarment for non-compliance."})
    if "unlimited liability" in low:
        risks.append({"type": "Unlimited liability",
                      "detail": "Document mentions unlimited liability; review with legal before bidding."})
    return risks


def analyze_rfp(text: str | None) -> RfpAnalysis:
    text = text or ""
    lines = text.splitlines()
    dates, published, due = extract_dates(lines)
    return {
        "requirements": extract_requirements(lines),
        "dates": dates,
        "amounts": extract_amounts(text),
        "risks": detect_risks(text, published, due),
    }


def extract_pdf_text(path: Path, max_pages: int = MAX_PDF_PAGES) -> str:
    """Best-effort PDF text extraction; returns "" for unreadable/scanned files."""
    import pypdf

    try:
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        parts = []
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception as exc:  # pypdf raises many error types on malformed files
        log.warning("pdf text extraction failed", extra={"path": str(path), "error": str(exc)})
        return ""
