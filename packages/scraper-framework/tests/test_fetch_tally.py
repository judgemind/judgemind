"""Tests for framework.fetch_tally.FetchTally (#4693).

The tally is the shared "every per-item fetch failed" gate: a scraper whose
fetch loop logs and swallows per-item exceptions must not record
``status=success records=0`` when nothing was captured and every attempt
failed. A fetch that succeeds and finds nothing (a genuinely empty calendar)
must stay a success.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from framework import BaseScraper, CapturedDocument, ContentFormat, ScraperConfig
from framework.base import ScraperPreconditionFailure
from framework.fetch_tally import FetchTally


class TestFetchTally:
    def test_all_failed_raises(self) -> None:
        tally = FetchTally("PDF fetches")
        for i in range(3):
            tally.attempt()
            tally.failed(RuntimeError(f"boom {i}"))

        with pytest.raises(ScraperPreconditionFailure) as ei:
            tally.raise_if_all_failed([])

        msg = str(ei.value)
        assert "all 3 PDF fetches failed" in msg
        assert "0 blocked, 3 raised errors" in msg
        assert "RuntimeError: boom 2" in msg

    def test_failed_without_attempt_counts_an_attempt(self) -> None:
        tally = FetchTally("lookups")
        tally.failed(ValueError("x"))
        tally.failed("plain reason")
        assert tally.n_attempted == 2
        assert tally.n_failed == 2
        assert tally.n_ok == 0
        assert tally.all_failed
        assert tally.last_error == "plain reason"

    def test_mixed_blocked_and_failed_raises(self) -> None:
        tally = FetchTally("case lookups")
        tally.attempt()
        tally.blocked("cloudflare block page")
        tally.attempt()
        tally.failed(TimeoutError("timed out"))

        assert tally.all_failed
        with pytest.raises(ScraperPreconditionFailure) as ei:
            tally.raise_if_all_failed([])
        msg = str(ei.value)
        assert "all 2 case lookups failed (1 blocked, 1 raised errors)" in msg
        assert "TimeoutError: timed out" in msg

    def test_all_blocked_message_names_block_reason(self) -> None:
        tally = FetchTally("lookups")
        tally.blocked("session expired")
        tally.blocked("session expired")
        with pytest.raises(ScraperPreconditionFailure, match="all 2 lookups were blocked"):
            tally.raise_if_all_failed([])
        with pytest.raises(ScraperPreconditionFailure, match="session expired"):
            tally.raise_if_all_failed([])

    def test_partial_success_does_not_raise(self) -> None:
        tally = FetchTally("PDF fetches")
        tally.attempt()
        tally.failed(RuntimeError("boom"))
        tally.attempt()  # completed without being marked -> ok
        tally.attempt()
        tally.blocked("blocked")

        assert tally.n_ok == 1
        assert not tally.all_failed
        tally.raise_if_all_failed([])  # no docs, but one fetch worked

    def test_explicit_ok_counts_one_attempt(self) -> None:
        tally = FetchTally("x")
        tally.attempt()
        tally.ok()
        tally.ok()
        assert tally.n_attempted == 2
        assert tally.n_ok == 2

    def test_docs_captured_never_raises(self) -> None:
        tally = FetchTally("x")
        tally.failed(RuntimeError("boom"))
        tally.raise_if_all_failed([object()])

    def test_genuine_empty_does_not_raise(self) -> None:
        """A fetch that completes and finds nothing is not a failure."""
        tally = FetchTally("calendar pages")
        tally.attempt()
        tally.raise_if_all_failed([])
        assert tally.n_ok == 1

    def test_no_attempts_does_not_raise(self) -> None:
        """Nothing to fetch (empty index) is a legitimate empty run."""
        tally = FetchTally("x")
        assert not tally.all_failed
        tally.raise_if_all_failed([])

    def test_custom_message_overrides_default(self) -> None:
        tally = FetchTally("x")
        tally.failed(RuntimeError("boom"))
        with pytest.raises(ScraperPreconditionFailure, match="^custom reason$"):
            tally.raise_if_all_failed([], message="custom reason")

    def test_last_error_is_truncated(self) -> None:
        tally = FetchTally("x")
        tally.failed(RuntimeError("y" * 1000))
        assert tally.last_error is not None
        assert len(tally.last_error) <= 300

    def test_last_error_keeps_only_first_line(self) -> None:
        tally = FetchTally("x")
        tally.failed(RuntimeError("Server error 500\nFor more information check: ..."))
        assert tally.last_error == "RuntimeError: Server error 500"
        tally.failed(RuntimeError(""))
        assert tally.last_error == "RuntimeError"

    def test_log_fields(self) -> None:
        tally = FetchTally("x")
        tally.attempt()
        tally.attempt()
        tally.failed(RuntimeError("boom"))
        assert tally.log_fields() == {
            "attempted": 2,
            "succeeded": 1,
            "blocked": 0,
            "failed": 1,
            "skipped": 0,
            "last_error": "RuntimeError: boom",
        }


class TestFetchTallyAbort:
    """An early abort that skips items is never a green run (#4734)."""

    def _aborted_after_success(self) -> FetchTally:
        tally = FetchTally("case lookups")
        tally.attempt()  # ok
        for _ in range(3):
            tally.attempt()
            tally.failed(TimeoutError("timed out"))
        tally.attempt()
        tally.blocked("block page")
        tally.abort("4 consecutive lookups failed", remaining=7)
        return tally

    def test_abort_counts_and_message(self) -> None:
        tally = self._aborted_after_success()
        assert tally.aborted
        assert tally.n_skipped == 7
        assert tally.log_fields()["skipped"] == 7
        msg = tally.abort_message()
        assert "case lookups aborted after 5 of 12, 7 skipped" in msg
        assert "(1 succeeded, 1 blocked, 3 raised errors)" in msg
        assert "4 consecutive lookups failed" in msg
        assert "TimeoutError: timed out" in msg
        assert "block page" in msg
        assert tally.partial_failure_message() == msg

    def test_abort_with_no_docs_raises_despite_successes(self) -> None:
        tally = self._aborted_after_success()
        assert not tally.all_failed
        with pytest.raises(ScraperPreconditionFailure, match="aborted after 5 of 12"):
            tally.raise_if_all_failed([])

    def test_abort_with_docs_does_not_raise(self) -> None:
        tally = self._aborted_after_success()
        tally.raise_if_all_failed(["doc"])  # partial failure, reported by run()

    def test_all_failed_abort_keeps_custom_message_and_names_skipped(self) -> None:
        tally = FetchTally("lookups")
        for _ in range(5):
            tally.failed(RuntimeError("x"))
        tally.abort("streak", remaining=3)
        with pytest.raises(ScraperPreconditionFailure) as ei:
            tally.raise_if_all_failed([], message="custom diagnosis")
        assert str(ei.value) == "custom diagnosis; 3 more skipped after abort"

    def test_abort_with_nothing_remaining_is_not_a_failure(self) -> None:
        tally = FetchTally("lookups")
        tally.attempt()
        tally.failed(RuntimeError("x"))
        tally.attempt()
        tally.abort("streak", remaining=0)
        assert not tally.aborted
        assert tally.partial_failure_message() is None
        tally.raise_if_all_failed([])

    def test_no_abort_has_no_partial_failure(self) -> None:
        tally = FetchTally("lookups")
        tally.attempt()
        assert tally.partial_failure_message() is None


class _AllFailScraper(BaseScraper):
    """Minimal scraper whose every per-item fetch raises."""

    def __init__(self, config: ScraperConfig, items: int, fail: bool) -> None:
        super().__init__(config)
        self._items = items
        self._fail = fail

    def fetch_documents(self) -> list[CapturedDocument]:
        docs: list[CapturedDocument] = []
        tally = FetchTally("items")
        for _ in range(self._items):
            tally.attempt()
            try:
                if self._fail:
                    raise ConnectionError("connection refused")
            except Exception as exc:
                tally.failed(exc)
        tally.raise_if_all_failed(docs)
        return docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        return doc


def _config() -> ScraperConfig:
    return ScraperConfig(
        scraper_id="test-fetch-tally",
        state="CA",
        county="X",
        court="Superior Court",
        target_urls=["https://example.test"],
        max_retries=1,
    )


class TestFetchTallyRunIntegration:
    def test_run_records_failure_when_every_item_fails(self) -> None:
        health = _AllFailScraper(_config(), items=3, fail=True).run()
        assert health.success is False
        assert health.records_captured == 0
        assert health.error_message is not None
        assert "all 3 items failed" in health.error_message
        assert "ConnectionError: connection refused" in health.error_message

    def test_run_records_success_on_genuine_empty(self) -> None:
        health = _AllFailScraper(_config(), items=3, fail=False).run()
        assert health.success is True
        assert health.records_captured == 0
        assert health.error_message is None


class _AbortingScraper(BaseScraper):
    """Captures *ok* docs, then aborts with *skipped* items left (#4734)."""

    def __init__(self, config: ScraperConfig, ok: int, skipped: int) -> None:
        super().__init__(config)
        self.ok = ok
        self.skipped = skipped

    def fetch_documents(self) -> list[CapturedDocument]:
        docs: list[CapturedDocument] = []
        tally = FetchTally("items")
        for i in range(self.ok):
            tally.attempt()
            docs.append(
                CapturedDocument(
                    scraper_id=self.config.scraper_id,
                    state=self.config.state,
                    county=self.config.county,
                    court=self.config.court,
                    source_url=f"https://example.test/{i}",
                    capture_timestamp=datetime.now(UTC),
                    content_format=ContentFormat.PDF,
                    raw_content=f"%PDF ruling {i}".encode(),
                    content_hash="",
                )
            )
        tally.attempt()
        tally.failed(TimeoutError("timed out"))
        tally.abort("breaker tripped", remaining=self.skipped)
        tally.raise_if_all_failed(docs)
        self._mark_partial_failure(tally.partial_failure_message())
        return docs

    def parse_document(self, doc: CapturedDocument) -> CapturedDocument:
        return doc


class TestPartialFailureRunIntegration:
    def test_abort_with_docs_archives_then_fails_run(self) -> None:
        archiver = MagicMock()
        archiver.archive.return_value = "ca/x/key.pdf"
        archiver.bucket = "bucket"
        scraper = _AbortingScraper(_config(), ok=2, skipped=4)
        scraper._archiver = archiver

        health = scraper.run()

        assert health.success is False
        assert health.records_captured == 2
        assert archiver.archive.call_count == 2
        assert health.error_message is not None
        assert "items aborted after 3 of 7, 4 skipped" in health.error_message
        assert "breaker tripped" in health.error_message

    def test_partial_failure_resets_between_runs(self) -> None:
        scraper = _AbortingScraper(_config(), ok=1, skipped=2)
        assert scraper.run().success is False
        scraper.skipped = 0
        health = scraper.run()
        assert health.success is True
        assert health.error_message is None

    def test_mark_partial_failure_none_is_noop(self) -> None:
        scraper = _AbortingScraper(_config(), ok=1, skipped=0)
        scraper._mark_partial_failure(None)
        assert scraper._partial_failure is None
