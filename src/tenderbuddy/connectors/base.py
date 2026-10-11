"""Connector interface: every tender source yields normalised ``TenderRecord``s."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Tenderbuddy/1.0 (+https://github.com/fernandogarzaaa/tenderbuddy)"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class ConnectorError(RuntimeError):
    """A source could not be fetched or parsed."""


class ConnectorConfigError(ConnectorError):
    """A source is enabled but missing required configuration (e.g. an API key)."""


@dataclass
class TenderRecord:
    source: str
    external_id: str
    title: str
    deadline: datetime
    ref_no: str = ""
    authority: str = ""
    category: str = ""
    cpv_codes: list[str] = field(default_factory=list)
    country: str = ""
    region: str = ""
    currency: str = ""
    estimated_value: float | None = None
    bid_security: float | None = None
    min_turnover: float | None = None
    min_experience_years: int | None = None
    certs_required: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    description: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        self.external_id = str(self.external_id).strip()
        self.title = " ".join(str(self.title).split())[:1000]
        if not self.external_id:
            raise ValueError("external_id is required")
        if not self.title:
            raise ValueError("title is required")
        self.deadline = to_naive_utc(self.deadline)
        if self.published_at is not None:
            self.published_at = to_naive_utc(self.published_at)
        self.currency = (self.currency or "").upper()[:3]
        if not self.ref_no:
            self.ref_no = self.external_id


@dataclass
class FetchResult:
    records: list[TenderRecord] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class Connector(ABC):
    """Base class. Subclasses set ``name``/``label`` and implement ``fetch``."""

    name: str = ""
    label: str = ""
    description: str = ""

    def __init__(self, client: httpx.Client | None = None, *, max_retries: int = 3,
                 timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep) -> None:
        self._client = client
        self._owns_client = client is None
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, headers={"User-Agent": USER_AGENT},
                                        follow_redirects=True)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def check_config(self) -> None:  # noqa: B027  (optional hook)
        """Raise ConnectorConfigError when the connector cannot run."""

    @abstractmethod
    def fetch(self, since: datetime, max_records: int) -> FetchResult:
        """Return tender notices published on/after ``since`` (naive UTC)."""

    def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        """HTTP request with retry/backoff on transient failures; returns parsed JSON."""
        attempt = 0
        while True:
            status: int | None = None
            try:
                resp = self.client.request(method, url, **kwargs)
                status = resp.status_code
                if status in RETRY_STATUS and attempt < self.max_retries:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                retryable = isinstance(exc, httpx.TransportError) or status in RETRY_STATUS
                if not retryable or attempt >= self.max_retries:
                    detail = f"HTTP {status}" if status else type(exc).__name__
                    raise ConnectorError(f"{self.name}: request failed ({detail})") from exc
                delay = min(30.0, 2.0**attempt)
                log.warning("retrying source request",
                            extra={"source": self.name, "attempt": attempt + 1, "delay": delay, "status": status})
                self._sleep(delay)
                attempt += 1
            except ValueError as exc:
                raise ConnectorError(f"{self.name}: invalid JSON response") from exc


# ---------------------------------------------------------------- parsing helpers

def to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_iso_datetime(raw: str | None) -> datetime | None:
    """Parse ISO-8601 datetimes and TED-style dates with offsets ("2026-11-06+01:00").

    Date-only values are taken as the end of that day (23:59:59) in their offset,
    the conservative reading of a submission deadline. Returns naive UTC.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return to_naive_utc(datetime.fromisoformat(s))
    except ValueError:
        pass
    if len(s) < 10:
        return None
    try:
        d = date.fromisoformat(s[:10])
    except ValueError:
        return None
    offset = s[10:]
    tz = timezone.utc
    if offset:
        try:
            sign = 1 if offset[0] == "+" else -1 if offset[0] == "-" else 0
            hh, mm = offset[1:].split(":")
            if sign == 0:
                return None
            tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
        except ValueError:
            return None
    return to_naive_utc(datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz))


def parse_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except ValueError:
        return None


def pick_lang(value: Any, preferred: tuple[str, ...] = ("eng", "en")) -> str:
    """TED multilingual fields: {"eng": "..."} or {"eng": ["..."]} -> str."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return str(value[0]) if value else ""
    if isinstance(value, dict):
        for lang in preferred:
            if lang in value:
                return pick_lang(value[lang], preferred)
        for v in value.values():
            return pick_lang(v, preferred)
        return ""
    return str(value)


def dedupe(values: list[str]) -> list[str]:
    seen: list[str] = []
    for v in values:
        v = str(v).strip()
        if v and v not in seen:
            seen.append(v)
    return seen
