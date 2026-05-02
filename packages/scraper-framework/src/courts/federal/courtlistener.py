"""CourtListener integration — federal court opinions via Free Law Project API.

CourtListener (https://www.courtlistener.com/) provides free access to millions
of legal opinions from federal and state courts via a REST API.

API details:
  Base URL: https://www.courtlistener.com/api/rest/v4/
  Rate limit: 5,000 requests/day unauthenticated; more with a free API token.
  Authentication: optional Bearer token via COURTLISTENER_API_TOKEN env var.
  Pagination: cursor-based (next/previous URLs in response).

Data flow:
  1. Fetch recent OpinionClusters (filtered by date_modified)
  2. For each cluster, fetch the associated Opinion(s)
  3. Map cluster + opinion data to CapturedDocument
  4. BaseScraper handles hashing, S3 archival, and event emission

Architecture spec reference: §3.4 — CourtListener provides significant
baseline coverage from day one for federal opinions and state appellate data.

Issue: #158
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from framework import BaseScraper, CapturedDocument, ContentFormat, ScheduleWindow, ScraperConfig

logger = structlog.get_logger(__name__)

API_BASE_URL = "https://www.courtlistener.com/api/rest/v4"

# Courtesy delay between API requests (seconds).  CourtListener allows
# 5,000 req/day unauthenticated (~3.5 req/min averaged), so 2 s between
# requests is well within limits even during bursts.
DEFAULT_REQUEST_DELAY = 2.0

# Maximum number of opinion clusters to fetch per run.  This caps the total
# number of API requests to roughly 2 * max_results (one for clusters list,
# one per cluster for its opinions).
DEFAULT_MAX_RESULTS = 100

# Maximum pages of cluster results to paginate through.
DEFAULT_MAX_PAGES = 10

# Maximum concurrent opinion fetches.  CourtListener's soft rate limit is
# ~5 req/s; combined with the 0.2 s sleep inside each semaphore slot this
# keeps us safely under that ceiling even for bursts.
DEFAULT_OPINION_FETCH_CONCURRENCY = 5


class CourtListenerClient:
    """HTTP client wrapper for the CourtListener REST API v4.

    Handles authentication, rate limiting, and cursor-based pagination.
    """

    def __init__(
        self,
        *,
        api_token: str | None = None,
        request_delay: float = DEFAULT_REQUEST_DELAY,
        timeout: float = 30.0,
    ) -> None:
        self._api_token = api_token or os.environ.get("COURTLISTENER_API_TOKEN", "")
        self._request_delay = request_delay
        self._timeout = timeout
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._api_token:
            headers["Authorization"] = f"Token {self._api_token}"
        else:
            logger.warning(
                "No CourtListener API token configured. "
                "Set COURTLISTENER_API_TOKEN env var. "
                "Requests will fail if the API requires authentication.",
            )
        self._client = httpx.Client(
            base_url=API_BASE_URL,
            headers=headers,
            timeout=self._timeout,
        )
        self._request_count = 0

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request with rate limiting.

        Args:
            url: Relative path (e.g. "/opinions/") or absolute URL (for pagination).
            params: Query parameters (ignored for absolute URLs from pagination).

        Returns:
            Parsed JSON response as a dict.
        """
        if self._request_count > 0:
            time.sleep(self._request_delay)
        self._request_count += 1

        # Absolute URLs from pagination next/previous links
        if url.startswith("http"):
            response = self._client.get(url)
        else:
            response = self._client.get(url, params=params)

        if response.status_code == 401:
            token_status = "configured" if self._api_token else "NOT configured"
            logger.error(
                "CourtListener API returned 401 Unauthorized. "
                "The API token may be expired, revoked, or missing. "
                "Update the COURTLISTENER_API_TOKEN secret in AWS Secrets Manager.",
                api_token_status=token_status,
                url=str(response.url),
            )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def fetch_clusters(
        self,
        *,
        date_modified_after: str,
        max_pages: int = DEFAULT_MAX_PAGES,
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        """Fetch OpinionClusters modified after a given date.

        Uses cursor-based pagination to collect results across multiple pages.

        Args:
            date_modified_after: ISO-8601 date string (e.g. "2026-03-01").
            max_pages: Maximum number of pages to fetch.
            page_size: Number of results per page (max 20 for CL API).

        Returns:
            List of cluster dicts from the API.
        """
        clusters: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "date_modified__gte": date_modified_after,
            "order_by": "-date_modified",
            "page_size": min(page_size, 20),  # CL API caps at 20
            "format": "json",
        }

        next_url: str | None = "/clusters/"
        pages_fetched = 0

        while next_url and pages_fetched < max_pages:
            if pages_fetched == 0:
                data = self._get(next_url, params=params)
            else:
                # Subsequent pages use the absolute 'next' URL from the response
                data = self._get(next_url)

            results = data.get("results", [])
            clusters.extend(results)
            next_url = data.get("next")
            pages_fetched += 1

            logger.debug(
                "Fetched cluster page",
                page=pages_fetched,
                results_on_page=len(results),
                total_so_far=len(clusters),
            )

        return clusters

    def fetch_opinions_for_cluster(self, cluster_id: int) -> list[dict[str, Any]]:
        """Fetch all opinions belonging to a specific cluster.

        Args:
            cluster_id: The CourtListener cluster ID.

        Returns:
            List of opinion dicts.
        """
        params: dict[str, Any] = {
            "cluster": cluster_id,
            "format": "json",
        }
        data = self._get("/opinions/", params=params)
        results: list[dict[str, Any]] = data.get("results", [])
        return results

    def fetch_opinions_for_clusters(
        self,
        cluster_ids: list[int],
    ) -> dict[int, list[dict[str, Any]]]:
        """Fetch opinions for multiple clusters concurrently.

        Uses bounded async concurrency (up to DEFAULT_OPINION_FETCH_CONCURRENCY
        simultaneous requests) so that a run with 50+ clusters completes in
        seconds rather than minutes.  Per-cluster HTTP errors are swallowed
        and logged as warnings; the affected id maps to an empty list so the
        caller loop sees the same shape it would from a successful fetch.

        Args:
            cluster_ids: List of CourtListener cluster IDs to fetch.

        Returns:
            Dict mapping cluster_id -> list of opinion dicts.
        """
        return asyncio.run(self._afetch_opinions_for_clusters(cluster_ids))

    async def _afetch_opinions_for_clusters(
        self,
        cluster_ids: list[int],
    ) -> dict[int, list[dict[str, Any]]]:
        """Async implementation of bounded-concurrency opinion batch fetch."""
        semaphore = asyncio.Semaphore(DEFAULT_OPINION_FETCH_CONCURRENCY)
        headers = dict(self._client.headers)

        async def _fetch_one(
            async_client: httpx.AsyncClient,
            cluster_id: int,
        ) -> tuple[int, list[dict[str, Any]]]:
            async with semaphore:
                # 0.2 s pacing inside the semaphore slot keeps throughput at
                # most 5 req/s even when responses are instantaneous.
                await asyncio.sleep(0.2)
                try:
                    response = await async_client.get(
                        "/opinions/",
                        params={"cluster": cluster_id, "format": "json"},
                    )
                    response.raise_for_status()
                    data: dict[str, Any] = response.json()
                    self._request_count += 1
                    return cluster_id, data.get("results", [])
                except httpx.HTTPStatusError as exc:
                    self._request_count += 1
                    logger.warning(
                        "Failed to fetch opinions for cluster",
                        cluster_id=cluster_id,
                        status=exc.response.status_code,
                    )
                    return cluster_id, []

        async with httpx.AsyncClient(
            base_url=API_BASE_URL,
            headers=headers,
            timeout=self._timeout,
        ) as async_client:
            tasks = [_fetch_one(async_client, cid) for cid in cluster_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        output: dict[int, list[dict[str, Any]]] = {}
        for item in results:
            if isinstance(item, BaseException):
                # Unexpected exception — log and skip so caller loop is robust.
                logger.warning("Unexpected error fetching opinions batch", exc_info=item)
                continue
            cid, opinions = item
            output[cid] = opinions
        return output

    def fetch_docket(self, docket_url: str) -> dict[str, Any]:
        """Fetch a single docket sub-resource by its absolute URL.

        Args:
            docket_url: Absolute URL to the docket resource
                        (e.g. ``https://www.courtlistener.com/api/rest/v4/dockets/123/``).

        Returns:
            Parsed docket dict from the API.
        """
        return self._get(docket_url)

    def fetch_dockets_for_clusters(
        self,
        cluster_id_to_docket_url: dict[int, str],
    ) -> dict[int, dict[str, Any]]:
        """Fetch dockets for multiple clusters concurrently.

        Uses bounded async concurrency identical to ``fetch_opinions_for_clusters``
        (semaphore=DEFAULT_OPINION_FETCH_CONCURRENCY, 0.2 s pacing).  Per-cluster
        HTTP errors are swallowed and logged as warnings; the affected id maps to
        an empty dict so the caller loop sees a consistent shape.

        Args:
            cluster_id_to_docket_url: Mapping of cluster_id -> absolute docket URL.

        Returns:
            Dict mapping cluster_id -> docket dict (or {} on error).
        """
        return asyncio.run(self._afetch_dockets_for_clusters(cluster_id_to_docket_url))

    async def _afetch_dockets_for_clusters(
        self,
        cluster_id_to_docket_url: dict[int, str],
    ) -> dict[int, dict[str, Any]]:
        """Async implementation of bounded-concurrency docket batch fetch."""
        semaphore = asyncio.Semaphore(DEFAULT_OPINION_FETCH_CONCURRENCY)
        headers = dict(self._client.headers)

        async def _fetch_one(
            async_client: httpx.AsyncClient,
            cluster_id: int,
            docket_url: str,
        ) -> tuple[int, dict[str, Any]]:
            async with semaphore:
                # 0.2 s pacing inside the semaphore slot keeps throughput at
                # most 5 req/s even when responses are instantaneous.
                await asyncio.sleep(0.2)
                try:
                    response = await async_client.get(docket_url)
                    response.raise_for_status()
                    data: dict[str, Any] = response.json()
                    self._request_count += 1
                    return cluster_id, data
                except httpx.HTTPStatusError as exc:
                    self._request_count += 1
                    logger.warning(
                        "Failed to fetch docket for cluster",
                        cluster_id=cluster_id,
                        docket_url=docket_url,
                        status=exc.response.status_code,
                    )
                    return cluster_id, {}

        async with httpx.AsyncClient(
            headers=headers,
            timeout=self._timeout,
        ) as async_client:
            tasks = [
                _fetch_one(async_client, cluster_id, docket_url)
                for cluster_id, docket_url in cluster_id_to_docket_url.items()
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        output: dict[int, dict[str, Any]] = {}
        for item in results:
            if isinstance(item, BaseException):
                logger.warning("Unexpected error fetching dockets batch", exc_info=item)
                continue
            cid, docket = item
            output[cid] = docket
        return output

    @property
    def request_count(self) -> int:
        """Total number of API requests made by this client instance."""
        return self._request_count


class CourtListenerScraper(BaseScraper):
    """Scraper for federal court opinions via the CourtListener API.

    Fetches recent opinion clusters and their associated opinions, maps
    them to CapturedDocument instances, and lets BaseScraper handle the
    downstream pipeline (hashing, S3 archival, event emission).
    """

    def __init__(
        self,
        config: ScraperConfig,
        *,
        days_back: int = 7,
        max_results: int = DEFAULT_MAX_RESULTS,
        max_pages: int = DEFAULT_MAX_PAGES,
        client: CourtListenerClient | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        self._days_back = days_back
        self._max_results = max_results
        self._max_pages = max_pages
        self._client = client

    def fetch_documents(self) -> list[CapturedDocument]:
        """Fetch recent opinions from CourtListener and map to CapturedDocuments."""
        client = self._client or CourtListenerClient(
            request_delay=self.config.request_delay_seconds,
            timeout=self.config.request_timeout_seconds,
        )
        owns_client = self._client is None

        try:
            return self._fetch_with_client(client)
        finally:
            if owns_client:
                client.close()

    def _fetch_with_client(self, client: CourtListenerClient) -> list[CapturedDocument]:
        """Internal fetch logic using a provided client."""
        cutoff = datetime.now(UTC) - timedelta(days=self._days_back)
        cutoff_str = cutoff.strftime("%Y-%m-%d")

        self._log.info(
            "Fetching clusters from CourtListener",
            days_back=self._days_back,
            cutoff=cutoff_str,
        )

        clusters = client.fetch_clusters(
            date_modified_after=cutoff_str,
            max_pages=self._max_pages,
        )

        self._log.info("Fetched clusters", count=len(clusters))

        # Collect all valid cluster IDs upfront, then batch-fetch their
        # opinions with bounded concurrency.
        cluster_id_list: list[int] = [c["id"] for c in clusters if c.get("id")]
        cluster_by_id: dict[int, dict[str, Any]] = {c["id"]: c for c in clusters if c.get("id")}

        opinions_by_cluster = client.fetch_opinions_for_clusters(cluster_id_list)

        # Build docket URL map: skip clusters with no docket URL
        cluster_id_to_docket_url: dict[int, str] = {
            c["id"]: c["docket"] for c in clusters if c.get("id") and c.get("docket")
        }
        dockets_by_cluster = client.fetch_dockets_for_clusters(cluster_id_to_docket_url)

        docs: list[CapturedDocument] = []
        for cluster_id in cluster_id_list:
            if len(docs) >= self._max_results:
                break

            cluster = cluster_by_id[cluster_id]
            opinions = opinions_by_cluster.get(cluster_id, [])
            docket = dockets_by_cluster.get(cluster_id)

            for opinion in opinions:
                if len(docs) >= self._max_results:
                    break

                doc = self._map_to_document(cluster, opinion, docket=docket)
                if doc is not None:
                    docs.append(doc)

        self._log.info(
            "CourtListener fetch complete",
            documents=len(docs),
            api_requests=client.request_count,
        )
        return docs

    def _map_to_document(
        self,
        cluster: dict[str, Any],
        opinion: dict[str, Any],
        docket: dict[str, Any] | None = None,
    ) -> CapturedDocument | None:
        """Map a CourtListener cluster + opinion to a CapturedDocument.

        Args:
            cluster: OpinionCluster API response dict.
            opinion: Opinion API response dict.
            docket: Resolved docket sub-resource dict (optional).

        Returns:
            A CapturedDocument, or None if the opinion has no usable content.
        """
        # CourtListener opinions ship two parallel representations:
        #   - plain_text:           clean text, no markup (what we want for ruling_text)
        #   - html_with_citations:  same text wrapped in <pre class="inline"> with
        #                           inline <span class="citation"> links
        # Storing html_with_citations into ruling_text leaves <pre>/<span> markup
        # in the canonical text column (derived.rulings.ruling_text); the schema
        # already has a separate ruling_text_html column for the rich version.
        plain_text_value = opinion.get("plain_text") or ""
        html_text_value = (
            opinion.get("html_with_citations")
            or opinion.get("html")
            or opinion.get("html_columbia")
            or opinion.get("html_lawbox")
            or ""
        )

        if not plain_text_value and not html_text_value:
            return None

        canonical_text = plain_text_value or html_text_value

        raw_content = json.dumps(
            {"cluster": cluster, "opinion": opinion},
            default=str,
        ).encode("utf-8")

        # Build source URL from the opinion's absolute_url or resource_uri
        opinion_id = opinion.get("id", "unknown")
        source_url = f"https://www.courtlistener.com/api/rest/v4/opinions/{opinion_id}/"

        # Extract court identifier from cluster
        court_id = cluster.get("court", "") or ""
        # court field is typically a URL like "/api/rest/v4/courts/scotus/"
        # Extract the short ID from the URL path
        if "/" in court_id:
            parts = court_id.rstrip("/").split("/")
            court_id = parts[-1] if parts else court_id

        # Resolve jurisdiction from court_id mapping.
        # Default to ("Unknown", "Unknown") on miss and emit a structured warning
        # so unknown IDs surface in CloudWatch rather than silently inheriting
        # the scraper-level "Federal" defaults.
        if court_id in _CL_COURT_ID_TO_JURISDICTION:
            resolved_state, resolved_county = _CL_COURT_ID_TO_JURISDICTION[court_id]
        elif court_id:
            resolved_state, resolved_county = ("Unknown", "Unknown")
            logger.warning(
                "Unknown CourtListener court_id — defaulting to Unknown jurisdiction",
                courtlistener_court_id=court_id,
            )
        else:
            # Empty court_id — fall back to config defaults (Federal/Federal for the
            # standard federal-courtlistener scraper) rather than Unknown.
            resolved_state = self.config.state
            resolved_county = self.config.county

        doc = self._make_base_doc(
            source_url=source_url,
            raw_content=raw_content,
            content_format=ContentFormat.TEXT,
        )

        # Override state/county with the resolved jurisdiction
        doc.state = resolved_state
        doc.county = resolved_county

        # Map structured fields
        doc.case_title = (
            cluster.get("case_name_full")
            or cluster.get("case_name")
            or cluster.get("case_name_short")
            or None
        )
        doc.case_number = _extract_docket_number(cluster, docket=docket)
        doc.judge_name = _extract_judge_names(cluster)
        doc.hearing_date = _parse_date(cluster.get("date_filed"))
        doc.ruling_text = canonical_text[:10000] if canonical_text else None
        doc.ruling_text_html = html_text_value[:10000] if html_text_value else None
        doc.outcome = None  # CourtListener opinions don't have a simple outcome field
        doc.motion_type = _map_opinion_type(opinion.get("type"))

        # Store CourtListener-specific metadata in extra
        doc.extra = {
            "courtlistener_cluster_id": cluster.get("id"),
            "courtlistener_opinion_id": opinion.get("id"),
            "courtlistener_court_id": court_id,
            "courtlistener_docket_id": docket.get("id") if docket else None,
            "date_modified": cluster.get("date_modified"),
            "precedential_status": cluster.get("precedential_status"),
            "citation_count": cluster.get("citation_count", 0),
        }

        return doc

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        """Parse structured fields from doc.raw_content.

        For CourtListener, most parsing happens in _map_to_document() because
        the API returns structured JSON.  This method performs any additional
        cleanup or enrichment.
        """
        # Fields are already populated in _map_to_document.
        # The raw_content is the full JSON payload; ruling_text was extracted
        # during mapping.  No additional parsing needed.
        return doc


def _extract_docket_number(
    cluster: dict[str, Any],
    docket: dict[str, Any] | None = None,
) -> str:
    """Extract docket number from a cluster and/or its resolved docket sub-resource.

    Resolution order:
    1. docket['docket_number'] if docket is non-empty and the field is truthy.
    2. cluster['docket_number'] if truthy.
    3. f"CL-cluster-{cluster['id']}" — deterministic fallback, never None.

    Args:
        cluster: OpinionCluster API response dict.
        docket: Resolved docket sub-resource dict (optional).

    Returns:
        A non-None docket number string.
    """
    if docket and docket.get("docket_number"):
        return str(docket["docket_number"])
    docket_number = cluster.get("docket_number")
    if docket_number:
        return str(docket_number)
    return f"CL-cluster-{cluster['id']}"


def _extract_judge_names(cluster: dict[str, Any]) -> str | None:
    """Extract judge name(s) from a cluster.

    The judges field in a cluster response contains the full text of the
    judge attribution (e.g. "Per Curiam" or "Justice Sotomayor").
    """
    judges = cluster.get("judges")
    if judges and isinstance(judges, str):
        return judges.strip()
    return None


def _parse_date(date_str: str | None) -> datetime | None:
    """Parse an ISO-8601 date string to a datetime."""
    if not date_str:
        return None
    try:
        # Handle both "YYYY-MM-DD" and full ISO timestamps
        if "T" in date_str:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Court ID → (state, county) jurisdiction mapping
# ---------------------------------------------------------------------------

#: Maps CourtListener short court IDs to (state, county) tuples.
#:
#: Federal courts map to ("Federal", "Federal").
#: State high courts and appellate courts map to ("<State>", "Statewide").
#: On a miss, the scraper defaults to ("Unknown", "Unknown") and logs a warning.
#:
#: Source: https://www.courtlistener.com/api/rest/v4/courts/?format=json
_CL_COURT_ID_TO_JURISDICTION: dict[str, tuple[str, str]] = {
    # Supreme Court of the United States
    "scotus": ("Federal", "Federal"),
    # Federal Circuit Courts of Appeals
    "ca1": ("Federal", "Federal"),
    "ca2": ("Federal", "Federal"),
    "ca3": ("Federal", "Federal"),
    "ca4": ("Federal", "Federal"),
    "ca5": ("Federal", "Federal"),
    "ca6": ("Federal", "Federal"),
    "ca7": ("Federal", "Federal"),
    "ca8": ("Federal", "Federal"),
    "ca9": ("Federal", "Federal"),
    "ca10": ("Federal", "Federal"),
    "ca11": ("Federal", "Federal"),
    "cadc": ("Federal", "Federal"),  # DC Circuit
    "cafc": ("Federal", "Federal"),  # Federal Circuit
    # Federal District Courts (select — catches common ones)
    "dcd": ("Federal", "Federal"),
    "almd": ("Federal", "Federal"),
    "alnd": ("Federal", "Federal"),
    "alsd": ("Federal", "Federal"),
    "akd": ("Federal", "Federal"),
    "azd": ("Federal", "Federal"),
    "ared": ("Federal", "Federal"),
    "arwd": ("Federal", "Federal"),
    "cacd": ("Federal", "Federal"),
    "caed": ("Federal", "Federal"),
    "cand": ("Federal", "Federal"),
    "casd": ("Federal", "Federal"),
    "cod": ("Federal", "Federal"),
    "ctd": ("Federal", "Federal"),
    "ded": ("Federal", "Federal"),
    "flmd": ("Federal", "Federal"),
    "flnd": ("Federal", "Federal"),
    "flsd": ("Federal", "Federal"),
    "gamd": ("Federal", "Federal"),
    "gand": ("Federal", "Federal"),
    "gasd": ("Federal", "Federal"),
    "hid": ("Federal", "Federal"),
    "idd": ("Federal", "Federal"),
    "ilcd": ("Federal", "Federal"),
    "ilnd": ("Federal", "Federal"),
    "ilsd": ("Federal", "Federal"),
    "innd": ("Federal", "Federal"),
    "insd": ("Federal", "Federal"),
    "iand": ("Federal", "Federal"),
    "iasd": ("Federal", "Federal"),
    "ksd": ("Federal", "Federal"),
    "kyed": ("Federal", "Federal"),
    "kywd": ("Federal", "Federal"),
    "laed": ("Federal", "Federal"),
    "lamd": ("Federal", "Federal"),
    "lawd": ("Federal", "Federal"),
    "med": ("Federal", "Federal"),
    "mdd": ("Federal", "Federal"),
    "mad": ("Federal", "Federal"),
    "mied": ("Federal", "Federal"),
    "miwd": ("Federal", "Federal"),
    "mnd": ("Federal", "Federal"),
    "msnd": ("Federal", "Federal"),
    "mssd": ("Federal", "Federal"),
    "moed": ("Federal", "Federal"),
    "mowd": ("Federal", "Federal"),
    "mtd": ("Federal", "Federal"),
    "ned": ("Federal", "Federal"),
    "nvd": ("Federal", "Federal"),
    "nhd": ("Federal", "Federal"),
    "njd": ("Federal", "Federal"),
    "nmd": ("Federal", "Federal"),
    "nyed": ("Federal", "Federal"),
    "nynd": ("Federal", "Federal"),
    "nysd": ("Federal", "Federal"),
    "nywd": ("Federal", "Federal"),
    "nced": ("Federal", "Federal"),
    "ncmd": ("Federal", "Federal"),
    "ncwd": ("Federal", "Federal"),
    "ndd": ("Federal", "Federal"),
    "ohnd": ("Federal", "Federal"),
    "ohsd": ("Federal", "Federal"),
    "oked": ("Federal", "Federal"),
    "oknd": ("Federal", "Federal"),
    "okwd": ("Federal", "Federal"),
    "ord": ("Federal", "Federal"),
    "paed": ("Federal", "Federal"),
    "pamd": ("Federal", "Federal"),
    "pawd": ("Federal", "Federal"),
    "rid": ("Federal", "Federal"),
    "scd": ("Federal", "Federal"),
    "sdd": ("Federal", "Federal"),
    "tned": ("Federal", "Federal"),
    "tnmd": ("Federal", "Federal"),
    "tnwd": ("Federal", "Federal"),
    "txed": ("Federal", "Federal"),
    "txnd": ("Federal", "Federal"),
    "txsd": ("Federal", "Federal"),
    "txwd": ("Federal", "Federal"),
    "utd": ("Federal", "Federal"),
    "vtd": ("Federal", "Federal"),
    "vaed": ("Federal", "Federal"),
    "vawd": ("Federal", "Federal"),
    "waed": ("Federal", "Federal"),
    "wawd": ("Federal", "Federal"),
    "wvnd": ("Federal", "Federal"),
    "wvsd": ("Federal", "Federal"),
    "wied": ("Federal", "Federal"),
    "wiwd": ("Federal", "Federal"),
    "wyd": ("Federal", "Federal"),
    # Specialty federal courts
    "uscfc": ("Federal", "Federal"),  # Court of Federal Claims
    "cc": ("Federal", "Federal"),  # Court of Claims (historical)
    "uscit": ("Federal", "Federal"),  # Court of International Trade
    "bap1": ("Federal", "Federal"),
    "bap2": ("Federal", "Federal"),
    "bap6": ("Federal", "Federal"),
    "bap8": ("Federal", "Federal"),
    "bap9": ("Federal", "Federal"),
    "bap10": ("Federal", "Federal"),
    "bapme": ("Federal", "Federal"),
    "bapma": ("Federal", "Federal"),
    # -----------------------------------------------------------------------
    # State high courts (supreme courts)
    # -----------------------------------------------------------------------
    "cal": ("California", "Statewide"),
    "tex": ("Texas", "Statewide"),
    "ny": ("New York", "Statewide"),
    "fla": ("Florida", "Statewide"),
    "ga": ("Georgia", "Statewide"),
    "ohio": ("Ohio", "Statewide"),
    "ill": ("Illinois", "Statewide"),
    "pa": ("Pennsylvania", "Statewide"),
    "mich": ("Michigan", "Statewide"),
    "nc": ("North Carolina", "Statewide"),
    "nj": ("New Jersey", "Statewide"),
    "va": ("Virginia", "Statewide"),
    "wash": ("Washington", "Statewide"),
    "ariz": ("Arizona", "Statewide"),
    "tenn": ("Tennessee", "Statewide"),
    "mo": ("Missouri", "Statewide"),
    "md": ("Maryland", "Statewide"),
    "wis": ("Wisconsin", "Statewide"),
    "minn": ("Minnesota", "Statewide"),
    "colo": ("Colorado", "Statewide"),
    "sc": ("South Carolina", "Statewide"),
    "ala": ("Alabama", "Statewide"),
    "la": ("Louisiana", "Statewide"),
    "ky": ("Kentucky", "Statewide"),
    "ore": ("Oregon", "Statewide"),
    "okla": ("Oklahoma", "Statewide"),
    "conn": ("Connecticut", "Statewide"),
    "utah": ("Utah", "Statewide"),
    "iowa": ("Iowa", "Statewide"),
    "nev": ("Nevada", "Statewide"),
    "ark": ("Arkansas", "Statewide"),
    "miss": ("Mississippi", "Statewide"),
    "kan": ("Kansas", "Statewide"),
    "nm": ("New Mexico", "Statewide"),
    "neb": ("Nebraska", "Statewide"),
    "wva": ("West Virginia", "Statewide"),
    "idaho": ("Idaho", "Statewide"),
    "hi": ("Hawaii", "Statewide"),
    "me": ("Maine", "Statewide"),
    "nh": ("New Hampshire", "Statewide"),
    "ri": ("Rhode Island", "Statewide"),
    "mont": ("Montana", "Statewide"),
    "del": ("Delaware", "Statewide"),
    "sd": ("South Dakota", "Statewide"),
    "nd": ("North Dakota", "Statewide"),
    "alaska": ("Alaska", "Statewide"),
    "vt": ("Vermont", "Statewide"),
    "wyo": ("Wyoming", "Statewide"),
    # -----------------------------------------------------------------------
    # State appellate courts
    # -----------------------------------------------------------------------
    # Texas Courts of Appeals (14 districts)
    "texapp1": ("Texas", "Statewide"),
    "texapp2": ("Texas", "Statewide"),
    "texapp3": ("Texas", "Statewide"),
    "texapp4": ("Texas", "Statewide"),
    "texapp5": ("Texas", "Statewide"),
    "texapp6": ("Texas", "Statewide"),
    "texapp7": ("Texas", "Statewide"),
    "texapp8": ("Texas", "Statewide"),
    "texapp9": ("Texas", "Statewide"),
    "texapp10": ("Texas", "Statewide"),
    "texapp11": ("Texas", "Statewide"),
    "texapp12": ("Texas", "Statewide"),
    "texapp13": ("Texas", "Statewide"),
    "texapp14": ("Texas", "Statewide"),
    # Texas Court of Criminal Appeals
    "texcrimapp": ("Texas", "Statewide"),
    # New York Appellate Divisions
    "nyappdiv1": ("New York", "Statewide"),
    "nyappdiv2": ("New York", "Statewide"),
    "nyappdiv3": ("New York", "Statewide"),
    "nyappdiv4": ("New York", "Statewide"),
    # New York Appellate Term
    "nyappterm1": ("New York", "Statewide"),
    "nyappterm2": ("New York", "Statewide"),
    # Florida District Courts of Appeal
    "fladistctapp": ("Florida", "Statewide"),
    "fladistctapp1": ("Florida", "Statewide"),
    "fladistctapp2": ("Florida", "Statewide"),
    "fladistctapp3": ("Florida", "Statewide"),
    "fladistctapp4": ("Florida", "Statewide"),
    "fladistctapp5": ("Florida", "Statewide"),
    "fladistctapp6": ("Florida", "Statewide"),
    # Georgia Court of Appeals
    "gactapp": ("Georgia", "Statewide"),
    # Ohio Courts of Appeals (12 districts)
    "ohioctapp1": ("Ohio", "Statewide"),
    "ohioctapp2": ("Ohio", "Statewide"),
    "ohioctapp3": ("Ohio", "Statewide"),
    "ohioctapp4": ("Ohio", "Statewide"),
    "ohioctapp5": ("Ohio", "Statewide"),
    "ohioctapp6": ("Ohio", "Statewide"),
    "ohioctapp7": ("Ohio", "Statewide"),
    "ohioctapp8": ("Ohio", "Statewide"),
    "ohioctapp9": ("Ohio", "Statewide"),
    "ohioctapp10": ("Ohio", "Statewide"),
    "ohioctapp11": ("Ohio", "Statewide"),
    "ohioctapp12": ("Ohio", "Statewide"),
    # California Courts of Appeal
    "calctapp1": ("California", "Statewide"),
    "calctapp2": ("California", "Statewide"),
    "calctapp3": ("California", "Statewide"),
    "calctapp4": ("California", "Statewide"),
    "calctapp5": ("California", "Statewide"),
    "calctapp6": ("California", "Statewide"),
    # Illinois Appellate Courts
    "illappct1": ("Illinois", "Statewide"),
    "illappct2": ("Illinois", "Statewide"),
    "illappct3": ("Illinois", "Statewide"),
    "illappct4": ("Illinois", "Statewide"),
    "illappct5": ("Illinois", "Statewide"),
    # Pennsylvania Superior and Commonwealth Courts
    "pasuperct": ("Pennsylvania", "Statewide"),
    "pacommwct": ("Pennsylvania", "Statewide"),
    # Michigan Court of Appeals
    "michctapp": ("Michigan", "Statewide"),
    # North Carolina Court of Appeals
    "ncctapp": ("North Carolina", "Statewide"),
    # New Jersey Appellate Division
    "njsuperctappdiv": ("New Jersey", "Statewide"),
    # Virginia Court of Appeals
    "vactapp": ("Virginia", "Statewide"),
    # Washington Court of Appeals
    "washctapp1": ("Washington", "Statewide"),
    "washctapp2": ("Washington", "Statewide"),
    "washctapp3": ("Washington", "Statewide"),
    # Arizona Court of Appeals
    "arizctapp1": ("Arizona", "Statewide"),
    "arizctapp2": ("Arizona", "Statewide"),
    # Tennessee Court of Appeals
    "tennctapp": ("Tennessee", "Statewide"),
    "tenncrimapp": ("Tennessee", "Statewide"),
    # Missouri Court of Appeals
    "moctapp": ("Missouri", "Statewide"),
    "moctappwdist": ("Missouri", "Statewide"),
    "moctappedist": ("Missouri", "Statewide"),
    "moctappsdist": ("Missouri", "Statewide"),
    # Maryland Court of Special Appeals
    "mdctspecapp": ("Maryland", "Statewide"),
    # Wisconsin Court of Appeals
    "wisctapp1": ("Wisconsin", "Statewide"),
    "wisctapp2": ("Wisconsin", "Statewide"),
    "wisctapp3": ("Wisconsin", "Statewide"),
    "wisctapp4": ("Wisconsin", "Statewide"),
    # Minnesota Court of Appeals
    "minnctapp": ("Minnesota", "Statewide"),
    # Colorado Court of Appeals
    "coloctapp": ("Colorado", "Statewide"),
    # South Carolina Court of Appeals
    "scctapp": ("South Carolina", "Statewide"),
    # Alabama Court of Civil Appeals / Court of Criminal Appeals
    "alacivapp": ("Alabama", "Statewide"),
    "alacrimapp": ("Alabama", "Statewide"),
    # Louisiana Courts of Appeal
    "laapp1": ("Louisiana", "Statewide"),
    "laapp2": ("Louisiana", "Statewide"),
    "laapp3": ("Louisiana", "Statewide"),
    "laapp4": ("Louisiana", "Statewide"),
    "laapp5": ("Louisiana", "Statewide"),
    # Kentucky Court of Appeals
    "kyctapp": ("Kentucky", "Statewide"),
    # Oregon Court of Appeals
    "orctapp": ("Oregon", "Statewide"),
    # Oklahoma Court of Civil Appeals / Court of Criminal Appeals
    "oklacivilapp": ("Oklahoma", "Statewide"),
    "oklacrimapp": ("Oklahoma", "Statewide"),
    # Connecticut Appellate Court
    "connappct": ("Connecticut", "Statewide"),
    # Utah Court of Appeals
    "utahctapp": ("Utah", "Statewide"),
    # Iowa Court of Appeals
    "iowactapp": ("Iowa", "Statewide"),
    # Arkansas Court of Appeals
    "arkctapp": ("Arkansas", "Statewide"),
    # Mississippi Court of Appeals
    "missctapp": ("Mississippi", "Statewide"),
    # Kansas Court of Appeals
    "kanctapp": ("Kansas", "Statewide"),
    # New Mexico Court of Appeals
    "nmctapp": ("New Mexico", "Statewide"),
    # Nebraska Court of Appeals
    "nebctapp": ("Nebraska", "Statewide"),
    # West Virginia Intermediate Court of Appeals
    "wvactapp": ("West Virginia", "Statewide"),
    # Idaho Court of Appeals
    "idahoctapp": ("Idaho", "Statewide"),
    # Montana
    "montag": ("Montana", "Statewide"),
    # DC Court of Appeals (local, not federal circuit)
    "dc": ("District of Columbia", "Statewide"),
}


# CourtListener opinion type codes
# https://www.courtlistener.com/api/rest/v4/ (schema)
_OPINION_TYPE_MAP = {
    "010combined": "Combined Opinion",
    "015unamimous": "Unanimous Opinion",
    "020lead": "Lead Opinion",
    "025plurality": "Plurality Opinion",
    "030concurrence": "Concurrence",
    "035concurrenceinpart": "Concurrence in Part",
    "040dissent": "Dissent",
    "045dissentinpart": "Dissent in Part",
    "050addendum": "Addendum",
    "060remittitur": "Remittitur",
    "070rehearing": "Rehearing",
    "080onbandon": "On the Merits",
    "090onmotiontostrike": "On Motion to Strike",
}


def _map_opinion_type(opinion_type: str | None) -> str | None:
    """Map CourtListener opinion type code to a human-readable string."""
    if not opinion_type:
        return None
    return _OPINION_TYPE_MAP.get(opinion_type, opinion_type)


def default_config(s3_bucket: str = "") -> ScraperConfig:
    """Factory for the default CourtListener scraper configuration."""
    from datetime import time as dtime

    return ScraperConfig(
        scraper_id="federal-courtlistener-opinions",
        state="Federal",
        county="Federal",
        court="CourtListener",
        target_urls=[f"{API_BASE_URL}/clusters/"],
        poll_interval_seconds=86400,  # daily
        schedule_windows=[
            # Run once daily in off-peak hours (UTC) to be courteous
            ScheduleWindow(
                start=dtime(6, 0),
                end=dtime(7, 0),
                timezone="UTC",
            ),
        ],
        request_delay_seconds=DEFAULT_REQUEST_DELAY,
        request_timeout_seconds=30.0,
        max_retries=3,
        s3_bucket=s3_bucket,
    )
