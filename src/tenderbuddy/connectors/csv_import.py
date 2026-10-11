"""CSV connector: imports portal exports (GeM, CPP, eProcure, PhilGEPS, ...) or any CSV.

Used two ways: files dropped into ``CSV_STAGING_DIR`` (``<staging>/<source>/*.csv``)
are imported by sync, and users can upload a CSV in the UI/API.

Columns (header names, case-insensitive)::

    title*, deadline*, ref_no, external_id, authority, category, cpv_codes,
    country, region, currency, estimated_value, bid_security, min_turnover,
    min_experience_years, certs_required, published_date, description, url, source

``*`` = required. Lists (cpv_codes, certs_required) are ``|`` or ``;`` separated.
Legacy beta columns are still accepted and converted to INR: ``est_value_lakh``,
``emd_lakh`` (x 100,000), ``turnover_req_cr`` (x 10,000,000), ``exp_req_years``,
``certs_req`` and ``state``.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tenderbuddy.connectors.base import Connector, FetchResult, TenderRecord, parse_float

LAKH = 100_000
CRORE = 10_000_000
MAX_CSV_BYTES = 10 * 1024 * 1024
_DATE_FMTS = (
    "%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%Y %H:%M", "%d %b %Y", "%d %B %Y", "%b %d, %Y",
)
_SOURCE_RE = re.compile(r"[^a-z0-9_]+")


class CsvFormatError(ValueError):
    pass


def slug(name: str) -> str:
    return _SOURCE_RE.sub("_", name.strip().lower()).strip("_")[:64] or "csv"


def parse_csv_date(raw: str | None) -> datetime | None:
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def split_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[|;]", str(raw)) if p.strip()]


def _get(row: Mapping[str, Any], *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def row_to_record(row: Mapping[str, Any], default_source: str) -> TenderRecord:
    r = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    title = _get(r, "title")
    if not title:
        raise CsvFormatError("missing title")
    deadline = parse_csv_date(_get(r, "deadline"))
    if deadline is None:
        raise CsvFormatError(f"missing or unparseable deadline {_get(r, 'deadline')!r}")
    if any(k in r for k in ("est_value_lakh", "emd_lakh", "turnover_req_cr")):
        currency = _get(r, "currency") or "INR"
        est = parse_float(_get(r, "est_value_lakh"))
        emd = parse_float(_get(r, "emd_lakh"))
        turnover = parse_float(_get(r, "turnover_req_cr"))
        estimated_value = est * LAKH if est is not None else None
        bid_security = emd * LAKH if emd else None
        min_turnover = turnover * CRORE if turnover else None
        country = _get(r, "country") or "IND"
    else:
        currency = _get(r, "currency")
        estimated_value = parse_float(_get(r, "estimated_value"))
        bid_security = parse_float(_get(r, "bid_security"))
        min_turnover = parse_float(_get(r, "min_turnover"))
        country = _get(r, "country")
    exp = parse_float(_get(r, "min_experience_years", "exp_req_years"))
    ref_no = _get(r, "ref_no")
    external_id = _get(r, "external_id") or ref_no or f"{title}|{deadline:%Y-%m-%d}"
    return TenderRecord(
        source=slug(_get(r, "source") or default_source),
        external_id=external_id[:255],
        ref_no=ref_no,
        title=title,
        deadline=deadline,
        authority=_get(r, "authority"),
        category=_get(r, "category"),
        cpv_codes=split_list(_get(r, "cpv_codes")),
        country=country,
        region=_get(r, "region", "state"),
        currency=currency,
        estimated_value=estimated_value,
        bid_security=bid_security or None,
        min_turnover=min_turnover or None,
        min_experience_years=int(exp) if exp else None,
        certs_required=split_list(_get(r, "certs_required", "certs_req")),
        published_at=parse_csv_date(_get(r, "published_date", "published_at")),
        description=_get(r, "description")[:20000],
        url=_get(r, "url"),
    )


def parse_rows(rows: Iterable[Mapping[str, Any]], default_source: str) -> FetchResult:
    result = FetchResult()
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        try:
            result.records.append(row_to_record(row, default_source))
        except ValueError as exc:
            result.skipped += 1
            result.errors.append(f"row {i}: {exc}")
    return result


def parse_csv_bytes(data: bytes, default_source: str) -> FetchResult:
    if len(data) > MAX_CSV_BYTES:
        raise CsvFormatError("CSV file is larger than 10 MB")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
    headers = {h.strip().lower() for h in reader.fieldnames or [] if h}
    missing = {"title", "deadline"} - headers
    if missing:
        raise CsvFormatError("CSV is missing required column(s): " + ", ".join(sorted(missing)))
    return parse_rows(reader, default_source)


class CsvConnector(Connector):
    name = "csv"
    label = "CSV staging folder"
    description = "Imports CSV exports dropped into <staging>/<source>/*.csv (GeM, eProcure, PhilGEPS, ...)."

    def __init__(self, *, staging_dir: Path, **kw: Any) -> None:
        super().__init__(**kw)
        self.staging_dir = Path(staging_dir)

    def staged_files(self) -> list[Path]:
        if not self.staging_dir.is_dir():
            return []
        return sorted(p for p in self.staging_dir.rglob("*.csv") if p.is_file())

    def fetch(self, since: datetime, max_records: int) -> FetchResult:
        result = FetchResult()
        for path in self.staged_files():
            rel = path.relative_to(self.staging_dir)
            default_source = rel.parts[0] if len(rel.parts) > 1 else "csv"
            try:
                part = parse_csv_bytes(path.read_bytes(), default_source)
            except (CsvFormatError, OSError) as exc:
                result.errors.append(f"{rel}: {exc}")
                continue
            result.records.extend(part.records)
            result.skipped += part.skipped
            result.errors.extend(f"{rel}: {e}" for e in part.errors)
            if len(result.records) >= max_records:
                result.records = result.records[:max_records]
                break
        return result
