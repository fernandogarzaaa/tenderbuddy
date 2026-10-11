"""Live smoke tests against the real public APIs. Not run by default or in CI.

    pytest -m live            # TED + Contracts Finder (no keys needed)
    SAM_API_KEY=... pytest -m live
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from tenderbuddy.connectors.contracts_finder import ContractsFinderConnector
from tenderbuddy.connectors.sam_gov import SamGovConnector
from tenderbuddy.connectors.ted import TedConnector
from tenderbuddy.models import utcnow

pytestmark = pytest.mark.live
SINCE = utcnow() - timedelta(days=7)


def test_live_ted() -> None:
    conn = TedConnector(api_url="https://api.ted.europa.eu/v3/notices/search", countries=["IRL"])
    res = conn.fetch(SINCE, 20)
    conn.close()
    assert res.records, res.errors
    assert all(r.url.startswith("https://ted.europa.eu/") for r in res.records)


def test_live_contracts_finder() -> None:
    conn = ContractsFinderConnector(
        api_url="https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search")
    res = conn.fetch(SINCE, 20)
    conn.close()
    assert res.records, res.errors
    assert all(r.currency for r in res.records)


@pytest.mark.skipif(not os.getenv("SAM_API_KEY"), reason="SAM_API_KEY not set")
def test_live_sam_gov() -> None:
    conn = SamGovConnector(api_url="https://api.sam.gov/opportunities/v2/search", api_key=os.environ["SAM_API_KEY"])
    res = conn.fetch(SINCE, 20)
    conn.close()
    assert res.records or res.skipped, res.errors
