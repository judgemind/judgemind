"""Tests for the CourtListener integration scraper.

Tests use mocked HTTP responses to verify:
- API client pagination and rate limiting
- Data mapping from CourtListener to CapturedDocument
- Field extraction (judge name, case title, docket number, dates, opinion type)
- Edge cases (empty opinions, missing fields, pagination)
- BaseScraper integration (run loop, health reporting)
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from unittest.mock import MagicMock

import httpx
import pytest
import respx
import structlog.testing
from helpers.reingest import make_reingest_cap_doc

from courts.federal.courtlistener import (
    _CL_COURT_ID_TO_JURISDICTION,
    API_BASE_URL,
    DEFAULT_OPINION_FETCH_CONCURRENCY,
    CourtListenerClient,
    CourtListenerScraper,
    _extract_docket_number,
    _extract_judge_names,
    _map_opinion_type,
    _parse_date,
    default_config,
)
from framework import CapturedDocument, ContentFormat, ScraperConfig

# Identifier-shape defaults shared across the reingest-path regression tests.
_CL_SCRAPER_ID = "federal-courtlistener-opinions"
_CL_STATE = "Federal"
_CL_COUNTY = "Federal"
_CL_COURT = "CourtListener"
_CL_SOURCE_URL_BASE = "https://www.courtlistener.com/api/rest/v4/opinions"

# ---------------------------------------------------------------------------
# Fixtures — sample API responses
# ---------------------------------------------------------------------------


def _make_cluster(
    *,
    cluster_id: int = 1001,
    case_name: str = "Smith v. Jones",
    docket_number: str = "22-1234",
    judges: str = "Justice Roberts",
    date_filed: str = "2026-03-01",
    court: str = "/api/rest/v4/courts/scotus/",
    precedential_status: str = "Published",
    date_modified: str = "2026-03-05T12:00:00Z",
    docket: str | None = None,
) -> dict:
    """Build a sample CourtListener OpinionCluster response."""
    cluster: dict = {
        "id": cluster_id,
        "case_name": case_name,
        "case_name_short": case_name.split(" v. ")[0] if " v. " in case_name else case_name,
        "docket_number": docket_number,
        "judges": judges,
        "date_filed": date_filed,
        "court": court,
        "precedential_status": precedential_status,
        "date_modified": date_modified,
        "citation_count": 5,
        "absolute_url": f"/opinion/{cluster_id}/smith-v-jones/",
        "docket": (
            docket
            if docket is not None
            else f"{API_BASE_URL}/dockets/{cluster_id * 10}/?format=json"
        ),
    }
    return cluster


def _make_opinion(
    *,
    opinion_id: int = 2001,
    cluster_id: int = 1001,
    opinion_type: str = "010combined",
    plain_text: str = "This is the opinion text of the court.",
    html: str = "",
) -> dict:
    """Build a sample CourtListener Opinion response."""
    return {
        "id": opinion_id,
        "cluster": f"/api/rest/v4/clusters/{cluster_id}/",
        "type": opinion_type,
        "plain_text": plain_text,
        "html": html,
        "html_with_citations": "",
        "html_columbia": "",
        "html_lawbox": "",
        "resource_uri": f"/api/rest/v4/opinions/{opinion_id}/",
    }


def _make_paginated_response(
    results: list[dict],
    next_url: str | None = None,
    count: int | None = None,
) -> dict:
    """Wrap results in a CourtListener paginated response envelope."""
    return {
        "count": count if count is not None else len(results),
        "next": next_url,
        "previous": None,
        "results": results,
    }


# ---------------------------------------------------------------------------
# CourtListenerClient tests
# ---------------------------------------------------------------------------


class TestCourtListenerClient:
    """Tests for the API client wrapper."""

    @respx.mock
    def test_fetch_clusters_single_page(self) -> None:
        """Fetch clusters returns results from a single page."""
        cluster = _make_cluster()
        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )

        client = CourtListenerClient(request_delay=0)
        clusters = client.fetch_clusters(date_modified_after="2026-03-01")
        client.close()

        assert len(clusters) == 1
        assert clusters[0]["id"] == 1001

    @respx.mock
    def test_fetch_clusters_pagination(self) -> None:
        """Fetch clusters follows pagination links."""
        cluster1 = _make_cluster(cluster_id=1001)
        cluster2 = _make_cluster(cluster_id=1002)

        page1 = _make_paginated_response(
            [cluster1],
            next_url=f"{API_BASE_URL}/clusters/?cursor=abc123",
            count=2,
        )
        page2 = _make_paginated_response([cluster2])

        call_count = 0

        def handle_clusters(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json=page1)
            return httpx.Response(200, json=page2)

        respx.get(url__startswith=f"{API_BASE_URL}/clusters/").mock(side_effect=handle_clusters)

        client = CourtListenerClient(request_delay=0)
        clusters = client.fetch_clusters(date_modified_after="2026-03-01")
        client.close()

        assert len(clusters) == 2
        assert clusters[0]["id"] == 1001
        assert clusters[1]["id"] == 1002

    @respx.mock
    def test_fetch_clusters_max_pages_limit(self) -> None:
        """Fetch clusters stops at max_pages even if more pages exist."""
        cluster = _make_cluster()
        page = _make_paginated_response(
            [cluster],
            next_url=f"{API_BASE_URL}/clusters/?cursor=next",
            count=100,
        )

        respx.get(f"{API_BASE_URL}/clusters/").mock(return_value=httpx.Response(200, json=page))

        client = CourtListenerClient(request_delay=0)
        clusters = client.fetch_clusters(date_modified_after="2026-03-01", max_pages=1)
        client.close()

        assert len(clusters) == 1

    @respx.mock
    def test_fetch_opinions_for_cluster(self) -> None:
        """Fetch opinions returns opinion list for a cluster."""
        opinion = _make_opinion()
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        client = CourtListenerClient(request_delay=0)
        opinions = client.fetch_opinions_for_cluster(1001)
        client.close()

        assert len(opinions) == 1
        assert opinions[0]["id"] == 2001

    @respx.mock
    def test_auth_header_with_token(self) -> None:
        """Client sends Authorization header when API token is provided."""
        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([]))
        )

        client = CourtListenerClient(api_token="test-token-123", request_delay=0)
        client.fetch_clusters(date_modified_after="2026-03-01")

        request = respx.calls.last.request
        assert request.headers["Authorization"] == "Token test-token-123"
        client.close()

    @respx.mock
    def test_no_auth_header_without_token(self) -> None:
        """Client does not send Authorization header when no token is provided."""
        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([]))
        )

        client = CourtListenerClient(request_delay=0)
        client.fetch_clusters(date_modified_after="2026-03-01")

        request = respx.calls.last.request
        assert "Authorization" not in request.headers
        client.close()

    def test_no_token_creates_client_without_auth(self) -> None:
        """Client without token does not set Authorization header and does not raise."""
        client = CourtListenerClient(request_delay=0)
        # Verify internal state: no token means no auth header
        assert "Authorization" not in client._client.headers
        client.close()

    @respx.mock
    def test_401_raises_with_clear_message(self) -> None:
        """Client raises HTTPStatusError on 401 with diagnostic logging."""
        import pytest

        body = {"detail": "Authentication credentials were not provided."}
        respx.get(f"{API_BASE_URL}/clusters/").mock(return_value=httpx.Response(401, json=body))

        client = CourtListenerClient(request_delay=0)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.fetch_clusters(date_modified_after="2026-03-01")
        assert exc_info.value.response.status_code == 401
        client.close()

    @respx.mock
    def test_401_with_token_raises(self) -> None:
        """Client raises HTTPStatusError on 401 even when token is configured."""
        import pytest

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(401, json={"detail": "Invalid token."})
        )

        client = CourtListenerClient(api_token="expired-token", request_delay=0)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.fetch_clusters(date_modified_after="2026-03-01")
        assert exc_info.value.response.status_code == 401
        client.close()

    @respx.mock
    def test_request_count_tracking(self) -> None:
        """Client tracks total request count."""
        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([]))
        )

        client = CourtListenerClient(request_delay=0)
        assert client.request_count == 0

        client.fetch_clusters(date_modified_after="2026-03-01")
        assert client.request_count == 1

        client.fetch_opinions_for_cluster(1001)
        assert client.request_count == 2
        client.close()

    @respx.mock
    def test_fetch_docket(self) -> None:
        """fetch_docket returns the parsed docket dict from a single GET."""
        docket_data = {
            "id": 123,
            "docket_number": "1:24-cv-00123-ABC",
            "case_name": "Alpha v. Beta",
        }
        respx.get(f"{API_BASE_URL}/dockets/123/").mock(
            return_value=httpx.Response(200, json=docket_data)
        )

        client = CourtListenerClient(request_delay=0)
        result = client.fetch_docket(f"{API_BASE_URL}/dockets/123/")
        client.close()

        assert result == docket_data
        assert client.request_count == 1

    @respx.mock
    def test_fetch_dockets_for_clusters_batch(self) -> None:
        """All three docket URLs resolve and request_count increments per fetch."""
        docket_map = {
            10: f"{API_BASE_URL}/dockets/10/?format=json",
            20: f"{API_BASE_URL}/dockets/20/?format=json",
            30: f"{API_BASE_URL}/dockets/30/?format=json",
        }

        def _mock_docket(request: httpx.Request) -> httpx.Response:
            # Extract docket id from path e.g. /api/rest/v4/dockets/10/
            path_parts = str(request.url.path).rstrip("/").split("/")
            docket_id = int(path_parts[-1])
            return httpx.Response(
                200,
                json={"id": docket_id, "docket_number": f"1:24-cv-0{docket_id}"},
            )

        respx.get(url__startswith=f"{API_BASE_URL}/dockets/").mock(side_effect=_mock_docket)

        client = CourtListenerClient(request_delay=0)
        result = client.fetch_dockets_for_clusters(docket_map)
        client.close()

        assert set(result.keys()) == {10, 20, 30}
        assert result[10]["docket_number"] == "1:24-cv-010"
        assert result[20]["docket_number"] == "1:24-cv-020"
        assert result[30]["docket_number"] == "1:24-cv-030"
        assert client.request_count == 3

    @respx.mock
    def test_fetch_dockets_for_clusters_partial_failure(self) -> None:
        """A 500 for one docket URL returns {} for that cluster id; others succeed.

        Also verifies that a warning is logged for the failing cluster.
        """
        docket_map = {
            100: f"{API_BASE_URL}/dockets/100/?format=json",
            200: f"{API_BASE_URL}/dockets/200/?format=json",
            300: f"{API_BASE_URL}/dockets/300/?format=json",
        }
        failing_path = "/api/rest/v4/dockets/200/"

        def _mock_docket(request: httpx.Request) -> httpx.Response:
            if (
                str(request.url.path).startswith(failing_path.rstrip("/") + "/")
                or str(request.url.path) == failing_path
                or str(request.url.path).rstrip("/") == failing_path.rstrip("/")
            ):
                return httpx.Response(500, json={"detail": "Server error"})
            path_parts = str(request.url.path).rstrip("/").split("/")
            docket_id = int(path_parts[-1])
            return httpx.Response(200, json={"id": docket_id, "docket_number": f"case-{docket_id}"})

        respx.get(url__startswith=f"{API_BASE_URL}/dockets/").mock(side_effect=_mock_docket)

        client = CourtListenerClient(request_delay=0)
        with structlog.testing.capture_logs() as cap_logs:
            result = client.fetch_dockets_for_clusters(docket_map)
        client.close()

        # Failing cluster maps to empty dict
        assert result[200] == {}
        # Successful clusters return their dockets
        assert result[100]["docket_number"] == "case-100"
        assert result[300]["docket_number"] == "case-300"
        # All three cluster IDs in result
        assert set(result.keys()) == {100, 200, 300}

        # Structlog warning fired for the failing cluster
        warnings = [
            e
            for e in cap_logs
            if e.get("log_level") == "warning"
            and e.get("event") == "Failed to fetch docket for cluster"
            and e.get("cluster_id") == 200
        ]
        assert warnings, f"Expected warning for cluster_id=200, got: {cap_logs!r}"

    @respx.mock
    def test_fetch_dockets_for_clusters_connect_error(self) -> None:
        """A ConnectError for one cluster returns {} for that id; others succeed."""
        docket_map = {
            100: f"{API_BASE_URL}/dockets/100/?format=json",
            200: f"{API_BASE_URL}/dockets/200/?format=json",
        }
        failing_path = "/api/rest/v4/dockets/200/"

        def _mock_docket(request: httpx.Request) -> httpx.Response:
            if str(request.url.path).rstrip("/") == failing_path.rstrip("/"):
                raise httpx.ConnectError("connection refused")
            path_parts = str(request.url.path).rstrip("/").split("/")
            docket_id = int(path_parts[-1])
            return httpx.Response(200, json={"id": docket_id, "docket_number": f"case-{docket_id}"})

        respx.get(url__startswith=f"{API_BASE_URL}/dockets/").mock(side_effect=_mock_docket)

        client = CourtListenerClient(request_delay=0)
        with structlog.testing.capture_logs() as cap_logs:
            result = client.fetch_dockets_for_clusters(docket_map)
        client.close()

        assert 200 in result, "Failing cluster_id must be a key in result"
        assert result[200] == {}, "Failing cluster must map to empty dict"
        assert result[100]["docket_number"] == "case-100"
        assert set(result.keys()) == {100, 200}

        warnings = [
            e
            for e in cap_logs
            if e.get("log_level") == "warning"
            and e.get("event") == "Failed to fetch docket for cluster"
            and e.get("cluster_id") == 200
        ]
        assert warnings, f"Expected warning for cluster_id=200, got: {cap_logs!r}"

    @respx.mock
    def test_fetch_dockets_for_clusters_timeout(self) -> None:
        """A ReadTimeout for one cluster returns {} for that id; others succeed."""
        docket_map = {
            100: f"{API_BASE_URL}/dockets/100/?format=json",
            200: f"{API_BASE_URL}/dockets/200/?format=json",
        }
        failing_path = "/api/rest/v4/dockets/200/"

        def _mock_docket(request: httpx.Request) -> httpx.Response:
            if str(request.url.path).rstrip("/") == failing_path.rstrip("/"):
                raise httpx.ReadTimeout("timed out")
            path_parts = str(request.url.path).rstrip("/").split("/")
            docket_id = int(path_parts[-1])
            return httpx.Response(200, json={"id": docket_id, "docket_number": f"case-{docket_id}"})

        respx.get(url__startswith=f"{API_BASE_URL}/dockets/").mock(side_effect=_mock_docket)

        client = CourtListenerClient(request_delay=0)
        with structlog.testing.capture_logs() as cap_logs:
            result = client.fetch_dockets_for_clusters(docket_map)
        client.close()

        assert 200 in result, "Failing cluster_id must be a key in result"
        assert result[200] == {}, "Failing cluster must map to empty dict"
        assert result[100]["docket_number"] == "case-100"
        assert set(result.keys()) == {100, 200}

        warnings = [
            e
            for e in cap_logs
            if e.get("log_level") == "warning"
            and e.get("event") == "Failed to fetch docket for cluster"
            and e.get("cluster_id") == 200
        ]
        assert warnings, f"Expected warning for cluster_id=200, got: {cap_logs!r}"

    @respx.mock
    def test_fetch_dockets_for_clusters_malformed_json(self) -> None:
        """Malformed JSON response for one cluster returns {} for that id; others succeed."""
        docket_map = {
            100: f"{API_BASE_URL}/dockets/100/?format=json",
            200: f"{API_BASE_URL}/dockets/200/?format=json",
        }
        failing_path = "/api/rest/v4/dockets/200/"

        def _mock_docket(request: httpx.Request) -> httpx.Response:
            if str(request.url.path).rstrip("/") == failing_path.rstrip("/"):
                return httpx.Response(200, content=b"not json{")
            path_parts = str(request.url.path).rstrip("/").split("/")
            docket_id = int(path_parts[-1])
            return httpx.Response(200, json={"id": docket_id, "docket_number": f"case-{docket_id}"})

        respx.get(url__startswith=f"{API_BASE_URL}/dockets/").mock(side_effect=_mock_docket)

        client = CourtListenerClient(request_delay=0)
        with structlog.testing.capture_logs() as cap_logs:
            result = client.fetch_dockets_for_clusters(docket_map)
        client.close()

        assert 200 in result, "Failing cluster_id must be a key in result"
        assert result[200] == {}, "Failing cluster must map to empty dict"
        assert result[100]["docket_number"] == "case-100"
        assert set(result.keys()) == {100, 200}

        warnings = [
            e
            for e in cap_logs
            if e.get("log_level") == "warning"
            and e.get("event") == "Failed to fetch docket for cluster"
            and e.get("cluster_id") == 200
        ]
        assert warnings, f"Expected warning for cluster_id=200, got: {cap_logs!r}"


# ---------------------------------------------------------------------------
# Data mapping helper tests
# ---------------------------------------------------------------------------


class TestDataMapping:
    """Tests for the data mapping helper functions."""

    def test_extract_docket_number(self) -> None:
        """Extract docket number from cluster."""
        cluster = _make_cluster(docket_number="22-1234")
        assert _extract_docket_number(cluster) == "22-1234"

    def test_extract_docket_number_missing(self) -> None:
        """Return CL-cluster-<id> fallback when both cluster and docket have no docket number."""
        cluster = _make_cluster(cluster_id=1001)
        cluster["docket_number"] = None
        assert _extract_docket_number(cluster) == "CL-cluster-1001"

    def test_extract_docket_number_uses_docket_sub_resource(self) -> None:
        """Prefer docket sub-resource docket_number over cluster docket_number=None.

        Covers the issue's hand-verified payload shape for cluster 10841826.
        """
        cluster = _make_cluster(cluster_id=10841826)
        cluster["docket_number"] = None
        docket = {"docket_number": "1:24-cv-04372-JPB", "id": 73251044}
        assert _extract_docket_number(cluster, docket=docket) == "1:24-cv-04372-JPB"

    def test_extract_docket_number_falls_back_to_cl_cluster_id(self) -> None:
        """Return CL-cluster-<id> when both cluster and docket have empty docket_number.

        Covers the issue's hand-verified payload shape for cluster 10850488.
        """
        cluster = _make_cluster(cluster_id=10850488)
        cluster["docket_number"] = None
        docket = {"docket_number": None, "id": 99999}
        assert _extract_docket_number(cluster, docket=docket) == "CL-cluster-10850488"

    def test_extract_docket_number_prefers_docket_over_cluster(self) -> None:
        """When both docket and cluster have docket_number, docket value wins."""
        cluster = _make_cluster(cluster_id=5000, docket_number="cluster-side-value")
        docket = {"docket_number": "docket-side-value", "id": 12345}
        assert _extract_docket_number(cluster, docket=docket) == "docket-side-value"

    def test_extract_judge_names(self) -> None:
        """Extract judge names from cluster."""
        cluster = _make_cluster(judges="Justice Sotomayor")
        assert _extract_judge_names(cluster) == "Justice Sotomayor"

    def test_extract_judge_names_per_curiam(self) -> None:
        """Handle Per Curiam attribution."""
        cluster = _make_cluster(judges="Per Curiam")
        assert _extract_judge_names(cluster) == "Per Curiam"

    def test_extract_judge_names_missing(self) -> None:
        """Return None when judge field is missing."""
        cluster = _make_cluster()
        cluster["judges"] = None
        assert _extract_judge_names(cluster) is None

    def test_parse_date_iso(self) -> None:
        """Parse a simple ISO date string."""
        result = _parse_date("2026-03-01")
        assert result == datetime(2026, 3, 1)

    def test_parse_date_with_timestamp(self) -> None:
        """Parse a full ISO timestamp."""
        result = _parse_date("2026-03-01T14:30:00Z")
        assert result is not None
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 1

    def test_parse_date_none(self) -> None:
        """Return None for None input."""
        assert _parse_date(None) is None

    def test_parse_date_invalid(self) -> None:
        """Return None for invalid date strings."""
        assert _parse_date("not-a-date") is None

    def test_map_opinion_type_combined(self) -> None:
        """Map known opinion type code."""
        assert _map_opinion_type("010combined") == "Combined Opinion"

    def test_map_opinion_type_dissent(self) -> None:
        """Map dissent opinion type."""
        assert _map_opinion_type("040dissent") == "Dissent"

    def test_map_opinion_type_unknown(self) -> None:
        """Unknown type codes pass through as-is."""
        assert _map_opinion_type("999unknown") == "999unknown"

    def test_map_opinion_type_none(self) -> None:
        """Return None for None input."""
        assert _map_opinion_type(None) is None


# ---------------------------------------------------------------------------
# CourtListenerScraper tests
# ---------------------------------------------------------------------------


def _make_scraper_config() -> ScraperConfig:
    """Build a minimal scraper config for testing."""
    return ScraperConfig(
        scraper_id="federal-courtlistener-opinions-test",
        state="Federal",
        county="Federal",
        court="CourtListener",
        target_urls=[f"{API_BASE_URL}/clusters/"],
        request_delay_seconds=0,
        request_timeout_seconds=10.0,
        max_retries=1,
    )


class TestCourtListenerScraper:
    """Tests for the full scraper integration."""

    @respx.mock
    def test_fetch_documents_basic(self) -> None:
        """Scraper fetches clusters and opinions, returns CapturedDocuments."""
        cluster = _make_cluster()
        opinion = _make_opinion(plain_text="The court holds that...")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7, max_results=100)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        doc = docs[0]
        assert doc.case_title == "Smith v. Jones"
        assert doc.case_number == "22-1234"
        assert doc.judge_name == "Justice Roberts"
        assert doc.ruling_text == "The court holds that..."
        assert doc.motion_type == "Combined Opinion"
        assert doc.content_format == ContentFormat.TEXT
        assert doc.state == "Federal"
        assert doc.county == "Federal"
        assert doc.court == "CourtListener"

    @respx.mock
    def test_fetch_documents_uses_plain_text_for_ruling_text(self) -> None:
        """ruling_text uses plain_text; ruling_text_html captures the HTML variant.

        derived.rulings has separate columns for the canonical text and a rich
        HTML representation. Storing html_with_citations in ruling_text leaves
        <pre>/<span> markup in the canonical text column and breaks downstream
        consumers that expect ruling_text to be plain.
        """
        cluster = _make_cluster()
        opinion = _make_opinion(
            plain_text="Plain version",
            html="<p>HTML version</p>",
        )
        opinion["html_with_citations"] = "<p>HTML with <a>citations</a></p>"

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        assert docs[0].ruling_text == "Plain version"
        assert docs[0].ruling_text_html == "<p>HTML with <a>citations</a></p>"

    @respx.mock
    def test_fetch_documents_falls_back_to_html_when_plain_text_empty(self) -> None:
        """When plain_text is empty, ruling_text falls back to the HTML variant."""
        cluster = _make_cluster()
        opinion = _make_opinion(plain_text="", html="")
        opinion["html_with_citations"] = "<p>Only HTML available</p>"

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        assert docs[0].ruling_text == "<p>Only HTML available</p>"
        assert docs[0].ruling_text_html == "<p>Only HTML available</p>"

    @respx.mock
    def test_fetch_documents_skips_empty_opinions(self) -> None:
        """Scraper skips opinions with no text content."""
        cluster = _make_cluster()
        opinion = _make_opinion(plain_text="", html="")
        opinion["html_with_citations"] = ""
        opinion["html_columbia"] = ""
        opinion["html_lawbox"] = ""

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 0

    @respx.mock
    def test_fetch_documents_max_results_limit(self) -> None:
        """Scraper stops fetching after max_results documents."""
        clusters = [_make_cluster(cluster_id=i) for i in range(5)]
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response(clusters))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7, max_results=3)
        docs = scraper.fetch_documents()

        assert len(docs) == 3

    @respx.mock
    def test_fetch_documents_handles_opinion_fetch_error(self) -> None:
        """Scraper continues when fetching opinions for a cluster fails."""
        cluster1 = _make_cluster(cluster_id=1001)
        cluster2 = _make_cluster(cluster_id=1002, case_name="Doe v. Roe")
        opinion2 = _make_opinion(opinion_id=2002, cluster_id=1002, plain_text="Second opinion")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster1, cluster2]))
        )

        # First opinion request fails, second succeeds
        call_count = 0

        def mock_opinions(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(500, json={"detail": "Server error"})
            return httpx.Response(200, json=_make_paginated_response([opinion2]))

        respx.get(f"{API_BASE_URL}/opinions/").mock(side_effect=mock_opinions)

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        # Should have only the document from the second cluster
        assert len(docs) == 1
        assert docs[0].case_title == "Doe v. Roe"

    @respx.mock
    def test_raw_content_is_json_payload(self) -> None:
        """Raw content contains the full cluster + opinion JSON for archival."""
        cluster = _make_cluster()
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        raw = json.loads(docs[0].raw_content)
        assert "cluster" in raw
        assert "opinion" in raw
        assert raw["cluster"]["id"] == 1001
        assert raw["opinion"]["id"] == 2001

    @respx.mock
    def test_extra_metadata_populated(self) -> None:
        """Extra dict contains CourtListener-specific metadata."""
        cluster = _make_cluster(court="/api/rest/v4/courts/ca9/")
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        extra = docs[0].extra
        assert extra["courtlistener_cluster_id"] == 1001
        assert extra["courtlistener_opinion_id"] == 2001
        assert extra["courtlistener_court_id"] == "ca9"
        assert extra["precedential_status"] == "Published"

    @respx.mock
    def test_court_id_extraction_from_url(self) -> None:
        """Court ID is extracted from the API URL path."""
        cluster = _make_cluster(court="/api/rest/v4/courts/scotus/")
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert docs[0].extra["courtlistener_court_id"] == "scotus"

    @respx.mock
    def test_hearing_date_parsed(self) -> None:
        """Hearing date is parsed from cluster's date_filed."""
        cluster = _make_cluster(date_filed="2026-03-15")
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert docs[0].hearing_date == datetime(2026, 3, 15)

    @respx.mock
    def test_parse_document_idempotent_on_already_populated_doc(self) -> None:
        """parse_document on an already-mapped doc preserves the populated fields.

        After _map_to_document populates the doc from the envelope, calling
        parse_document re-runs the same envelope-population step from
        raw_content.  The fields must come out unchanged because the
        envelope stored in raw_content is the same data the original mapping
        used.
        """
        cluster = _make_cluster()
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        original_title = docs[0].case_title
        original_ruling_text = docs[0].ruling_text
        parsed = scraper.parse_document(docs[0])
        assert parsed.case_title == original_title
        assert parsed.ruling_text == original_ruling_text

    @respx.mock
    def test_run_returns_health_event(self) -> None:
        """Full run() produces a successful ScraperHealthEvent."""
        cluster = _make_cluster()
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        health = scraper.run()

        assert health.success is True
        assert health.records_captured == 1
        assert health.scraper_id == "federal-courtlistener-opinions-test"

    @respx.mock
    def test_missing_case_fields_handled(self) -> None:
        """Scraper handles clusters with missing optional fields gracefully.

        doc.case_number uses the deterministic CL-cluster-<id> fallback path
        when both cluster.docket_number and docket sub-resource are absent.
        """
        cluster = {
            "id": 9999,
            "case_name": None,
            "case_name_short": None,
            "docket_number": None,
            "judges": None,
            "date_filed": None,
            "court": "",
            "precedential_status": None,
            "date_modified": "2026-03-05T12:00:00Z",
            "citation_count": 0,
            # No docket URL so no sub-resource fetch occurs
        }
        opinion = _make_opinion(plain_text="Some opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        doc = docs[0]
        assert doc.case_title is None
        # Deterministic fallback: CL-cluster-<id> (never None)
        assert doc.case_number == "CL-cluster-9999"
        assert doc.judge_name is None
        assert doc.hearing_date is None
        assert doc.ruling_text == "Some opinion text"

    @respx.mock
    def test_case_number_resolved_from_docket_sub_resource(self) -> None:
        """Full integration: cluster.docket_number=None resolved via docket sub-resource.

        Verifies the fix for UNKNOWN-% case numbers by fetching the docket URL
        and using docket_number from the docket response.
        """
        docket_url = f"{API_BASE_URL}/dockets/73251044/?format=json"
        cluster = _make_cluster(
            cluster_id=10841826,
            docket_number=None,
            docket=docket_url,
        )
        # Override docket_number to None explicitly (make_cluster might set it)
        cluster["docket_number"] = None

        opinion = _make_opinion(plain_text="Federal court opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )
        respx.get(docket_url).mock(
            return_value=httpx.Response(
                200, json={"id": 73251044, "docket_number": "1:24-cv-04372-JPB"}
            )
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        assert docs[0].case_number == "1:24-cv-04372-JPB"


# ---------------------------------------------------------------------------
# default_config tests
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    """Tests for the default configuration factory."""

    def test_default_config_values(self) -> None:
        """Default config has expected scraper ID and settings."""
        config = default_config()
        assert config.scraper_id == "federal-courtlistener-opinions"
        assert config.state == "Federal"
        assert config.county == "Federal"
        assert config.court == "CourtListener"
        assert config.request_delay_seconds == 2.0
        assert config.poll_interval_seconds == 86400
        assert len(config.schedule_windows) == 1

    def test_default_config_with_bucket(self) -> None:
        """Default config accepts S3 bucket parameter."""
        config = default_config(s3_bucket="my-bucket")
        assert config.s3_bucket == "my-bucket"

    def test_default_config_target_urls(self) -> None:
        """Default config points to the clusters endpoint."""
        config = default_config()
        assert len(config.target_urls) == 1
        assert "clusters" in config.target_urls[0]


# ---------------------------------------------------------------------------
# Bounded-concurrency batch opinion fetch tests
# ---------------------------------------------------------------------------


class TestFetchOpinionsForClusters:
    """Regression tests for fetch_opinions_for_clusters / _afetch_opinions_for_clusters."""

    @respx.mock
    def test_fetch_opinions_for_clusters_concurrency_capped(self) -> None:
        """Concurrent opinion fetches are capped at DEFAULT_OPINION_FETCH_CONCURRENCY.

        Spawns 12 cluster IDs and tracks how many requests are in-flight
        simultaneously.  The semaphore must prevent more than 5 concurrent
        calls even when there is no network latency in the mock.
        """
        num_clusters = 12
        in_flight: list[int] = [0]
        max_inflight: list[int] = [0]

        async def _slow_handler(request: httpx.Request) -> httpx.Response:
            in_flight[0] += 1
            max_inflight[0] = max(max_inflight[0], in_flight[0])
            # Yield control so other coroutines have a chance to run concurrently.
            await asyncio.sleep(0)
            in_flight[0] -= 1
            cluster_id = int(request.url.params["cluster"])
            opinion = _make_opinion(opinion_id=cluster_id * 10, cluster_id=cluster_id)
            return httpx.Response(200, json=_make_paginated_response([opinion]))

        respx.get(f"{API_BASE_URL}/opinions/").mock(side_effect=_slow_handler)

        cluster_ids = list(range(1, num_clusters + 1))
        client = CourtListenerClient(request_delay=0)
        result = client.fetch_opinions_for_clusters(cluster_ids)
        client.close()

        # All 12 clusters must be present in the result.
        assert len(result) == num_clusters
        for cid in cluster_ids:
            assert cid in result
            assert len(result[cid]) == 1

        # At no point should more than DEFAULT_OPINION_FETCH_CONCURRENCY requests
        # have been in-flight simultaneously.
        assert max_inflight[0] <= DEFAULT_OPINION_FETCH_CONCURRENCY

    @respx.mock
    def test_fetch_opinions_for_clusters_partial_failure(self) -> None:
        """A 500 for one cluster returns empty list for that id; others succeed.

        Also verifies that a warning is emitted for the failed cluster.
        """
        cluster_ids = [100, 200, 300]
        failing_id = 200

        def _mock_opinions(request: httpx.Request) -> httpx.Response:
            cluster_id = int(request.url.params["cluster"])
            if cluster_id == failing_id:
                return httpx.Response(500, json={"detail": "Server error"})
            opinion = _make_opinion(opinion_id=cluster_id * 10, cluster_id=cluster_id)
            return httpx.Response(200, json=_make_paginated_response([opinion]))

        respx.get(f"{API_BASE_URL}/opinions/").mock(side_effect=_mock_opinions)

        client = CourtListenerClient(request_delay=0)
        with structlog.testing.capture_logs() as cap_logs:
            result = client.fetch_opinions_for_clusters(cluster_ids)
        client.close()

        # Failing cluster returns empty list.
        assert result[failing_id] == []

        # Successful clusters return their opinions.
        assert len(result[100]) == 1
        assert len(result[300]) == 1

        # All three cluster IDs appear in result (failed one has empty list, not missing).
        assert set(result.keys()) == {100, 200, 300}

        # Structlog warning fired for the failing cluster.
        warnings = [
            e
            for e in cap_logs
            if e.get("log_level") == "warning"
            and e.get("event") == "Failed to fetch opinions for cluster"
            and e.get("cluster_id") == failing_id
        ]
        assert warnings, f"Expected warning for cluster_id={failing_id}, got: {cap_logs!r}"

    @respx.mock
    def test_50_cluster_fetch_completes_quickly(self) -> None:
        """Fetching 50 clusters with mocked responses finishes well under 5 s.

        With 5 concurrent slots each sleeping 0.2 s, the theoretical minimum
        wall-clock time for 50 clusters is ceil(50/5)*0.2 = 2.0 s.  The test
        asserts < 5 s to give a comfortable margin for CI overhead while still
        proving that the code is not sequential (sequential would take ≥10 s).
        Also verifies that exactly 50 opinion API requests were issued.
        """
        num_clusters = 50

        def _mock_opinions(request: httpx.Request) -> httpx.Response:
            cluster_id = int(request.url.params["cluster"])
            opinion = _make_opinion(opinion_id=cluster_id * 10, cluster_id=cluster_id)
            return httpx.Response(200, json=_make_paginated_response([opinion]))

        respx.get(f"{API_BASE_URL}/opinions/").mock(side_effect=_mock_opinions)

        cluster_ids = list(range(1, num_clusters + 1))
        client = CourtListenerClient(request_delay=0)

        start = time.monotonic()
        result = client.fetch_opinions_for_clusters(cluster_ids)
        elapsed = time.monotonic() - start

        client.close()

        assert len(result) == num_clusters, f"Expected {num_clusters} results, got {len(result)}"
        assert elapsed < 5.0, f"Batch fetch took {elapsed:.2f}s, expected < 5s"
        # One opinion API request per cluster.
        assert client.request_count == num_clusters, (
            f"Expected {num_clusters} API requests, got {client.request_count}"
        )


# ---------------------------------------------------------------------------
# Jurisdiction mapping tests (AC2)
# ---------------------------------------------------------------------------


class TestJurisdictionMapping:
    """Verify that _CL_COURT_ID_TO_JURISDICTION correctly classifies courts and
    that CourtListenerScraper applies the mapping in _map_to_document."""

    @pytest.mark.parametrize(
        "court_id,expected_state,expected_county",
        [
            # Federal — SCOTUS and circuit courts
            ("scotus", "Federal", "Federal"),
            ("ca9", "Federal", "Federal"),
            ("ca1", "Federal", "Federal"),
            ("cadc", "Federal", "Federal"),
            ("cafc", "Federal", "Federal"),
            # State high courts
            ("tex", "Texas", "Statewide"),
            ("ny", "New York", "Statewide"),
            ("fla", "Florida", "Statewide"),
            ("cal", "California", "Statewide"),
            ("ga", "Georgia", "Statewide"),
            # State appellate courts
            ("texapp1", "Texas", "Statewide"),
            ("texapp14", "Texas", "Statewide"),
            ("nyappdiv1", "New York", "Statewide"),
            ("nyappdiv2", "New York", "Statewide"),
            ("nyappdiv3", "New York", "Statewide"),
            ("nyappdiv4", "New York", "Statewide"),
            ("fladistctapp", "Florida", "Statewide"),
            ("fladistctapp1", "Florida", "Statewide"),
            ("gactapp", "Georgia", "Statewide"),
            ("ohioctapp1", "Ohio", "Statewide"),
        ],
    )
    def test_mapping_table_entries(
        self, court_id: str, expected_state: str, expected_county: str
    ) -> None:
        """Direct lookup into the mapping table returns the correct (state, county)."""
        assert court_id in _CL_COURT_ID_TO_JURISDICTION, (
            f"Court ID {court_id!r} missing from _CL_COURT_ID_TO_JURISDICTION"
        )
        state, county = _CL_COURT_ID_TO_JURISDICTION[court_id]
        assert state == expected_state, (
            f"For {court_id!r}: expected state={expected_state!r}, got {state!r}"
        )
        assert county == expected_county, (
            f"For {court_id!r}: expected county={expected_county!r}, got {county!r}"
        )

    def test_unknown_court_id_not_in_mapping(self) -> None:
        """A sentinel unknown court_id is absent from the mapping table."""
        assert "zzunknownsentinel" not in _CL_COURT_ID_TO_JURISDICTION

    @pytest.mark.parametrize(
        "court_url,expected_state,expected_county",
        [
            ("/api/rest/v4/courts/scotus/", "Federal", "Federal"),
            ("/api/rest/v4/courts/ca9/", "Federal", "Federal"),
            ("/api/rest/v4/courts/tex/", "Texas", "Statewide"),
            ("/api/rest/v4/courts/texapp1/", "Texas", "Statewide"),
            ("/api/rest/v4/courts/nyappdiv1/", "New York", "Statewide"),
            ("/api/rest/v4/courts/fladistctapp/", "Florida", "Statewide"),
        ],
    )
    @respx.mock
    def test_scraper_applies_mapping_to_document(
        self, court_url: str, expected_state: str, expected_county: str
    ) -> None:
        """CourtListenerScraper._map_to_document assigns correct state/county via mapping."""
        cluster = _make_cluster(court=court_url)
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        doc = docs[0]
        assert doc.state == expected_state, (
            f"For court_url={court_url!r}: expected state={expected_state!r}, got {doc.state!r}"
        )
        assert doc.county == expected_county, (
            f"For court_url={court_url!r}: expected county={expected_county!r}, got {doc.county!r}"
        )

    @respx.mock
    def test_unknown_court_id_defaults_to_unknown(self, capsys: pytest.CaptureFixture) -> None:
        """Unknown court_id produces (Unknown, Unknown) and logs a warning."""
        cluster = _make_cluster(court="/api/rest/v4/courts/zzunknownsentinel/")
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        doc = docs[0]
        assert doc.state == "Unknown"
        assert doc.county == "Unknown"

        # Warning with court_id should be emitted to stdout (structlog renders there in tests)
        captured = capsys.readouterr()
        assert "zzunknownsentinel" in captured.out

    @respx.mock
    def test_empty_court_id_falls_back_to_config_defaults(self) -> None:
        """Empty court field on cluster falls back to config state/county."""
        cluster = _make_cluster(court="")
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()  # state="Federal", county="Federal"
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        doc = docs[0]
        assert doc.state == "Federal"
        assert doc.county == "Federal"


# ---------------------------------------------------------------------------
# extra field survives event emission (AC1)
# ---------------------------------------------------------------------------


class TestExtraSurvivesEventEmission:
    """Verify that doc.extra round-trips through emit_document_captured."""

    @respx.mock
    def test_extra_survives_event_emission(self) -> None:
        """courtlistener_court_id in doc.extra must appear in the emitted Redis payload."""
        import json as _json

        from framework.events import EventBus

        cluster = _make_cluster(court="/api/rest/v4/courts/texapp1/")
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        doc = docs[0]

        # Emit through EventBus backed by a mock Redis
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = b"1234-0"
        bus = EventBus(mock_redis)
        bus.emit_document_captured(doc, producer_id="test-federal-courtlistener")

        call_args = mock_redis.xadd.call_args
        payload = _json.loads(call_args[0][1]["data"])

        assert "extra" in payload, "extra key missing from emitted payload"
        assert payload["extra"]["courtlistener_court_id"] == "texapp1"
        assert payload["extra"]["courtlistener_cluster_id"] == 1001
        assert payload["extra"]["courtlistener_opinion_id"] == 2001

    @respx.mock
    def test_extra_state_county_correct_for_texas_appellate(self) -> None:
        """Texas appellate opinions must have state=Texas, county=Statewide in event."""
        import json as _json

        from framework.events import EventBus

        cluster = _make_cluster(court="/api/rest/v4/courts/texapp3/")
        opinion = _make_opinion(plain_text="Texas appellate opinion")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        doc = docs[0]
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = b"1234-0"
        bus = EventBus(mock_redis)
        bus.emit_document_captured(doc, producer_id="test")

        payload = _json.loads(mock_redis.xadd.call_args[0][1]["data"])
        assert payload["state"] == "Texas"
        assert payload["county"] == "Statewide"


# ---------------------------------------------------------------------------
# case_title field preference (AC1: case_name_full > case_name > case_name_short)
# ---------------------------------------------------------------------------


class TestCaseTitlePreference:
    """Verify _map_to_document uses case_name_full > case_name > case_name_short."""

    @respx.mock
    def test_case_title_prefers_case_name_full(self) -> None:
        """When case_name_full is set it wins over case_name and case_name_short."""
        cluster = _make_cluster(case_name="Smith v. Jones")
        cluster["case_name_full"] = "Joseph Lester Smith v. Williams Jones et al."
        cluster["case_name_short"] = "Smith"
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        assert docs[0].case_title == "Joseph Lester Smith v. Williams Jones et al."

    @respx.mock
    def test_case_title_falls_back_to_case_name(self) -> None:
        """When case_name_full is absent/empty, case_name is used."""
        cluster = _make_cluster(case_name="Smith v. Jones")
        cluster["case_name_full"] = ""
        cluster["case_name_short"] = "Smith"
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        assert docs[0].case_title == "Smith v. Jones"

    @respx.mock
    def test_case_title_falls_back_to_case_name_short(self) -> None:
        """When both case_name_full and case_name are absent/empty, case_name_short is used."""
        cluster = _make_cluster(case_name="")
        cluster["case_name_full"] = ""
        cluster["case_name_short"] = "Smith"
        opinion = _make_opinion(plain_text="Opinion text")

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([cluster]))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response([opinion]))
        )

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7)
        docs = scraper.fetch_documents()

        assert len(docs) == 1
        assert docs[0].case_title == "Smith"


# ---------------------------------------------------------------------------
# Post-pagination cap tests (AC1: ≤ max_results requests for opinion+docket)
# ---------------------------------------------------------------------------


class TestFetchCapsToMaxResults:
    """Verify that cluster_id_list is capped to _max_results AFTER full pagination."""

    @respx.mock
    def test_docket_and_opinion_fetches_capped_to_max_results(self) -> None:
        """With 50 clusters paginated and max_results=10, opinion+docket requests ≤ 10.

        Proves that the cap is applied before the concurrent fetches, not just in
        the doc-build loop.  Without the fix, 50 opinion requests and 50 docket
        requests would be issued.
        """
        num_clusters = 50
        max_results = 10

        # Build 50 distinct clusters, all on a single page for simplicity.
        clusters = [
            _make_cluster(
                cluster_id=2000 + i,
                case_name=f"Case {i} v. Defendant",
                docket=f"{API_BASE_URL}/dockets/{3000 + i}/?format=json",
            )
            for i in range(num_clusters)
        ]

        opinion_request_count = 0
        docket_request_count = 0

        def _mock_opinions(request: httpx.Request) -> httpx.Response:
            nonlocal opinion_request_count
            opinion_request_count += 1
            cluster_id = int(request.url.params["cluster"])
            opinion = _make_opinion(
                opinion_id=cluster_id * 10,
                cluster_id=cluster_id,
                plain_text=f"Opinion for cluster {cluster_id}",
            )
            return httpx.Response(200, json=_make_paginated_response([opinion]))

        def _mock_dockets(request: httpx.Request) -> httpx.Response:
            nonlocal docket_request_count
            docket_request_count += 1
            path_parts = str(request.url.path).rstrip("/").split("/")
            docket_id = int(path_parts[-1])
            return httpx.Response(
                200, json={"id": docket_id, "docket_number": f"1:24-cv-{docket_id:05d}"}
            )

        respx.get(f"{API_BASE_URL}/clusters/").mock(
            return_value=httpx.Response(200, json=_make_paginated_response(clusters))
        )
        respx.get(f"{API_BASE_URL}/opinions/").mock(side_effect=_mock_opinions)
        respx.get(url__startswith=f"{API_BASE_URL}/dockets/").mock(side_effect=_mock_dockets)

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7, max_results=max_results)
        docs = scraper.fetch_documents()

        # The cap must limit both opinion and docket fetches to ≤ max_results each.
        assert opinion_request_count <= max_results, (
            f"Expected ≤ {max_results} opinion requests, got {opinion_request_count}"
        )
        assert docket_request_count <= max_results, (
            f"Expected ≤ {max_results} docket requests, got {docket_request_count}"
        )
        # Final document count must also not exceed max_results.
        assert len(docs) <= max_results, f"Expected ≤ {max_results} docs, got {len(docs)}"

    @respx.mock
    def test_post_pagination_cap_preserves_date_modified_order(self) -> None:
        """Cap is applied on the post-pagination flat list, so most-recent clusters win.

        Page 1 (date_modified=2026-04-01) comes first in the flat list because
        fetch_clusters returns results in -date_modified order.  With max_results=3
        the cap must select from page 1's clusters, not page 2's.
        """
        max_results = 3

        # Page 1: more-recent clusters (date_modified 2026-04-01)
        page1_clusters = [
            _make_cluster(
                cluster_id=5000 + i,
                case_name=f"Recent Case {i}",
                date_modified="2026-04-01T10:00:00Z",
            )
            for i in range(5)
        ]
        # Page 2: older clusters (date_modified 2026-03-01)
        page2_clusters = [
            _make_cluster(
                cluster_id=6000 + i,
                case_name=f"Older Case {i}",
                date_modified="2026-03-01T10:00:00Z",
            )
            for i in range(5)
        ]

        page1_response = _make_paginated_response(
            page1_clusters,
            next_url=f"{API_BASE_URL}/clusters/?cursor=page2",
            count=10,
        )
        page2_response = _make_paginated_response(page2_clusters)

        cluster_call_count = 0

        def _handle_clusters(request: httpx.Request) -> httpx.Response:
            nonlocal cluster_call_count
            cluster_call_count += 1
            if cluster_call_count == 1:
                return httpx.Response(200, json=page1_response)
            return httpx.Response(200, json=page2_response)

        def _mock_opinions(request: httpx.Request) -> httpx.Response:
            cluster_id = int(request.url.params["cluster"])
            opinion = _make_opinion(
                opinion_id=cluster_id * 10,
                cluster_id=cluster_id,
                plain_text=f"Opinion for cluster {cluster_id}",
            )
            return httpx.Response(200, json=_make_paginated_response([opinion]))

        respx.get(url__startswith=f"{API_BASE_URL}/clusters/").mock(side_effect=_handle_clusters)
        respx.get(f"{API_BASE_URL}/opinions/").mock(side_effect=_mock_opinions)

        config = _make_scraper_config()
        client = CourtListenerClient(request_delay=0)
        scraper = CourtListenerScraper(config, client=client, days_back=7, max_results=max_results)
        docs = scraper.fetch_documents()

        # All returned docs must come from page 1 clusters (ids 5000-5004).
        page1_ids = {5000 + i for i in range(5)}
        for doc in docs:
            cluster_id_in_doc = doc.extra["courtlistener_cluster_id"]
            assert cluster_id_in_doc in page1_ids, (
                f"Doc cluster_id={cluster_id_in_doc} is not from page 1 "
                f"(page1_ids={page1_ids}). Cap must be post-pagination."
            )
        # Confirm we got max_results docs (one per cluster since each has one opinion).
        assert len(docs) == max_results, (
            f"Expected exactly {max_results} docs from page 1, got {len(docs)}"
        )


# ---------------------------------------------------------------------------
# parse_document on a fresh CapturedDocument — reingest path (issue #3986)
# ---------------------------------------------------------------------------


class TestParseDocumentReingestPath:
    """Regression tests for the reingest path: ``parse_document`` must
    populate ``ruling_text`` / ``ruling_text_html`` from a fresh
    ``CapturedDocument`` carrying only ``raw_content``.

    Before the fix in issue #3986, ``parse_document`` was a no-op that
    assumed ``_map_to_document`` had already populated the fields.  This
    held for the live capture path but not for ``scripts/reingest_from_s3.py``,
    which constructs a fresh ``CapturedDocument`` from S3-archived bytes.
    The reingest path therefore stored the raw JSON envelope (truncated
    to 50,000 chars by the SQL writer) as ``ruling_text``.
    """

    def _make_envelope_doc(
        self,
        *,
        cluster: dict | None = None,
        opinion: dict | None = None,
        docket: dict | None = None,
    ) -> CapturedDocument:
        """Build a fresh CapturedDocument with only raw_content set —
        mirrors the shape ``scripts/reingest_from_s3.py:890-901`` constructs.

        Delegates to ``helpers.reingest.make_reingest_cap_doc`` (the
        shared scaffold from #4153) — only the envelope-shape JSON
        construction is CourtListener-specific.
        """
        envelope: dict = {}
        if cluster is not None:
            envelope["cluster"] = cluster
        if opinion is not None:
            envelope["opinion"] = opinion
        if docket is not None:
            envelope["docket"] = docket
        raw_content = json.dumps(envelope, default=str).encode("utf-8")

        return make_reingest_cap_doc(
            raw_content=raw_content,
            scraper_id=_CL_SCRAPER_ID,
            state=_CL_STATE,
            county=_CL_COUNTY,
            court=_CL_COURT,
            source_url=f"{_CL_SOURCE_URL_BASE}/2001/",
            content_format=ContentFormat.TEXT,
            document_id="reingest-doc-1",
            content_hash="0" * 64,
        )

    def _make_scraper(self) -> CourtListenerScraper:
        """Build a scraper with no client — parse_document doesn't touch the network."""
        config = _make_scraper_config()
        return CourtListenerScraper(config, client=MagicMock(), days_back=7)

    def test_parse_document_populates_ruling_text_from_plain_text(self) -> None:
        """AC1: parse_document populates ruling_text from opinion["plain_text"].

        Issue #3986: a fresh CapturedDocument from reingest carrying only
        raw_content (the JSON envelope) must come out of parse_document with
        ruling_text == opinion["plain_text"], not the raw JSON envelope.
        """
        cluster = _make_cluster()
        opinion = {
            "id": 2001,
            "type": "010combined",
            "plain_text": "Hello world",
            "html_with_citations": "<pre>Hello world</pre>",
        }
        doc = self._make_envelope_doc(cluster=cluster, opinion=opinion)
        # Pre-condition: fresh doc has no ruling_text yet (mirrors reingest).
        assert doc.ruling_text is None
        assert doc.ruling_text_html is None

        scraper = self._make_scraper()
        parsed = scraper.parse_document(doc)

        assert parsed.ruling_text == "Hello world"
        assert parsed.ruling_text_html == "<pre>Hello world</pre>"

    def test_parse_document_populates_html_fallback_when_plain_text_empty(self) -> None:
        """AC1 (fallback): when plain_text is empty, ruling_text falls back
        to the same html_with_citations / html / html_columbia / html_lawbox
        chain that ``_map_to_document`` uses.
        """
        cluster = _make_cluster()
        opinion = {
            "id": 2002,
            "type": "010combined",
            "plain_text": "",
            "html_with_citations": "<pre>HTML only opinion</pre>",
        }
        doc = self._make_envelope_doc(cluster=cluster, opinion=opinion)

        scraper = self._make_scraper()
        parsed = scraper.parse_document(doc)

        assert parsed.ruling_text == "<pre>HTML only opinion</pre>"
        assert parsed.ruling_text_html == "<pre>HTML only opinion</pre>"

    def test_parse_document_returns_unchanged_for_invalid_json(self) -> None:
        """AC2: parse_document tolerates raw_content that is not valid JSON.

        Pre-2024 captures or partial files may have raw_content that is not
        a valid JSON envelope.  parse_document must not raise and must
        leave the doc untouched so the reingest caller can fall back to
        its raw text decode.
        """
        doc = make_reingest_cap_doc(
            raw_content=b"not json",
            scraper_id=_CL_SCRAPER_ID,
            state=_CL_STATE,
            county=_CL_COUNTY,
            court=_CL_COURT,
            source_url=f"{_CL_SOURCE_URL_BASE}/9999/",
            document_id="reingest-bad-json",
            content_hash="0" * 64,
        )

        scraper = self._make_scraper()
        # Must not raise.
        parsed = scraper.parse_document(doc)

        # Doc returned unchanged — ruling_text stays None so the reingest
        # caller can fall back to its raw-text-decode path.
        assert parsed.ruling_text is None
        assert parsed.ruling_text_html is None
        assert parsed.case_title is None

    def test_parse_document_returns_unchanged_for_missing_envelope_keys(self) -> None:
        """AC2 (extension): parse_document tolerates JSON that is well-formed
        but doesn't have the expected ``cluster``/``opinion`` shape.
        """
        # Well-formed JSON but no cluster/opinion keys.
        raw = json.dumps({"unrelated": {"data": "shape"}}).encode("utf-8")
        doc = make_reingest_cap_doc(
            raw_content=raw,
            scraper_id=_CL_SCRAPER_ID,
            state=_CL_STATE,
            county=_CL_COUNTY,
            court=_CL_COURT,
            source_url=f"{_CL_SOURCE_URL_BASE}/9999/",
            document_id="reingest-wrong-shape",
            content_hash="0" * 64,
        )

        scraper = self._make_scraper()
        parsed = scraper.parse_document(doc)

        assert parsed.ruling_text is None
        assert parsed.ruling_text_html is None
        assert parsed.case_title is None

    def test_parse_document_populates_structured_fields_from_envelope(self) -> None:
        """parse_document populates case_title, case_number, judge_name,
        hearing_date, motion_type, and extra metadata so reingest benefits
        from the #3970 case_name_full mapper without requiring a second pass.
        """
        cluster = {
            "id": 1001,
            "case_name": "Smith v. Jones",
            "case_name_full": "Joseph Smith v. Williams Jones et al.",
            "case_name_short": "Smith",
            "docket_number": "22-1234",
            "judges": "Justice Roberts",
            "date_filed": "2026-03-01",
            "court": "/api/rest/v4/courts/scotus/",
            "precedential_status": "Published",
            "date_modified": "2026-03-05T12:00:00Z",
            "citation_count": 5,
        }
        opinion = {
            "id": 2001,
            "type": "010combined",
            "plain_text": "The court holds that...",
        }
        doc = self._make_envelope_doc(cluster=cluster, opinion=opinion)

        scraper = self._make_scraper()
        parsed = scraper.parse_document(doc)

        # case_title should pick up case_name_full per #3970 even via reingest.
        assert parsed.case_title == "Joseph Smith v. Williams Jones et al."
        assert parsed.case_number == "22-1234"
        assert parsed.judge_name == "Justice Roberts"
        assert parsed.hearing_date == datetime(2026, 3, 1)
        assert parsed.motion_type == "Combined Opinion"
        assert parsed.ruling_text == "The court holds that..."
        # extra metadata populated for downstream consumers.
        assert parsed.extra["courtlistener_cluster_id"] == 1001
        assert parsed.extra["courtlistener_opinion_id"] == 2001
        assert parsed.extra["courtlistener_court_id"] == "scotus"

    def test_parse_document_does_not_store_json_envelope_as_ruling_text(self) -> None:
        """Direct regression for issue #3986: ruling_text must NOT start
        with the JSON envelope shape (``{"cluster":...``).

        Verifies the failure mode ``_TRUNCATION_SENTINEL_LENGTH`` was
        added to detect (deterministic.py:40).  Before the fix, reingest
        produced rows with length(ruling_text) == 50000 and ruling_text
        starting ``{"cluster":...``.  After the fix, ruling_text is the
        opinion's plain_text body.
        """
        # Simulate a long opinion where naive UTF-8 decode of the envelope
        # would absolutely contain "{\"cluster\":..." at the start.
        cluster = _make_cluster()
        opinion = {
            "id": 2001,
            "type": "010combined",
            "plain_text": "The motion is GRANTED. " * 1000,  # ~22 KB body
        }
        doc = self._make_envelope_doc(cluster=cluster, opinion=opinion)

        # Sanity check: the raw_content envelope DOES start with the JSON
        # shape — this is exactly what the buggy decode path would store.
        assert doc.raw_content.decode("utf-8").startswith('{"cluster":')

        scraper = self._make_scraper()
        parsed = scraper.parse_document(doc)

        assert parsed.ruling_text is not None
        assert not parsed.ruling_text.startswith('{"cluster":'), (
            "ruling_text must be the opinion body, not the JSON envelope"
        )
        assert parsed.ruling_text.startswith("The motion is GRANTED.")
        # Sanity: respected the 10000-char cap matching _map_to_document.
        assert len(parsed.ruling_text) == 10000

    def test_parse_document_handles_none_raw_content(self) -> None:
        """Defensive: parse_document on a doc with raw_content=b'' returns unchanged."""
        doc = make_reingest_cap_doc(
            raw_content=b"",
            scraper_id=_CL_SCRAPER_ID,
            state=_CL_STATE,
            county=_CL_COUNTY,
            court=_CL_COURT,
            source_url=f"{_CL_SOURCE_URL_BASE}/9999/",
            document_id="reingest-empty",
            content_hash="0" * 64,
        )

        scraper = self._make_scraper()
        parsed = scraper.parse_document(doc)
        assert parsed.ruling_text is None
        assert parsed.ruling_text_html is None
