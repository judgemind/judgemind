"""Tests for post-processing filter re-application on LLM cache-hit paths (#2513).

When the LLM result cache returns a hit, ``LlmExtractor.extract`` and
``LlmExtractor.extract_from_pdf`` must re-apply the post-processing filters
that operate on the final ``ExtractedRuling[]`` list.  Otherwise, filter
updates made AFTER the cache entry was written do not take effect —
leaving stale rows in downstream tables until an expensive full reingest
is run with ``--bust-llm-cache``.

The filters are idempotent, so re-applying them at cache-read time has no
effect when the cached rulings already pass the filters.  When the filters
have been widened (as in #2489), the new logic fires on read and the
stale rulings are dropped before they reach the caller.

These tests use a ``MagicMock`` cache to simulate pre-computed entries
written under older, narrower filter logic.  No real network / LLM calls
happen.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from framework.llm_extractor import (
    PDF_PER_PAGE_PROMPT,
    LlmExtractor,
    _apply_pdf_cache_hit_filters,
    _apply_text_cache_hit_filters,
)
from framework.llm_schema import EXTRACTION_SYSTEM_PROMPT, ExtractedRuling


def _build_extractor_with_cache(cache: object) -> LlmExtractor:
    """Build a bare LlmExtractor with the provided cache attached.

    Mirrors the pattern used in ``test_citation_filter.py`` — constructs
    the instance via ``__new__`` and fills in the required attributes so
    no API key, client, or provider connection is needed.
    """
    extractor = LlmExtractor.__new__(LlmExtractor)
    extractor._provider = "anthropic"
    extractor._model = "stub"
    extractor._client = None
    extractor._max_retries = 0
    extractor._base_delay = 0.0
    extractor._max_delay = 0.0
    extractor._max_output_tokens = 4096
    extractor._max_chars_per_chunk = 80_000
    extractor._cache = cache
    extractor._bust_cache = False
    return extractor


# ---------------------------------------------------------------------------
# Unit tests for the two filter-application helpers
# ---------------------------------------------------------------------------


class TestApplyPdfCacheHitFilters:
    """Unit tests for ``_apply_pdf_cache_hit_filters`` (#2513)."""

    def test_drops_off_calendar_listing(self) -> None:
        """A cached OFF-CALENDAR listing is dropped on cache-hit."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-01",
                extracted_case_title="Smith v. Jones",
                ruling_text="The motion is GRANTED in full. Plaintiff's request is approved.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-02",
                extracted_case_title="Doe v. Roe",
                ruling_text="OFF-CALENDAR",
            ),
        ]
        filtered = _apply_pdf_cache_hit_filters(rulings, content_key="abc123def456")
        case_numbers = [r.extracted_case_number for r in filtered]
        assert case_numbers == ["30-2024-01"]

    def test_drops_bare_disposition_parenthetical(self) -> None:
        """A ``(Moot)`` listing (widened by #2489) is dropped on cache-hit."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-01",
                extracted_case_title="Real Ruling",
                ruling_text="The demurrer is SUSTAINED without leave to amend.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-02",
                extracted_case_title="Bare Disposition",
                ruling_text="(Moot)",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-03",
                extracted_case_title="OC Abbrev",
                ruling_text="O/C",
            ),
        ]
        filtered = _apply_pdf_cache_hit_filters(rulings, content_key="abc123def456")
        case_numbers = [r.extracted_case_number for r in filtered]
        assert case_numbers == ["30-2024-01"]

    def test_drops_short_unsubstantive_ruling_on_cache_hit(self) -> None:
        """A stale short-unsubstantive row (#2645) is dropped on cache-hit.

        Simulates a cache entry written before the short-unsubstantive
        filter existed: the 46-char case-caption noise from the issue body
        must be dropped on cache read rather than requiring a reingest.
        """
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-01",
                extracted_case_title="Real Ruling",
                ruling_text="The demurrer is SUSTAINED without leave to amend.",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-02",
                extracted_case_title="Empty-cell Noise (#2645)",
                ruling_text="MALKI, Wajih v. KARANOUH, Abdulmajid",
                motion_type=None,
                outcome=None,
            ),
        ]
        filtered = _apply_pdf_cache_hit_filters(rulings, content_key="abc123def456")
        case_numbers = [r.extracted_case_number for r in filtered]
        assert case_numbers == ["30-2024-01"]

    def test_preserves_valid_rulings(self) -> None:
        """Non-listing rulings are preserved through cache-hit filters."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-01",
                extracted_case_title="Foo v. Bar",
                ruling_text=(
                    "The court finds that the moving party has met its burden. "
                    "The motion is GRANTED. The opposing party shall comply "
                    "with the ruling within 30 days."
                ),
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-02",
                extracted_case_title="Baz v. Qux",
                ruling_text=(
                    "Defendant's demurrer is OVERRULED. Plaintiff's complaint "
                    "sufficiently states a cause of action for breach of contract."
                ),
            ),
        ]
        filtered = _apply_pdf_cache_hit_filters(rulings, content_key="abc123def456")
        assert len(filtered) == 2
        assert [r.extracted_case_number for r in filtered] == ["30-2024-01", "30-2024-02"]

    def test_deduplicates_identical_ruling_texts(self) -> None:
        """Duplicate ruling_text values are nulled out on cache-hit."""
        # Must exceed _DEDUP_MIN_LENGTH (200 chars) to trigger dedup.
        long_text = (
            "The court finds the motion to be well-taken on all grounds "
            "presented. After careful review of the briefs and supporting "
            "evidence, the motion is GRANTED in its entirety. The opposing "
            "party shall comply within thirty (30) days of notice of this order."
        )
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-01",
                ruling_text=long_text,
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-02",
                ruling_text=long_text,  # duplicate
            ),
        ]
        filtered = _apply_pdf_cache_hit_filters(rulings, content_key="abc123def456")
        # Dedup nulls out the text but keeps the row (metadata is preserved).
        assert len(filtered) == 2
        assert filtered[0].ruling_text == long_text
        assert filtered[1].ruling_text is None

    def test_idempotent_on_already_filtered_rulings(self) -> None:
        """Applying filters twice is a no-op when rulings already pass."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="30-2024-01",
                extracted_case_title="First Case",
                ruling_text="Motion GRANTED. The parties shall proceed as ordered.",
            ),
        ]
        once = _apply_pdf_cache_hit_filters(rulings, content_key="abc123def456")
        twice = _apply_pdf_cache_hit_filters(once, content_key="abc123def456")
        assert len(once) == len(twice) == 1
        assert once[0].extracted_case_number == twice[0].extracted_case_number

    def test_empty_input_returns_empty(self) -> None:
        """Empty input yields empty output."""
        assert _apply_pdf_cache_hit_filters([], content_key="abc123def456") == []

    def test_resolve_cross_references_on_cache_hit(self) -> None:
        """_apply_pdf_cache_hit_filters resolves stub rulings via entry_number (#3608).

        Simulates the Santa Clara cache-hit scenario: two cached ExtractedRuling
        objects where the stub (entry_number=2) points to the target
        (entry_number=1) via "See Line 1 below for...".  Without the fix, the
        resolver was not called on the cache-hit path and the stub persisted.
        """
        target_text = (
            "The demurrer is OVERRULED. Defendant's motion to dismiss is DENIED. "
            "Plaintiff has adequately stated a claim for breach of contract and "
            "the Court finds the allegations sufficient to survive demurrer. "
            "Defendant shall file an answer within twenty (20) days of this ruling."
        )
        stub_text = (
            "**Order on Defendants' Demurrer to the Fifth Cause of Action**\n\n"
            "See Line 1 below for complete tentative ruling."
        )
        rulings = [
            ExtractedRuling(
                extracted_case_number="20CV374597",
                extracted_case_title="Regional Medical v. County of SC",
                ruling_text=target_text,
                entry_number=1,
            ),
            ExtractedRuling(
                extracted_case_number="20CV374598",
                extracted_case_title="Doe v. County of SC",
                ruling_text=stub_text,
                entry_number=2,
            ),
        ]
        result = _apply_pdf_cache_hit_filters(rulings, content_key="abc123def456")

        # Both rulings survive (neither is a calendar listing or short text).
        assert len(result) == 2

        # Stub (index 1) must have been resolved: ruling_text replaced with
        # the target's text and cross_reference_source set to 1.
        assert result[1].ruling_text == target_text
        assert result[1].cross_reference_source == 1

        # Target (index 0) is unchanged.
        assert result[0].ruling_text == target_text
        assert result[0].cross_reference_source is None


class TestApplyTextCacheHitFilters:
    """Unit tests for ``_apply_text_cache_hit_filters`` (#2513)."""

    def test_drops_citation_artifacts(self) -> None:
        """Inline citations (title + short text) are filtered out on cache-hit."""
        real = ExtractedRuling(
            extracted_case_number="CIVSB2223971",
            extracted_case_title="Armistead v. Ford",
            ruling_text=("Before the Court is Plaintiffs' Motion for Attorneys' Fees. " * 60),
        )
        citation = ExtractedRuling(
            extracted_case_number=None,
            extracted_case_title="Zargarian v. BMW C.D. Cal. Mar. 3, 2020",
            ruling_text="B" * 200,  # shorter than _CITATION_MIN_RULING_LENGTH
        )
        filtered = _apply_text_cache_hit_filters([real, citation], content_key="abc123def456")
        assert len(filtered) == 1
        assert filtered[0].extracted_case_number == "CIVSB2223971"

    def test_deduplicates_identical_ruling_texts(self) -> None:
        """Duplicate ruling_text values are nulled out on cache-hit."""
        # Must exceed _DEDUP_MIN_LENGTH (200 chars) to trigger dedup.
        long_text = (
            "The court finds the motion to be well-taken on all grounds "
            "presented. After careful review of the briefs and supporting "
            "evidence, the motion is GRANTED in its entirety. The opposing "
            "party shall comply within thirty (30) days of notice of this order."
        )
        rulings = [
            ExtractedRuling(extracted_case_number="A", ruling_text=long_text),
            ExtractedRuling(extracted_case_number="B", ruling_text=long_text),
        ]
        filtered = _apply_text_cache_hit_filters(rulings, content_key="abc123def456")
        assert filtered[0].ruling_text == long_text
        assert filtered[1].ruling_text is None

    def test_preserves_valid_rulings(self) -> None:
        """Non-citation, non-duplicate rulings pass through unchanged."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="CIVSB1",
                extracted_case_title="Real Case A",
                ruling_text=("The motion is DENIED. Plaintiff has not shown good cause."),
            ),
            ExtractedRuling(
                extracted_case_number="CIVSB2",
                extracted_case_title="Real Case B",
                ruling_text=(
                    "Demurrer is SUSTAINED with leave to amend. Plaintiff shall "
                    "file an amended complaint within 20 days."
                ),
            ),
        ]
        filtered = _apply_text_cache_hit_filters(rulings, content_key="abc123def456")
        assert len(filtered) == 2

    def test_empty_input_returns_empty(self) -> None:
        """Empty input yields empty output."""
        assert _apply_text_cache_hit_filters([], content_key="abc123def456") == []


# ---------------------------------------------------------------------------
# Integration: cache-hit path on extract_from_pdf re-applies filters
# ---------------------------------------------------------------------------


class TestExtractFromPdfCacheHitReappliesFilters:
    """Integration: ``extract_from_pdf`` applies the filters on cache-hit (#2513)."""

    def test_cache_hit_drops_calendar_listing_rulings(self) -> None:
        """Simulates a cached entry written BEFORE #2489's filter widening.

        The cached payload contains a real ruling + a ``(Moot)`` bare-disposition
        listing.  On cache-hit, the post-processing filter now catches the listing
        and drops it — exactly the behavior that avoids the 22-45h reingest
        described in #2513.
        """
        # Pre-computed cache payload as it would be serialized to S3.
        cached_payload: list[dict[str, Any]] = [
            {
                "extracted_case_number": "30-2024-01",
                "extracted_case_title": "Real Motion",
                "ruling_text": (
                    "The motion is GRANTED. Moving party shall prepare "
                    "the order consistent with the tentative ruling."
                ),
            },
            {
                "extracted_case_number": "30-2024-02",
                "extracted_case_title": "Stale Listing",
                "ruling_text": "(Moot)",
            },
            {
                "extracted_case_number": "30-2024-03",
                "extracted_case_title": "Stale OFF-CALENDAR",
                "ruling_text": "OFF-CALENDAR",
            },
        ]
        cache = MagicMock()
        cache.get.return_value = cached_payload

        extractor = _build_extractor_with_cache(cache)

        # Any non-empty bytes payload drives the content_key computation.
        rulings = extractor.extract_from_pdf(b"pdf-bytes-content")

        cache.get.assert_called_once()
        # The cached payload key must be the per-page PDF prompt.
        prompt_arg = cache.get.call_args.args[0]
        assert prompt_arg == PDF_PER_PAGE_PROMPT

        case_numbers = [r.extracted_case_number for r in rulings]
        assert case_numbers == ["30-2024-01"]

    def test_cache_hit_respects_bust_cache_flag(self) -> None:
        """``bust_cache=True`` skips the cache read entirely, avoiding filter re-apply.

        The cache.get method must NOT be called, since the caller explicitly
        asked for fresh extraction.  The call still fails (no API key) — we
        only verify the bust behavior via ``cache.get.assert_not_called``.
        """
        cache = MagicMock()
        cache.get.return_value = [
            {
                "extracted_case_number": "30-2024-01",
                "ruling_text": "Anything.",
            }
        ]
        extractor = _build_extractor_with_cache(cache)

        # bust_cache=True short-circuits the cache-read branch.  The call
        # proceeds into _render_pdf_pages which will fail on invalid bytes —
        # but the important invariant is that cache.get was never called.
        try:
            extractor.extract_from_pdf(b"pdf-bytes-content", bust_cache=True)
        except Exception:  # noqa: BLE001
            # _render_pdf_pages / _extract_single_page may raise without a
            # real client.  That's fine — we only care about cache.get.
            pass
        cache.get.assert_not_called()

    def test_cache_hit_with_no_listings_returns_cached_rulings(self) -> None:
        """If the cached rulings all already pass the filters, output matches input."""
        cached_payload: list[dict[str, Any]] = [
            {
                "extracted_case_number": "30-2024-01",
                "ruling_text": (
                    "The court finds that the moving party has established good "
                    "cause. The motion is GRANTED. Defendant shall produce the "
                    "requested documents within 30 days."
                ),
            },
            {
                "extracted_case_number": "30-2024-02",
                "ruling_text": (
                    "Demurrer is OVERRULED as to the first cause of action and "
                    "SUSTAINED with leave to amend as to the second. Plaintiff "
                    "shall file an amended complaint within 20 days."
                ),
            },
        ]
        cache = MagicMock()
        cache.get.return_value = cached_payload
        extractor = _build_extractor_with_cache(cache)

        rulings = extractor.extract_from_pdf(b"pdf-bytes")
        assert [r.extracted_case_number for r in rulings] == ["30-2024-01", "30-2024-02"]


# ---------------------------------------------------------------------------
# Integration: cache-hit path on extract (text) re-applies filters
# ---------------------------------------------------------------------------


class TestExtractTextCacheHitReappliesFilters:
    """Integration: ``extract`` applies the filters on cache-hit (#2513)."""

    def test_cache_hit_drops_citation_artifacts(self) -> None:
        """Cached entries that include citation-artifact titles are filtered on read.

        The fresh-path ``extract`` applies ``_filter_citation_artifacts`` before
        caching, but if the cache was populated with older ``ExtractedRuling``
        objects (or before the citation filter existed), the stale artifacts
        must be removed on read.
        """
        cached_payload: list[dict[str, Any]] = [
            {
                "extracted_case_number": "CIVSB2223971",
                "extracted_case_title": "Armistead v. Ford",
                "ruling_text": (
                    "Before the Court is Plaintiffs' Motion for Attorneys' Fees. " * 60
                ),
            },
            {
                "extracted_case_number": None,
                "extracted_case_title": "Zargarian v. BMW C.D. Cal. Mar. 3, 2020",
                "ruling_text": "B" * 200,
            },
            {
                "extracted_case_number": None,
                "extracted_case_title": "Williams v. KMA BC722351",
                "ruling_text": "A" * 155,
            },
        ]
        cache = MagicMock()
        cache.get.return_value = cached_payload

        extractor = _build_extractor_with_cache(cache)

        rulings = extractor.extract("fake calendar text")

        cache.get.assert_called_once()
        prompt_arg = cache.get.call_args.args[0]
        assert prompt_arg == EXTRACTION_SYSTEM_PROMPT

        assert [r.extracted_case_number for r in rulings] == ["CIVSB2223971"]

    def test_cache_hit_deduplicates_duplicate_texts(self) -> None:
        """Cached duplicate ruling_text values are nulled on cache-hit."""
        # Must exceed _DEDUP_MIN_LENGTH (200 chars) to trigger dedup.
        long_text = (
            "The court finds the motion to be well-taken on all grounds "
            "presented. After review of the briefs and supporting evidence, "
            "the motion is GRANTED in its entirety. The parties shall comply "
            "within thirty (30) days of notice of this order."
        )
        cached_payload: list[dict[str, Any]] = [
            {
                "extracted_case_number": "CIVSB1",
                "ruling_text": long_text,
            },
            {
                "extracted_case_number": "CIVSB2",
                "ruling_text": long_text,
            },
        ]
        cache = MagicMock()
        cache.get.return_value = cached_payload
        extractor = _build_extractor_with_cache(cache)

        rulings = extractor.extract("fake calendar text")

        assert len(rulings) == 2
        assert rulings[0].ruling_text == long_text
        assert rulings[1].ruling_text is None

    def test_cache_hit_respects_bust_cache_flag(self) -> None:
        """``bust_cache=True`` skips the cache read entirely on the text path."""
        cache = MagicMock()
        cache.get.return_value = [
            {
                "extracted_case_number": "CIVSB1",
                "ruling_text": "Any text.",
            }
        ]
        extractor = _build_extractor_with_cache(cache)

        try:
            extractor.extract("fake calendar text", bust_cache=True)
        except Exception:  # noqa: BLE001
            # Without a real LLM, the fresh path fails — the invariant we care
            # about is that cache.get was not called.
            pass
        cache.get.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: exactly the scenario described in #2513
# ---------------------------------------------------------------------------


class TestCacheHitFilterReapplicationRegression:
    """Regression test for the exact scenario in #2513.

    Simulates the #2489 -> #2513 flow:

    1. Cache a set of rulings that include a pattern NOT yet in the filter
       (simulated by injecting a payload with ``(Moot)`` listings).
    2. The cache returns the rulings with the stale listings on cache-hit.
    3. The cache-hit path now applies the widened filter.
    4. Verify the listings are dropped before returning to the caller.
    """

    def test_cache_populated_with_stale_listings_is_filtered_on_read(self) -> None:
        """Stale ``(Moot)`` cache entries are dropped on read instead of requiring reingest."""
        # A cache entry as it might have been written BEFORE #2489's widening.
        # Under the old filter, none of these listings would have been dropped.
        stale_payload: list[dict[str, Any]] = [
            {
                "extracted_case_number": "30-2024-A",
                "ruling_text": (
                    "The demurrer is SUSTAINED with leave to amend. Plaintiff "
                    "shall file an amended complaint within 20 days."
                ),
            },
            {
                "extracted_case_number": "30-2024-B",
                "ruling_text": "(Moot)",
            },
            {
                "extracted_case_number": "30-2024-C",
                "ruling_text": "(Continued)",
            },
            {
                "extracted_case_number": "30-2024-D",
                "ruling_text": "(Withdrawn)",
            },
            {
                "extracted_case_number": "30-2024-E",
                "ruling_text": "OFF-CALENDAR",
            },
            {
                "extracted_case_number": "30-2024-F",
                "ruling_text": "O/C",
            },
            {
                "extracted_case_number": "30-2024-G",
                "ruling_text": "NO TENTATIVE",
            },
            {
                "extracted_case_number": "30-2024-H",
                "ruling_text": "Tentative pending",
            },
        ]
        cache = MagicMock()
        cache.get.return_value = stale_payload
        extractor = _build_extractor_with_cache(cache)

        rulings = extractor.extract_from_pdf(b"pdf-bytes")

        # Only the real ruling survives — seven stale listings dropped.
        assert len(rulings) == 1
        assert rulings[0].extracted_case_number == "30-2024-A"
