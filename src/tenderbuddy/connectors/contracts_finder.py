"""UK Contracts Finder connector — public OCDS search API, no key needed.

Docs: https://www.contractsfinder.service.gov.uk/apidocumentation
"""

from __future__ import annotations

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
)

PAGE_SIZE = 100
MAX_PAGES = 100


def parse_release(release: dict[str, Any]) -> TenderRecord | None:
    tender = release.get("tender") or {}
    if (tender.get("status") or "active") not in ("active", "planned"):
        return None
    ocid = str(release.get("ocid") or "").strip()
    deadline = parse_iso_datetime((tender.get("tenderPeriod") or {}).get("endDate"))
    title = str(tender.get("title") or "").strip()
    if not ocid or deadline is None or not title:
        return None
    main = tender.get("classification") or {}
    classifications = [main, *(tender.get("additionalClassifications") or [])]
    cpvs = dedupe([str(c.get("id")) for c in classifications if c.get("scheme") == "CPV" and c.get("id")])
    regions = dedupe([
        str(a.get("region"))
        for item in tender.get("items") or []
        for a in item.get("deliveryAddresses") or []
        if a.get("region")
    ])
    value = tender.get("value") or {}
    url = next(
        (str(d.get("url")) for d in tender.get("documents") or [] if d.get("documentType") == "tenderNotice"), ""
    )
    return TenderRecord(
        source="contracts_finder",
        external_id=ocid,
        ref_no=str(tender.get("id") or ocid),
        title=title,
        deadline=deadline,
        authority=str((release.get("buyer") or {}).get("name") or ""),
        category=str(main.get("description") or ""),
        cpv_codes=cpvs,
        country="GBR",
        region=regions[0] if regions else "",
        currency=str(value.get("currency") or "GBP"),
        estimated_value=parse_float(value.get("amount")),
        published_at=parse_iso_datetime(tender.get("datePublished") or release.get("date")),
        description=str(tender.get("description") or "")[:20000],
        url=url,
    )


class ContractsFinderConnector(Connector):
    name = "contracts_finder"
    label = "Contracts Finder (UK)"
    description = "UK public-sector opportunities from Contracts Finder (OCDS API, no key)."

    def __init__(self, *, api_url: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.api_url = api_url

    def fetch(self, since: datetime, max_records: int) -> FetchResult:
        result = FetchResult()
        params: dict[str, Any] | None = {
            "publishedFrom": since.strftime("%Y-%m-%dT%H:%M:%S"),
            "stages": "tender",
            "limit": min(PAGE_SIZE, max_records),
        }
        url = self.api_url
        seen = 0
        for _ in range(MAX_PAGES):
            data = self.request_json("GET", url, params=params)
            if not isinstance(data, dict) or not isinstance(data.get("releases"), list):
                raise ConnectorError("contracts_finder: unexpected response shape")
            releases = data["releases"]
            for release in releases[: max_records - seen]:
                seen += 1
                try:
                    rec = parse_release(release)
                except (ValueError, TypeError) as exc:
                    result.errors.append(f"{release.get('ocid')}: {exc}")
                    rec = None
                if rec is None:
                    result.skipped += 1
                else:
                    result.records.append(rec)
            nxt = (data.get("links") or {}).get("next")
            if not releases or not nxt or seen >= max_records:
                break
            url, params = str(nxt), None  # the cursor URL already carries the query
        return result
