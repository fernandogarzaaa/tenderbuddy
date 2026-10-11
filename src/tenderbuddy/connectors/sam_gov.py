"""SAM.gov (US federal) contract opportunities connector — Get Opportunities v2 API.

Requires a free personal public API key from your SAM.gov account
(Account Details -> Public API Key). Docs:
https://open.gsa.gov/api/get-opportunities-public-api/
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from tenderbuddy.connectors.base import (
    Connector,
    ConnectorConfigError,
    ConnectorError,
    FetchResult,
    TenderRecord,
    parse_iso_datetime,
)
from tenderbuddy.models import utcnow

PAGE_SIZE = 100


def clean(value: Any) -> str:
    s = "" if value is None else str(value).strip()
    return "" if s.lower() == "null" else s


def parse_opportunity(opp: dict[str, Any]) -> TenderRecord | None:
    if clean(opp.get("active")).lower() == "no":
        return None
    notice_id = clean(opp.get("noticeId"))
    deadline = parse_iso_datetime(clean(opp.get("responseDeadLine")) or None)
    title = clean(opp.get("title"))
    if not notice_id or deadline is None or not title:
        return None
    pop = opp.get("placeOfPerformance") or {}
    state = pop.get("state") or {}
    country = pop.get("country") or {}
    naics = clean(opp.get("naicsCode"))
    agency = clean(opp.get("fullParentPathName")) or clean(opp.get("department"))
    details = [
        ("Notice type", clean(opp.get("type"))),
        ("Set-aside", clean(opp.get("typeOfSetAsideDescription"))),
        ("NAICS", naics),
        ("Classification code", clean(opp.get("classificationCode"))),
    ]
    return TenderRecord(
        source="sam_gov",
        external_id=notice_id,
        ref_no=clean(opp.get("solicitationNumber")) or notice_id,
        title=title,
        deadline=deadline,
        authority=agency.split(".")[0],
        category=f"NAICS {naics}" if naics else "",
        country=clean(country.get("code")) or "USA",
        region=clean(state.get("code")) or clean(state.get("name")),
        currency="USD",
        published_at=parse_iso_datetime(clean(opp.get("postedDate")) or None),
        description="\n".join(f"{k}: {v}" for k, v in details if v),
        url=clean(opp.get("uiLink")) or f"https://sam.gov/opp/{notice_id}/view",
    )


class SamGovConnector(Connector):
    name = "sam_gov"
    label = "SAM.gov (US)"
    description = "US federal contract opportunities (requires a free SAM.gov public API key)."

    def __init__(self, *, api_url: str, api_key: str, ptypes: list[str] | None = None,
                 now: Callable[[], datetime] = utcnow, **kw: Any) -> None:
        super().__init__(**kw)
        self.api_url = api_url
        self.api_key = api_key
        self.ptypes = ptypes or ["o", "k"]
        self._now = now

    def check_config(self) -> None:
        if not self.api_key:
            raise ConnectorConfigError("sam_gov: SAM_API_KEY is not set")

    def fetch(self, since: datetime, max_records: int) -> FetchResult:
        self.check_config()
        result = FetchResult()
        today = self._now()
        since = max(since, today - timedelta(days=364))  # the API allows at most a 1-year window
        offset = 0
        while offset < max_records:
            limit = min(PAGE_SIZE, max_records - offset)
            data = self.request_json(
                "GET", self.api_url,
                params={"api_key": self.api_key, "postedFrom": since.strftime("%m/%d/%Y"),
                        "postedTo": today.strftime("%m/%d/%Y"), "ptype": ",".join(self.ptypes),
                        "limit": limit, "offset": offset},
            )
            if not isinstance(data, dict) or not isinstance(data.get("opportunitiesData"), list):
                raise ConnectorError("sam_gov: unexpected response shape")
            opps = data["opportunitiesData"]
            for opp in opps:
                try:
                    rec = parse_opportunity(opp)
                except (ValueError, TypeError) as exc:
                    result.errors.append(f"{opp.get('noticeId')}: {exc}")
                    rec = None
                if rec is None:
                    result.skipped += 1
                else:
                    result.records.append(rec)
            offset += len(opps)
            if not opps or offset >= int(data.get("totalRecords") or 0):
                break
        return result
