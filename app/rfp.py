"""Pure RFP/BOQ text analysis (regex + heuristics, no ML needed).

analyze_rfp(text) -> {"requirements": [...], "dates": [...],
                      "amounts": [...], "risks": [...]}
"""

from __future__ import annotations

import re
from datetime import datetime

_BULLET_RE = re.compile(r"^\s*(?:\d{1,2}[\.\)]|[a-zA-Z][\.\)]|[-*•▪–])\s+\S")
_BULLET_STRIP = re.compile(r"^\s*(?:\d{1,2}[\.\)]|[a-zA-Z][\.\)]|[-*•▪–])\s+")
_MODAL_RE = re.compile(
    r"\b(shall|must|mandatory|required|should|will be|to be submitted|eligib\w+)\b",
    re.IGNORECASE,
)

_DATE_RES = [
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
    re.compile(
        r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b",
        re.IGNORECASE,
    ),
]
_AMOUNT_RES = [
    re.compile(
        r"(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d+)?\s*(?:lakh?s?|crore?s?)?",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:lakh?s?|crore?s?)\b", re.IGNORECASE),
]

_DATE_FMTS = [
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%B %d, %Y",
    "%b %d, %Y",
]

_LABEL_HINTS = {
    "published": ["publish", "issue", "release", "advertise", "nit date"],
    "bid due": ["bid due", "last date", "deadline", "submission", "closing date", "due date"],
    "bid opening": ["opening", "open date", "technical bid open"],
}


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip().rstrip(",")
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _label_for(line: str) -> str | None:
    low = line.lower()
    for label, hints in _LABEL_HINTS.items():
        if any(h in low for h in hints):
            return label
    return None


def analyze_rfp(text: str) -> dict:
    text = text or ""
    lines = text.splitlines()

    requirements: list[str] = []
    for line in lines:
        s = line.strip()
        if len(s) < 10 or len(s) > 500:
            continue
        if _BULLET_RE.match(s) or _MODAL_RE.search(s):
            cleaned = _BULLET_STRIP.sub("", s).strip()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if cleaned and cleaned not in requirements:
                requirements.append(cleaned)
        if len(requirements) >= 40:
            break

    dates: list[dict] = []
    seen_dates: set[str] = set()
    published_at: datetime | None = None
    due_at: datetime | None = None
    for line in lines:
        prev_end = 0
        for rx in _DATE_RES:
            for m in rx.finditer(line):
                raw = m.group(0)
                if raw in seen_dates:
                    prev_end = m.end()
                    continue
                parsed = _parse_date(raw)
                if not parsed:
                    prev_end = m.end()
                    continue
                seen_dates.add(raw)
                # Label from the text since the previous date on this line, so
                # one line can carry both a published date and a due date.
                label = _label_for(line[prev_end:m.start()])
                prev_end = m.end()
                dates.append({"label": label or "mentioned", "date": raw})
                if label == "published" and not published_at:
                    published_at = parsed
                if label == "bid due" and not due_at:
                    due_at = parsed
        if len(dates) >= 20:
            break

    amounts: list[str] = []
    for rx in _AMOUNT_RES:
        for m in rx.finditer(text):
            amt = re.sub(r"\s+", " ", m.group(0)).strip()
            if amt and amt not in amounts:
                amounts.append(amt)
        if len(amounts) >= 20:
            break

    low = text.lower()
    risks: list[dict] = []
    if "liquidated damage" in low or re.search(r"\bpenalt\w*\b", low):
        risks.append(
            {
                "type": "Penalty clause",
                "detail": "Document mentions liquidated damages or penalties for delay/non-performance.",
            }
        )
    if "corrigendum" in low or "addendum" in low:
        risks.append(
            {
                "type": "Corrigendum issued",
                "detail": "A corrigendum/addendum is referenced; verify the latest amendment before bidding.",
            }
        )
    if "performance security" in low or "bank guarantee" in low:
        risks.append(
            {
                "type": "Security deposit",
                "detail": "Performance security or bank guarantee is required; factor in locked capital.",
            }
        )
    if published_at and due_at:
        days = (due_at - published_at).days
        if 0 <= days < 14:
            risks.append(
                {
                    "type": "Short timeline",
                    "detail": f"Only {days} days between published date and bid due date.",
                }
            )
    if re.search(r"\bblacklist\w*\b", low):
        risks.append(
            {
                "type": "Blacklisting clause",
                "detail": "Document references blacklisting for non-compliance.",
            }
        )

    return {
        "requirements": requirements,
        "dates": dates,
        "amounts": amounts,
        "risks": risks,
    }
