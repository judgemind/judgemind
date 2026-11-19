"""Per-item fetch failures that do not abort the run are counted (#5019).

Before #5019 a run where one of N per-item fetches raised (a ruling PDF that
404s or times out) recorded ``success=True records_failed=0``: the capture
loss was invisible in ``telemetry.scraper_runs``.  Every ``FetchTally``
created during ``fetch_documents`` now registers with the run, transient
errors are retried once in the run, and what still fails lands in
``records_failed`` and ``error_details`` with its URL and error.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
import respx

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig
from framework import fetch_tally as fetch_tally_mod
from framework.fetch_tally import (
    ITEM_RETRY_ATTEMPTS,
    MAX_EXHAUSTED_ITEM_RETRIES,
    MAX_FETCH_FAILURE_SAMPLES,
    FetchTally,
    collect_fetch_tallies,
)

BASE = "https://court.example"


def _status_error(status: int, url: str = f"{BASE}/a.pdf") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _config(**overrides: object) -> ScraperConfig:
    values: dict[str, object] = {
        "scraper_id": "test-fetch-failure-counted",
        "state": "CA",
        "county": "X",
        "court": "Superior Court",
        "target_urls": [BASE],
        "max_attempts": 1,
        "request_delay_seconds": 0,
    }
    values.update(overrides)
    return ScraperConfig(**values)  # type: ignore[arg-type]


class _PdfScraper(BaseScraper):
    """GETs each URL in *paths* through the shared tally helper."""

    def __init__(self, config: ScraperConfig, paths: list[str]) -> None:
        super().__init__(config)
        self.paths = paths

    def fetch_documents(self) -> list[CapturedDocument]:
        docs: list[CapturedDocument] = []
        tally = FetchTally("PDF fetches")
        with httpx.Client() as client:
            for path in self.paths:
                url = f"{BASE}{path}"
                tally.attempt(url=url)
                try:
                    response = tally.http_get(client, url)
                except Exception as exc:
                    tally.failed(exc)
                    continue
                docs.append(
                    CapturedDocument(
                        scraper_id=self.config.scraper_id,
                        state=self.config.state,
                        county=self.config.county,
                        court=self.config.court,
                        source_url=url,
                        capture_timestamp=datetime.now(UTC),
                        content_format=ContentFormat.PDF,
                        raw_content=response.content,
                        content_hash="",
                    )
                )
        tally.raise_if_all_failed(docs)
        return docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        return doc


# ---------------------------------------------------------------------------
# Run integration: the acceptance criteria
# ---------------------------------------------------------------------------


class TestFetchFailureCountedInRun:
    @respx.mock
    def test_fetch_failure_counted_404_and_timeout(self) -> None:
        respx.get(f"{BASE}/ok.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-1 ok"))
        respx.get(f"{BASE}/gone.pdf").mock(return_value=httpx.Response(404))
        respx.get(f"{BASE}/slow.pdf").mock(side_effect=httpx.ReadTimeout("timed out"))

        health = _PdfScraper(_config(), ["/ok.pdf", "/gone.pdf", "/slow.pdf"]).run()

        assert health.success is False
        assert health.records_captured == 1
        assert health.records_failed == 2
        assert health.error_message is not None
        assert "2 of 3 per-item fetches not captured" in health.error_message
        assert "1 gone (HTTP 404/410)" in health.error_message
        details = health.failure_details
        assert details is not None
        assert details["fetch_attempted"] == 3
        assert details["fetch_failed"] == 2
        assert details["fetch_blocked"] == 0
        # The timeout was retried once; the 404 was not.
        assert details["fetch_retries"] == 1
        assert details["fetch_recovered"] == 0
        by_url = {s["url"]: s for s in details["failed_fetches"]}
        gone = by_url[f"{BASE}/gone.pdf"]
        assert gone["http_status"] == 404
        assert gone["permanent"] is True
        assert gone["transient"] is False
        assert gone["what"] == "PDF fetches"
        slow = by_url[f"{BASE}/slow.pdf"]
        assert slow["transient"] is True
        assert slow["permanent"] is False
        assert "ReadTimeout" in slow["error"]
        assert details["failed_fetches_truncated"] is False

    @respx.mock
    def test_fetch_failure_counted_transient_then_success_is_clean(self) -> None:
        respx.get(f"{BASE}/a.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-1 a"))
        respx.get(f"{BASE}/b.pdf").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, content=b"%PDF-1 b")]
        )

        health = _PdfScraper(_config(), ["/a.pdf", "/b.pdf"]).run()

        assert health.success is True
        assert health.error_message is None
        assert health.records_captured == 2
        assert health.records_failed == 0
        # The recovered retry still leaves a trail in error_details.
        assert health.failure_details is not None
        assert health.failure_details["fetch_retries"] == 1
        assert health.failure_details["fetch_recovered"] == 1
        assert health.failure_details["fetch_failed"] == 0
        assert health.failure_details["failed_fetches"] == []

    @respx.mock
    def test_fetch_failure_counted_normal_run_has_no_details(self) -> None:
        respx.get(f"{BASE}/a.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-1 a"))

        health = _PdfScraper(_config(), ["/a.pdf"]).run()

        assert health.success is True
        assert health.records_failed == 0
        assert health.failure_details is None

    @respx.mock
    def test_fetch_failure_counted_when_every_item_fails(self) -> None:
        """The all-failed outage keeps its own error, and now also records the
        per-item samples."""
        respx.get(f"{BASE}/a.pdf").mock(return_value=httpx.Response(404))
        respx.get(f"{BASE}/b.pdf").mock(return_value=httpx.Response(410))

        health = _PdfScraper(_config(), ["/a.pdf", "/b.pdf"]).run()

        assert health.success is False
        assert health.error_message is not None
        assert "all 2 PDF fetches failed" in health.error_message
        assert health.records_failed == 2
        assert health.failure_details is not None
        assert [s["http_status"] for s in health.failure_details["failed_fetches"]] == [404, 410]

    def test_fetch_failure_counted_only_from_last_whole_run_attempt(self) -> None:
        """A whole-run retry that succeeds discards the failed attempt's tally."""

        class _FlakyOnce(BaseScraper):
            calls = 0

            def fetch_documents(self) -> list[CapturedDocument]:
                type(self).calls += 1
                tally = FetchTally("items")
                tally.attempt(url="https://court.example/x")
                if type(self).calls == 1:
                    tally.failed(ConnectionError("reset"))
                tally.raise_if_all_failed([])
                return []

            def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
                return doc

        scraper = _FlakyOnce(_config(max_attempts=2, all_failed_retry_delay_seconds=0))
        health = scraper.run()

        assert _FlakyOnce.calls == 2
        assert health.success is True
        assert health.records_failed == 0
        assert health.failure_details is None

    def test_fetch_failure_counted_from_async_fetch(self) -> None:
        """A tally created inside ``asyncio.run`` (the SD portal) registers too."""

        class _AsyncScraper(BaseScraper):
            def fetch_documents(self) -> list[CapturedDocument]:
                return asyncio.run(self._fetch())

            async def _fetch(self) -> list[CapturedDocument]:
                tally = FetchTally("lookups")
                tally.attempt(url="SD portal case lookup 24CU1")
                tally.attempt(url="SD portal case lookup 24CU2")
                tally.blocked("Cloudflare challenge")
                return []

            def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
                return doc

        health = _AsyncScraper(_config()).run()

        assert health.success is False
        assert health.records_failed == 1
        assert health.failure_details is not None
        sample = health.failure_details["failed_fetches"][0]
        assert sample["outcome"] == "blocked"
        assert sample["url"] == "SD portal case lookup 24CU2"

    def test_fetch_failure_counted_tallies_reset_between_runs(self) -> None:
        class _Toggle(BaseScraper):
            fail = True

            def fetch_documents(self) -> list[CapturedDocument]:
                tally = FetchTally("items")
                tally.attempt()
                tally.attempt()
                if self.fail:
                    tally.failed(RuntimeError("boom"))
                return []

            def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
                return doc

        scraper = _Toggle(_config())
        assert scraper.run().success is False
        scraper.fail = False
        health = scraper.run()
        assert health.success is True
        assert health.records_failed == 0


# ---------------------------------------------------------------------------
# FetchTally samples
# ---------------------------------------------------------------------------


class TestFetchTallySamples:
    def test_attempt_url_names_the_failure(self) -> None:
        tally = FetchTally("PDF fetches")
        tally.attempt(url="https://court.example/1.pdf")
        tally.failed(RuntimeError("parse error"))
        assert tally.samples == [
            {
                "what": "PDF fetches",
                "outcome": "failed",
                "url": "https://court.example/1.pdf",
                "error": "RuntimeError: parse error",
                "transient": False,
                "http_status": None,
                "permanent": False,
            }
        ]

    def test_explicit_url_overrides_attempt_url(self) -> None:
        tally = FetchTally()
        tally.attempt(url="listing")
        tally.failed(RuntimeError("x"), url="detail")
        assert tally.samples[0]["url"] == "detail"

    def test_httpx_error_url_is_the_fallback(self) -> None:
        tally = FetchTally()
        tally.failed(_status_error(410, f"{BASE}/old.pdf"))
        sample = tally.samples[0]
        assert sample["url"] == f"{BASE}/old.pdf"
        assert sample["http_status"] == 410
        assert sample["permanent"] is True

    def test_httpx_error_without_request_has_no_url(self) -> None:
        tally = FetchTally()
        tally.failed(httpx.ReadTimeout("timed out"))
        assert tally.samples[0]["url"] is None
        assert tally.samples[0]["transient"] is True

    def test_blocked_sample(self) -> None:
        tally = FetchTally()
        tally.attempt(url="u")
        tally.blocked("stale ViewState", transient=True)
        sample = tally.samples[0]
        assert sample["outcome"] == "blocked"
        assert sample["url"] == "u"
        assert sample["transient"] is True

    def test_url_does_not_leak_to_the_next_attempt(self) -> None:
        tally = FetchTally()
        tally.attempt(url="first")
        tally.attempt()
        tally.failed(RuntimeError("x"))
        assert tally.samples[0]["url"] is None

    def test_samples_are_capped_counts_are_exact(self) -> None:
        tally = FetchTally()
        for _ in range(MAX_FETCH_FAILURE_SAMPLES + 5):
            tally.failed(RuntimeError("x"))
        assert tally.n_failed == MAX_FETCH_FAILURE_SAMPLES + 5
        assert len(tally.samples) == MAX_FETCH_FAILURE_SAMPLES


# ---------------------------------------------------------------------------
# In-run per-item retry
# ---------------------------------------------------------------------------


class _Flaky:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class TestFetchTallyRetry:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        sleeps: list[float] = []
        monkeypatch.setattr(fetch_tally_mod.time, "sleep", sleeps.append)
        return sleeps

    def test_transient_then_success(self, _no_sleep: list[float]) -> None:
        tally = FetchTally()
        fn = _Flaky(httpx.ConnectError("reset"), "ok")
        assert tally.retry(fn) == "ok"
        assert fn.calls == 2
        assert tally.n_retries == 1
        assert tally.n_recovered == 1
        assert tally.n_failed == 0
        assert _no_sleep == [fetch_tally_mod.ITEM_RETRY_DELAY_SECONDS]

    @pytest.mark.parametrize("error", [_status_error(404), _status_error(403), ValueError("x")])
    def test_permanent_error_is_not_retried(self, error: Exception) -> None:
        tally = FetchTally()
        fn = _Flaky(error)
        with pytest.raises(type(error)):
            tally.retry(fn)
        assert fn.calls == 1
        assert tally.n_retries == 0

    def test_retry_stops_after_the_attempt_limit(self) -> None:
        tally = FetchTally()
        fn = _Flaky(*[httpx.ReadTimeout("t")] * ITEM_RETRY_ATTEMPTS)
        with pytest.raises(httpx.ReadTimeout):
            tally.retry(fn)
        assert fn.calls == ITEM_RETRY_ATTEMPTS
        assert tally.n_recovered == 0

    def test_retries_stop_once_the_site_looks_down(self) -> None:
        tally = FetchTally()
        for _ in range(MAX_EXHAUSTED_ITEM_RETRIES):
            with pytest.raises(httpx.ReadTimeout):
                tally.retry(_Flaky(httpx.ReadTimeout("t"), httpx.ReadTimeout("t")))
        fn = _Flaky(httpx.ReadTimeout("t"), "ok")
        with pytest.raises(httpx.ReadTimeout):
            tally.retry(fn)
        assert fn.calls == 1
        assert tally.n_retries == MAX_EXHAUSTED_ITEM_RETRIES

    def test_passes_arguments(self) -> None:
        tally = FetchTally()
        assert tally.retry(lambda a, b=0: a + b, 1, b=2) == 3

    @respx.mock
    def test_http_get_retries_5xx_and_returns_response(self) -> None:
        respx.get(f"{BASE}/a").mock(side_effect=[httpx.Response(502), httpx.Response(200)])
        tally = FetchTally()
        with httpx.Client() as client:
            assert tally.http_get(client, f"{BASE}/a").status_code == 200
        assert tally.n_recovered == 1

    @respx.mock
    def test_http_post_raises_for_status(self) -> None:
        respx.post(f"{BASE}/search").mock(return_value=httpx.Response(404))
        tally = FetchTally()
        with httpx.Client() as client, pytest.raises(httpx.HTTPStatusError):
            tally.http_post(client, f"{BASE}/search", data={"d": "1"})
        assert tally.n_retries == 0


class TestCollectFetchTallies:
    def test_registers_tallies_created_inside(self) -> None:
        with collect_fetch_tallies() as tallies:
            a = FetchTally("a")
            b = FetchTally("b")
        assert tallies == [a, b]

    def test_no_registration_outside(self) -> None:
        with collect_fetch_tallies() as tallies:
            pass
        FetchTally("later")
        assert tallies == []
