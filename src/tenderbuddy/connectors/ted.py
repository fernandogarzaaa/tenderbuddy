"""TED (Tenders Electronic Daily, EU) connector — public v3 Search API, no key needed.

Docs: https://docs.ted.europa.eu/api/latest/search.html
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from tenderbuddy.connectors.base import (
    Connector,
    ConnectorError,
    FetchResult,
    TenderRecord,
    dedupe,
    parse_float,
    parse_iso_datetime,
    pick_lang,
)

FIELDS = [
    "publication-number",
    "notice-title",
    "title-proc",
    "buyer-name",
    "buyer-country",
    "deadline-receipt-tender-date-lot",
    "deadline-receipt-tender-time-lot",
    "deadline-receipt-request-date-lot",
    "classification-cpv",
    "place-of-performance",
    "publication-date",
    "estimated-value-lot",
    "estimated-value-cur-lot",
    "description-proc",
    "notice-type",
]
NOTICE_TYPES = ("cn-standard", "cn-social", "cn-desg")
PAGE_SIZE = 100
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9]+\*?$")


def build_query(since: datetime, countries: list[str], cpv_prefixes: list[str]) -> str:
    for value in [*countries, *cpv_prefixes]:
        if not _SAFE_TOKEN.match(value):
            raise ConnectorError(f"ted: invalid filter value {value!r}")
    parts = [f"notice-type IN ({' '.join(NOTICE_TYPES)})", f"publication-date>={since:%Y%m%d}"]
    if countries:
        parts.append(f"buyer-country IN ({' '.join(countries)})")
    if cpv_prefixes:
        parts.append("classification-cpv IN (" + " ".join(f"{p.rstrip('*')}*" for p in cpv_prefixes) + ")")
    return " AND ".join(parts)


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return "" if value is None else str(value)


def notice_deadline(notice: dict[str, Any]) -> datetime | None:
    """Earliest tender-receipt deadline across lots (date + time when given),
    falling back to the request-to-participate deadline (restricted procedures)."""
    dates = [str(d) for d in notice.get("deadline-receipt-tender-date-lot") or []]
    times = [str(t) for t in notice.get("deadline-receipt-tender-time-lot") or []]
    candidates: list[datetime] = []
    for i, d in enumerate(dates):
        parsed = parse_iso_datetime(f"{d[:10]}T{times[i]}") if i < len(times) and len(d) >= 10 else None
        parsed = parsed or parse_iso_datetime(d)
        if parsed:
            candidates.append(parsed)
    if not candidates:
        for d in notice.get("deadline-receipt-request-date-lot") or []:
            parsed = parse_iso_datetime(str(d))
            if parsed:
                candidates.append(parsed)
    return min(candidates) if candidates else None


def notice_title(notice: dict[str, Any]) -> str:
    title = pick_lang(notice.get("title-proc"))
    if title:
        return title
    full = pick_lang(notice.get("notice-title"))
    parts = [p.strip() for p in full.split(" – ")]
    return parts[-1] if len(parts) >= 3 else full


def parse_notice(notice: dict[str, Any]) -> TenderRecord | None:
    pub_no = str(notice.get("publication-number") or "").strip()
    deadline = notice_deadline(notice)
    title = notice_title(notice)
    if not pub_no or deadline is None or not title:
        return None
    values = [v for v in (parse_float(x) for x in notice.get("estimated-value-lot") or []) if v]
    currencies = dedupe([str(c) for c in notice.get("estimated-value-cur-lot") or []])
    places = [str(p) for p in notice.get("place-of-performance") or []]
    region = next((p for p in places if len(p) > 3 and not p.endswith("ZZZ")), "")
    return TenderRecord(
        source="ted",
        external_id=pub_no,
        ref_no=pub_no,
        title=title,
        deadline=deadline,
        authority=pick_lang(notice.get("buyer-name")),
        cpv_codes=dedupe([str(c) for c in notice.get("classification-cpv") or []]),
        country=_first(notice.get("buyer-country")),
        region=region,
        currency=currencies[0] if len(currencies) == 1 else "",
        estimated_value=sum(values) if values and len(currencies) <= 1 else None,
        published_at=parse_iso_datetime(_first(notice.get("publication-date"))),
        description=pick_lang(notice.get("description-proc"))[:20000],
        url=f"https://ted.europa.eu/en/notice/-/detail/{pub_no}",
    )


class TedConnector(Connector):
    name = "ted"
    label = "TED (EU)"
    description = "EU public procurement notices from Tenders Electronic Daily (public API, no key)."

    def __init__(self, *, api_url: str, countries: list[str] | None = None,
                 cpv_prefixes: list[str] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.api_url = api_url
        self.countries = countries or []
        self.cpv_prefixes = cpv_prefixes or []

    def fetch(self, since: datetime, max_records: int) -> FetchResult:
        result = FetchResult()
        query = build_query(since, self.countries, self.cpv_prefixes)
        page_size = min(PAGE_SIZE, max_records)
        page = 1
        seen = 0
        while seen < max_records:
            data = self.request_json(
                "POST", self.api_url,
                json={"query": query, "fields": FIELDS, "limit": page_size, "page": page,
                      "scope": "ACTIVE", "paginationMode": "PAGE_NUMBER"},
            )
            if not isinstance(data, dict) or not isinstance(data.get("notices"), list):
                raise ConnectorError("ted: unexpected response shape")
            notices = data["notices"]
            for notice in notices[: max_records - seen]:
                seen += 1
                try:
                    rec = parse_notice(notice)
                except (ValueError, TypeError) as exc:
                    result.errors.append(f"{notice.get('publication-number')}: {exc}")
                    rec = None
                if rec is None:
                    result.skipped += 1
                else:
                    result.records.append(rec)
            total = int(data.get("totalNoticeCount") or 0)
            if len(notices) < page_size or page * page_size >= total:
                break
            page += 1
        return result
