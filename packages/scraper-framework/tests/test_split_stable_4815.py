"""Regression tests for #4815: identical PDF bytes must split identically.

The Orange PDF ``ab7b1ec3...455f.pdf`` split into 12 rulings on the live
worker path and 13 on the rebuild / reingest path.  Both paths were served
from the LLM cache, but from DIFFERENT entries: the cache key hashed the PDF
bytes together with the caller's ``metadata`` (judge / department / hearing
date).  The live scraper event carries ``{"judge_name": "Shaina H. Colover",
"department": "C34"}``; a rebuild event carries none of them.  Each path
therefore had its own LLM run cached, and the two runs disagreed: the
metadata-free run emitted the department's boilerplate preamble ("TENTATIVE
RULINGS / DEPT C34 / ...") as a ruling at position 0, shifting every real
ruling one positional split-child id to the right.

The contract these tests pin:

1. The LLM input and every cache key are a function of the PDF bytes alone.
   Caller metadata is applied deterministically after extraction.
2. Entries written under the old metadata-bearing keys are still served when
   no bytes-only entry exists, and are promoted to the bytes-only key so every
   path converges on one result.
3. A calendar preamble row (no case number, no entry number, text is the
   department header) is never emitted as a ruling, on fresh or cache-hit
   paths.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import anthropic

from framework.llm_extractor import (
    PDF_PER_PAGE_PROMPT,
    ExtractedRuling,
    LlmExtractor,
    _apply_pdf_cache_hit_filters,
    _content_hash_for_cache,
    _LlmCache,
)
from ingestion.ruling_guards import convert_extracted_rulings

PARENT_ID = "a5cd43b0-5e0d-5f2a-b2c6-8354eeea662e"

LIVE_METADATA = {"judge_name": "Shaina H. Colover", "department": "C34"}
REBUILD_METADATA: dict[str, str] = {}

PREAMBLE_TEXT = (
    "**TENTATIVE RULINGS**\n\n**DEPT C34**\n\n**JUDGE H. SHAINA CLOVER**\n\n"
    "**LAW AND MOTION IS HEARD ON THURSDAY AT 1:30 P.M.**\n\n"
    "**Tentative Rulings:** The Court endeavors to post tentative rulings on the "
    "Court's website by 10:00 a.m. in the morning, prior to the afternoon hearing. "
    "Do not call the Department for tentative rulings if none are posted."
)

CASES = [
    ("2023-01363497", "Yang v. California TD Specialists", "Motion to set aside default. " * 12),
    ("2024-01396485", "Lopez v. Acme Corp.", "Motion for summary judgment. " * 12),
    ("2023-01361390", "Chavez v. Nambo", "Motion for terminating sanctions. " * 12),
]


def _row(entry: int | None, case_info: str, text: str) -> dict:
    return {"entry_number": entry, "case_info": case_info, "ruling_text": text}


def _page_json(rows: list[dict]) -> str:
    return json.dumps({"page_header": None, "rulings": rows})


# The run the live path got: three rulings.
PAGE_ROWS_12 = [_row(i + 1, f"{num} {title}", text) for i, (num, title, text) in enumerate(CASES)]
# The run the rebuild path got: the same rulings plus the preamble at position 0.
PAGE_ROWS_13 = [_row(None, "", PREAMBLE_TEXT), *PAGE_ROWS_12]

PAGES = [(b"\x89PNG_page1", "image/png")]


def _resp(text: str) -> object:
    from ingestion.llm_providers import LLMResponse

    return LLMResponse(text=text, input_tokens=10, output_tokens=5)


class _FakeS3:
    """Dict-backed stand-in for the boto3 S3 client used by ``_LlmCache``."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict:  # noqa: N803
        self.objects[Key] = Body
        return {}


def _extractor(cache: _LlmCache | None) -> LlmExtractor:
    with patch.object(anthropic, "Anthropic"):
        ext = LlmExtractor(api_key="test-key")
    ext._base_delay = 0.0
    ext._max_retries = 1
    ext._cache = cache
    return ext


def _cache() -> tuple[_LlmCache, _FakeS3]:
    s3 = _FakeS3()
    return _LlmCache(s3, "bucket", "google", "gemini-2.5-flash-lite"), s3


def _answer(page_json: str) -> Callable[..., object]:
    def _call(**kwargs: Any) -> object:
        return _resp(page_json)

    return _call


def _split(rulings: list[ExtractedRuling]) -> dict[str, str | None]:
    """Map each positional split-child id to the case number it carries."""
    return {cr.document_id: cr.case_number for cr in convert_extracted_rulings(rulings, PARENT_ID)}


# ---------------------------------------------------------------------------
# 1. Identical bytes -> identical split, whatever the caller's metadata
# ---------------------------------------------------------------------------


class TestSplitStableAcrossPaths:
    def test_split_stable_when_second_path_would_get_a_different_row_count(self) -> None:
        """The live and rebuild paths hand the same bytes different metadata.
        The second path's LLM would return a perturbed row count; it must be
        served the first path's split instead, so no ruling changes id."""
        cache, _ = _cache()
        ext = _extractor(cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_12)),
            ),
        ):
            live = ext.extract_from_pdf(b"same-pdf-bytes", metadata=LIVE_METADATA)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_13)),
            ) as rebuild_llm,
        ):
            rebuild = ext.extract_from_pdf(b"same-pdf-bytes", metadata=REBUILD_METADATA or None)

        rebuild_llm.assert_not_called()
        assert _split(rebuild) == _split(live)
        assert [r.ruling_text for r in rebuild] == [r.ruling_text for r in live]

    def test_split_stable_in_reverse_order(self) -> None:
        """Whichever path runs first, the second one converges on its split."""
        cache, _ = _cache()
        ext = _extractor(cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_12)),
            ),
        ):
            rebuild = ext.extract_from_pdf(b"same-pdf-bytes")

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_13)),
            ) as live_llm,
        ):
            live = ext.extract_from_pdf(b"same-pdf-bytes", metadata=LIVE_METADATA)

        live_llm.assert_not_called()
        assert _split(live) == _split(rebuild)

    def test_page_cache_shared_across_metadata(self) -> None:
        """A document-level miss (e.g. an earlier page failed) re-sends no
        page that already succeeded, whatever metadata the caller brings."""
        cache, s3 = _cache()
        ext = _extractor(cache)
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_12)),
            ),
        ):
            ext.extract_from_pdf(b"pdf-a", metadata=LIVE_METADATA)
        # Drop the document-level entries so only the page entry remains.
        for key in [k for k in s3.objects if "/pages/" not in k]:
            del s3.objects[key]

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_13)),
            ) as second_llm,
        ):
            rulings = ext.extract_from_pdf(b"pdf-a")

        second_llm.assert_not_called()
        assert [r.extracted_case_number for r in rulings] == [c[0] for c in CASES]

    def test_llm_page_message_carries_no_metadata(self) -> None:
        """The LLM input is a function of the page image alone."""
        ext = _extractor(None)
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_12)),
            ) as llm,
        ):
            ext.extract_from_pdf(b"pdf", metadata=LIVE_METADATA)

        message = llm.call_args.kwargs["text_message"]
        assert "Colover" not in message
        assert "C34" not in message

    def test_metadata_still_overrides_judge_and_department(self) -> None:
        """Metadata is authoritative for judge / department / hearing date,
        on both the fresh and the cache-hit path."""
        cache, _ = _cache()
        ext = _extractor(cache)
        metadata = {**LIVE_METADATA, "hearing_date": "2026-09-24"}
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_12)),
            ),
        ):
            fresh = ext.extract_from_pdf(b"pdf", metadata=metadata)
            hit = ext.extract_from_pdf(b"pdf", metadata=metadata)
            bare = ext.extract_from_pdf(b"pdf")

        for rulings in (fresh, hit):
            assert {r.extracted_judge_name for r in rulings} == {"Shaina H. Colover"}
            assert {r.department for r in rulings} == {"C34"}
            assert {r.hearing_date for r in rulings} == {"2026-09-24"}
        # The cached entry itself carries no caller metadata.
        assert {r.extracted_judge_name for r in bare} == {None}


# ---------------------------------------------------------------------------
# 2. Entries under the old metadata-bearing keys are served and promoted
# ---------------------------------------------------------------------------


class TestLegacyMetadataKeyedEntries:
    def _seed_legacy_doc(self, s3: _FakeS3, cache: _LlmCache, pdf: bytes) -> None:
        rulings = [
            ExtractedRuling(
                extracted_case_number=num,
                ruling_text=text,
                extracted_judge_name=LIVE_METADATA["judge_name"],
                department=LIVE_METADATA["department"],
            ).model_dump(mode="json")
            for num, _title, text in CASES
        ]
        cache.put(PDF_PER_PAGE_PROMPT, _content_hash_for_cache(pdf, LIVE_METADATA), rulings)

    def test_legacy_doc_entry_served_without_llm_and_promoted(self) -> None:
        cache, s3 = _cache()
        self._seed_legacy_doc(s3, cache, b"pdf")
        ext = _extractor(cache)

        with patch("ingestion.llm_providers.call_llm_with_images") as llm:
            first = ext.extract_from_pdf(b"pdf", metadata=LIVE_METADATA)
        llm.assert_not_called()
        assert [r.extracted_case_number for r in first] == [c[0] for c in CASES]
        # Promoted: a caller with no metadata now gets the same split.
        assert cache.get(PDF_PER_PAGE_PROMPT, _content_hash_for_cache(b"pdf")) is not None
        with patch("ingestion.llm_providers.call_llm_with_images") as llm2:
            second = ext.extract_from_pdf(b"pdf")
        llm2.assert_not_called()
        assert _split(second) == _split(first)

    def test_bytes_only_entry_wins_over_legacy_entry(self) -> None:
        """When both exist, every caller gets the bytes-only entry."""
        cache, s3 = _cache()
        self._seed_legacy_doc(s3, cache, b"pdf")
        canonical = [
            ExtractedRuling(extracted_case_number=CASES[0][0], ruling_text=CASES[0][2]).model_dump(
                mode="json"
            )
        ]
        cache.put(PDF_PER_PAGE_PROMPT, _content_hash_for_cache(b"pdf"), canonical)
        ext = _extractor(cache)

        with patch("ingestion.llm_providers.call_llm_with_images") as llm:
            live = ext.extract_from_pdf(b"pdf", metadata=LIVE_METADATA)
            rebuild = ext.extract_from_pdf(b"pdf")
        llm.assert_not_called()
        assert [r.extracted_case_number for r in live] == [CASES[0][0]]
        assert _split(live) == _split(rebuild)

    def test_legacy_page_entry_served_and_promoted(self) -> None:
        cache, s3 = _cache()
        img = PAGES[0][0]
        cache.put_page(
            PDF_PER_PAGE_PROMPT,
            _content_hash_for_cache(img, LIVE_METADATA),
            _page_json(PAGE_ROWS_12),
        )
        ext = _extractor(cache)
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch("ingestion.llm_providers.call_llm_with_images") as llm,
        ):
            rulings = ext.extract_from_pdf(b"pdf", metadata=LIVE_METADATA)
        llm.assert_not_called()
        assert [r.extracted_case_number for r in rulings] == [c[0] for c in CASES]
        assert cache.get_page(PDF_PER_PAGE_PROMPT, _content_hash_for_cache(img)) is not None

    def test_bust_cache_ignores_legacy_entries(self) -> None:
        cache, s3 = _cache()
        self._seed_legacy_doc(s3, cache, b"pdf")
        ext = _extractor(cache)
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_12[:1])),
            ) as llm,
        ):
            rulings = ext.extract_from_pdf(b"pdf", metadata=LIVE_METADATA, bust_cache=True)
        llm.assert_called_once()
        assert len(rulings) == 1


# ---------------------------------------------------------------------------
# 3. A calendar preamble row is never a ruling
# ---------------------------------------------------------------------------


class TestCalendarPreambleDropped:
    def test_fresh_extraction_drops_preamble_row(self) -> None:
        ext = _extractor(None)
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_13)),
            ),
        ):
            with_preamble = ext.extract_from_pdf(b"pdf-13")
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_answer(_page_json(PAGE_ROWS_12)),
            ),
        ):
            without = ext.extract_from_pdf(b"pdf-12")

        assert [r.extracted_case_number for r in with_preamble] == [c[0] for c in CASES]
        assert _split(with_preamble) == _split(without)

    def test_cache_hit_drops_preamble_row(self) -> None:
        """An entry cached before this fix (the rebuild's 13-row entry) is
        corrected on read, without a cache bust."""
        cached = [
            ExtractedRuling(ruling_text=PREAMBLE_TEXT, department="C34"),
            *[
                ExtractedRuling(extracted_case_number=num, ruling_text=text, entry_number=i + 1)
                for i, (num, _title, text) in enumerate(CASES)
            ],
        ]
        rulings = _apply_pdf_cache_hit_filters(cached, content_key="k")
        assert [r.extracted_case_number for r in rulings] == [c[0] for c in CASES]

    def test_real_ruling_under_a_page_header_is_kept(self) -> None:
        """A ruling with a case number is never dropped, even when its text
        begins with the department header."""
        cached = [
            ExtractedRuling(
                extracted_case_number=CASES[0][0],
                ruling_text=PREAMBLE_TEXT + "\n\n" + CASES[0][2],
            ),
            ExtractedRuling(extracted_case_number=CASES[1][0], ruling_text=CASES[1][2]),
        ]
        rulings = _apply_pdf_cache_hit_filters(cached, content_key="k")
        assert [r.extracted_case_number for r in rulings] == [CASES[0][0], CASES[1][0]]

    def test_preamble_with_a_case_number_in_text_is_kept(self) -> None:
        """No case number was extracted, but the text names one: keep it."""
        cached = [
            ExtractedRuling(ruling_text=PREAMBLE_TEXT + "\n\n2025-01503259 Demurrer OVERRULED."),
            ExtractedRuling(extracted_case_number=CASES[1][0], ruling_text=CASES[1][2]),
        ]
        rulings = _apply_pdf_cache_hit_filters(cached, content_key="k")
        assert len(rulings) == 2

    def test_lone_preamble_is_not_dropped_to_empty(self) -> None:
        cached = [ExtractedRuling(ruling_text=PREAMBLE_TEXT)]
        assert len(_apply_pdf_cache_hit_filters(cached, content_key="k")) == 1
