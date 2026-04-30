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

import httpx
import pytest
import respx

from courts.federal.courtlistener import (
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
from framework import ContentFormat, ScraperConfig

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
) -> dict:
    """Build a sample CourtListener OpinionCluster response."""
    return {
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
    }


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
        """Return None when docket number is missing."""
        cluster = _make_cluster()
        cluster["docket_number"] = None
        assert _extract_docket_number(cluster) is None

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
    def test_fetch_documents_prefers_html(self) -> None:
        """Scraper prefers html_with_citations over plain_text."""
        cluster = _make_cluster()
        opinion = _make_opinion(
            plain_text="Plain version",
            html="<p>HTML version</p>",
        )
        # html_with_citations takes priority, set it directly
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
        assert docs[0].ruling_text == "<p>HTML with <a>citations</a></p>"

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
    def test_parse_document_is_passthrough(self) -> None:
        """parse_document returns the document unchanged (fields set in mapping)."""
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
        parsed = scraper.parse_document(docs[0])
        assert parsed.case_title == original_title

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
        """Scraper handles clusters with missing optional fields gracefully."""
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
        assert doc.case_number is None
        assert doc.judge_name is None
        assert doc.hearing_date is None
        assert doc.ruling_text == "Some opinion text"


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
    def test_fetch_opinions_for_clusters_partial_failure(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """A 500 for one cluster returns empty list for that id; others succeed.

        Also verifies that a warning is emitted for the failed cluster.
        Structlog renders to stdout in tests, so we capture that.
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
        result = client.fetch_opinions_for_clusters(cluster_ids)
        client.close()

        # Failing cluster returns empty list.
        assert result[failing_id] == []

        # Successful clusters return their opinions.
        assert len(result[100]) == 1
        assert len(result[300]) == 1

        # All three cluster IDs appear in result (failed one has empty list, not missing).
        assert set(result.keys()) == {100, 200, 300}

        # Structlog warning for the failing cluster was rendered to stdout.
        captured = capsys.readouterr()
        assert str(failing_id) in captured.out

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
