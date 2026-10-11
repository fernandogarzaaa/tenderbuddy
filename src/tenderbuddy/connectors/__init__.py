"""Pluggable tender-source connectors.

Add a source by subclassing :class:`Connector`, implementing ``fetch`` and
registering a factory in :data:`REGISTRY` (and its name in
``config.KNOWN_CONNECTORS``).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from tenderbuddy.config import Settings
from tenderbuddy.connectors.base import Connector, ConnectorConfigError, ConnectorError, FetchResult, TenderRecord
from tenderbuddy.connectors.contracts_finder import ContractsFinderConnector
from tenderbuddy.connectors.csv_import import CsvConnector
from tenderbuddy.connectors.sam_gov import SamGovConnector
from tenderbuddy.connectors.ted import TedConnector

Factory = Callable[[Settings, "httpx.Client | None"], Connector]


def _ted(s: Settings, c: httpx.Client | None) -> Connector:
    return TedConnector(api_url=s.TED_API_URL, countries=s.TED_COUNTRIES, cpv_prefixes=s.TED_CPV_PREFIXES,
                        client=c, max_retries=s.HTTP_MAX_RETRIES, timeout=s.HTTP_TIMEOUT_SECONDS)


def _cf(s: Settings, c: httpx.Client | None) -> Connector:
    return ContractsFinderConnector(api_url=s.CONTRACTS_FINDER_URL, client=c, max_retries=s.HTTP_MAX_RETRIES,
                                    timeout=s.HTTP_TIMEOUT_SECONDS)


def _sam(s: Settings, c: httpx.Client | None) -> Connector:
    return SamGovConnector(api_url=s.SAM_API_URL, api_key=s.SAM_API_KEY, ptypes=s.SAM_PTYPES, client=c,
                           max_retries=s.HTTP_MAX_RETRIES, timeout=s.HTTP_TIMEOUT_SECONDS)


def _csv(s: Settings, c: httpx.Client | None) -> Connector:
    return CsvConnector(staging_dir=s.staging_dir, client=c)


REGISTRY: dict[str, Factory] = {"ted": _ted, "contracts_finder": _cf, "sam_gov": _sam, "csv": _csv}

LABELS: dict[str, str] = {
    "ted": TedConnector.label,
    "contracts_finder": ContractsFinderConnector.label,
    "sam_gov": SamGovConnector.label,
    "csv": CsvConnector.label,
    "manual": "Manual entry",
    "demo": "Demo data",
}


def build_connector(name: str, settings: Settings, client: httpx.Client | None = None) -> Connector:
    factory = REGISTRY.get(name)
    if factory is None:
        raise ConnectorError(f"unknown connector {name!r}")
    return factory(settings, client)


__all__ = [
    "LABELS",
    "REGISTRY",
    "Connector",
    "ConnectorConfigError",
    "ConnectorError",
    "FetchResult",
    "TenderRecord",
    "build_connector",
]
