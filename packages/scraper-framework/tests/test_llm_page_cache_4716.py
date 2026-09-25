"""Regression tests for #4716: multimodal PDF extraction caching.

Before #4716, ``LlmExtractor.extract_from_pdf`` skipped the cache write
whenever *any* page returned zero rows.  That lumped together two very
different outcomes:

* a page whose LLM call and JSON parse succeeded but which legitimately
  carries no rulings (boilerplate / header-only pages — explicitly allowed
  by ``PDF_PER_PAGE_PROMPT``), and
* a page whose API call failed or whose response did not parse as JSON.

The first is a complete result and must be cacheable; the second is a
failure that must not be cached as "empty".  A 40-page Orange PDF with one
unparseable page was re-sent to Gemini in full on every run (~135s of LLM
time each time).

These tests pin the new contract:

1. Valid-JSON zero-row pages do not block the document-level cache write.
2. A page whose response fails to parse is retried (with a stricter
   "return valid JSON" nudge) before it is treated as failed.
3. Pages that succeeded are cached individually, so a document with a
   persistently failing page only re-sends the failed page on later runs.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import anthropic

from framework.llm_extractor import (
    PAGE_JSON_RETRY_NUDGE,
    LlmExtractor,
    _LlmCache,
    _PageExtraction,
    _parse_page_rows_with_status,
)

PAGE1_JSON = json.dumps(
    [
        {
            "entry_number": 1,
            "case_info": "2024-01393434 Martinez v. ABC Manufacturing Inc.",
            "ruling_text": "The motion for summary adjudication is DENIED.",
        }
    ]
)
PAGE3_JSON = json.dumps(
    [
        {
            "entry_number": 2,
            "case_info": "2024-00567890 Garcia v. State Farm Insurance",
            "ruling_text": "The motion to compel further responses is GRANTED.",
        }
    ]
)
EMPTY_PAGE_JSON = "[]"
BOILERPLATE_PAGE_JSON = json.dumps({"page_header": None, "rulings": []})
UNPARSEABLE = 'Here are the rulings: [{"entry_number": 1, "case_info": "2024-0'

PAGES = [
    (b"\x89PNG_page1", "image/png"),
    (b"\x89PNG_page2", "image/png"),
    (b"\x89PNG_page3", "image/png"),
]


def _resp(text: str) -> object:
    from ingestion.llm_providers import LLMResponse

    return LLMResponse(text=text, input_tokens=10, output_tokens=5)


def _make_extractor(cache: object | None = None) -> LlmExtractor:
    with patch.object(anthropic, "Anthropic"):
        ext = LlmExtractor(api_key="test-key")
    ext._base_delay = 0.0
    ext._max_retries = 1
    ext._cache = cache
    return ext


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


def _real_cache() -> tuple[_LlmCache, _FakeS3]:
    s3 = _FakeS3()
    return _LlmCache(s3, "bucket", "anthropic", "test-model"), s3


def _by_image(responses: dict[bytes, list[str | None]]) -> Callable[..., object]:
    """side_effect for call_llm_with_images that answers per page image."""
    queues = {k: list(v) for k, v in responses.items()}

    def _call(**kwargs: Any) -> object:
        img = kwargs["images"][0][0]
        text = queues[img].pop(0)
        return None if text is None else _resp(text)

    return _call


# ---------------------------------------------------------------------------
# AC1 — a valid-JSON zero-row page does not block the cache write
# ---------------------------------------------------------------------------


class TestEmptyPageIsCacheable:
    def test_cache_written_when_empty_page_returns_valid_empty_array(self) -> None:
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_cache.get_page.return_value = None
        ext = _make_extractor(mock_cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[_resp(PAGE1_JSON), _resp(EMPTY_PAGE_JSON), _resp(PAGE3_JSON)],
            ),
        ):
            rulings = ext.extract_from_pdf(b"pdf-bytes")

        assert len(rulings) == 2
        mock_cache.put.assert_called_once()

    def test_cache_written_when_empty_page_is_boilerplate_object(self) -> None:
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_cache.get_page.return_value = None
        ext = _make_extractor(mock_cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[
                    _resp(PAGE1_JSON),
                    _resp(BOILERPLATE_PAGE_JSON),
                    _resp(PAGE3_JSON),
                ],
            ),
        ):
            ext.extract_from_pdf(b"pdf-bytes")

        mock_cache.put.assert_called_once()

    def test_second_run_is_cache_hit_with_empty_page(self) -> None:
        """End-to-end with the real ``_LlmCache``: run 2 makes zero LLM calls."""
        cache, _ = _real_cache()
        ext = _make_extractor(cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[_resp(PAGE1_JSON), _resp(EMPTY_PAGE_JSON), _resp(PAGE3_JSON)],
            ) as first_call,
        ):
            first = ext.extract_from_pdf(b"pdf-bytes")
        assert first_call.call_count == 3

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES) as render,
            patch("ingestion.llm_providers.call_llm_with_images") as second_call,
        ):
            second = ext.extract_from_pdf(b"pdf-bytes")

        second_call.assert_not_called()
        render.assert_not_called()  # served from the document-level entry
        assert [r.extracted_case_number for r in second] == [r.extracted_case_number for r in first]


# ---------------------------------------------------------------------------
# AC2 — a page whose JSON fails to parse is retried before it counts as failed
# ---------------------------------------------------------------------------


class TestPageParseErrorRetry:
    def test_page_parse_error_retry_succeeds_and_caches(self) -> None:
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_cache.get_page.return_value = None
        ext = _make_extractor(mock_cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_by_image(
                    {
                        b"\x89PNG_page1": [PAGE1_JSON],
                        b"\x89PNG_page2": [UNPARSEABLE, EMPTY_PAGE_JSON],
                        b"\x89PNG_page3": [PAGE3_JSON],
                    }
                ),
            ) as mock_call,
        ):
            rulings = ext.extract_from_pdf(b"pdf-bytes")

        assert mock_call.call_count == 4
        assert len(rulings) == 2
        # The retry carries the stricter "return valid JSON" nudge.
        retry_kwargs = mock_call.call_args_list[2].kwargs
        assert retry_kwargs["images"][0][0] == b"\x89PNG_page2"
        assert PAGE_JSON_RETRY_NUDGE in retry_kwargs["text_message"]
        first_kwargs = mock_call.call_args_list[1].kwargs
        assert PAGE_JSON_RETRY_NUDGE not in first_kwargs["text_message"]
        # Every page ended with a valid response, so the document is cached.
        mock_cache.put.assert_called_once()

    def test_page_parse_error_retry_exhausted_is_not_cached_as_empty(self) -> None:
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_cache.get_page.return_value = None
        ext = _make_extractor(mock_cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_by_image(
                    {
                        b"\x89PNG_page1": [PAGE1_JSON],
                        b"\x89PNG_page2": [UNPARSEABLE, UNPARSEABLE],
                        b"\x89PNG_page3": [PAGE3_JSON],
                    }
                ),
            ) as mock_call,
        ):
            rulings = ext.extract_from_pdf(b"pdf-bytes")

        # One retry only — bounded cost.
        assert mock_call.call_count == 4
        # The good pages still yield their rulings ...
        assert len(rulings) == 2
        # ... but the partial result is NOT cached at document level (#3517),
        mock_cache.put.assert_not_called()
        # and the failed page is not cached as an empty page.
        cached_raw = [c.args[2] for c in mock_cache.put_page.call_args_list]
        assert cached_raw == [PAGE1_JSON, PAGE3_JSON]

    def test_page_parse_error_retry_api_failure_counts_as_failed(self) -> None:
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_cache.get_page.return_value = None
        ext = _make_extractor(mock_cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES[:1]),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[_resp(UNPARSEABLE), None],
            ),
        ):
            rulings = ext.extract_from_pdf(b"pdf-bytes")

        assert rulings == []
        mock_cache.put.assert_not_called()
        mock_cache.put_page.assert_not_called()

    def test_extract_single_page_reports_status(self) -> None:
        ext = _make_extractor(None)
        from framework.llm_extractor import TokenUsage

        with patch(
            "ingestion.llm_providers.call_llm_with_images",
            side_effect=[_resp(EMPTY_PAGE_JSON), _resp(UNPARSEABLE), _resp(UNPARSEABLE), None],
        ):
            ok = ext._extract_single_page(b"img", "image/png", usage=TokenUsage())
            parse_fail = ext._extract_single_page(b"img", "image/png", usage=TokenUsage())
            api_fail = ext._extract_single_page(b"img", "image/png", usage=TokenUsage())

        assert ok == _PageExtraction(status="ok", rows=[], raw_text=EMPTY_PAGE_JSON)
        assert parse_fail.status == "parse_error"
        assert parse_fail.rows == []
        assert api_fail.status == "api_error"


# ---------------------------------------------------------------------------
# Per-page cache — only failed pages are re-sent on later runs
# ---------------------------------------------------------------------------


class TestPerPageCache:
    def test_only_failed_page_is_resent_on_next_run(self) -> None:
        cache, _ = _real_cache()
        ext = _make_extractor(cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_by_image(
                    {
                        b"\x89PNG_page1": [PAGE1_JSON],
                        b"\x89PNG_page2": [UNPARSEABLE, UNPARSEABLE],
                        b"\x89PNG_page3": [PAGE3_JSON],
                    }
                ),
            ),
        ):
            ext.extract_from_pdf(b"pdf-bytes")

        # Run 2: page 2 now parses.  Pages 1 and 3 come from the page cache.
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=_by_image({b"\x89PNG_page2": [EMPTY_PAGE_JSON]}),
            ) as second_call,
        ):
            second = ext.extract_from_pdf(b"pdf-bytes")

        assert second_call.call_count == 1
        assert second_call.call_args.kwargs["images"][0][0] == b"\x89PNG_page2"
        assert len(second) == 2

        # Run 3: everything succeeded on run 2, so the document entry is a hit.
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES),
            patch("ingestion.llm_providers.call_llm_with_images") as third_call,
        ):
            third = ext.extract_from_pdf(b"pdf-bytes")
        third_call.assert_not_called()
        assert len(third) == 2

    def test_page_cache_key_includes_metadata(self) -> None:
        cache, s3 = _real_cache()
        ext = _make_extractor(cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES[:1]),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[_resp(PAGE1_JSON), _resp(PAGE1_JSON)],
            ) as mock_call,
        ):
            ext.extract_from_pdf(b"pdf-a", metadata={"department": "C25"})
            ext.extract_from_pdf(b"pdf-b", metadata={"department": "N16"})

        # Different metadata => different page prompt => no page-cache reuse.
        assert mock_call.call_count == 2
        page_keys = [k for k in s3.objects if "/pages/" in k]
        assert len(page_keys) == 2

    def test_bust_cache_skips_page_cache_reads_but_writes(self) -> None:
        cache, s3 = _real_cache()
        ext = _make_extractor(cache)

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES[:1]),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[_resp(PAGE1_JSON), _resp(PAGE1_JSON)],
            ) as mock_call,
        ):
            ext.extract_from_pdf(b"pdf-bytes")
            ext.extract_from_pdf(b"pdf-bytes", bust_cache=True)

        assert mock_call.call_count == 2
        assert any("/pages/" in k for k in s3.objects)

    def test_page_cache_hit_is_reparsed(self) -> None:
        """Page entries store the raw response, so parse fixes apply on hit."""
        cache, s3 = _real_cache()
        ext = _make_extractor(cache)
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES[:1]),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[_resp(PAGE1_JSON)],
            ),
        ):
            ext.extract_from_pdf(b"pdf-bytes")

        page_key = next(k for k in s3.objects if "/pages/" in k)
        stored = json.loads(s3.objects[page_key])
        assert stored["raw_text"] == PAGE1_JSON

    def test_malformed_page_cache_entry_is_a_miss(self) -> None:
        cache, s3 = _real_cache()
        ext = _make_extractor(cache)
        # Seed a malformed entry at the page key by running once, then corrupting it.
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES[:1]),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[_resp(EMPTY_PAGE_JSON), _resp(PAGE1_JSON)],
            ) as mock_call,
        ):
            ext.extract_from_pdf(b"pdf-bytes")
            page_key = next(k for k in s3.objects if "/pages/" in k)
            s3.objects[page_key] = b'["not", "a", "page", "entry"]'
            rulings = ext.extract_from_pdf(b"pdf-bytes")

        assert mock_call.call_count == 2
        assert len(rulings) == 1

    def test_page_cache_entry_that_no_longer_parses_is_a_miss(self) -> None:
        cache, s3 = _real_cache()
        ext = _make_extractor(cache)
        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=PAGES[:1]),
            patch(
                "ingestion.llm_providers.call_llm_with_images",
                side_effect=[_resp(EMPTY_PAGE_JSON), _resp(PAGE1_JSON)],
            ) as mock_call,
        ):
            ext.extract_from_pdf(b"pdf-bytes")
            page_key = next(k for k in s3.objects if "/pages/" in k)
            s3.objects[page_key] = json.dumps({"raw_text": UNPARSEABLE}).encode()
            rulings = ext.extract_from_pdf(b"pdf-bytes")

        assert mock_call.call_count == 2
        assert len(rulings) == 1
        # The fresh, parseable response replaced the stale entry.
        assert json.loads(s3.objects[page_key])["raw_text"] == PAGE1_JSON


class TestParsePageRowsWithStatus:
    def test_valid_empty_array_is_ok(self) -> None:
        assert _parse_page_rows_with_status("[]", 0) == (True, [])

    def test_valid_empty_rulings_object_is_ok(self) -> None:
        ok, rows = _parse_page_rows_with_status('{"rulings": []}', 0)
        assert ok is True
        assert rows == []

    def test_unparseable_is_not_ok(self) -> None:
        assert _parse_page_rows_with_status(UNPARSEABLE, 0) == (False, [])

    def test_scalar_json_is_not_ok(self) -> None:
        assert _parse_page_rows_with_status("42", 0) == (False, [])
