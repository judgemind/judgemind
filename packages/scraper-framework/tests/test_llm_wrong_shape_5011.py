"""Regression tests for #5011: a wrong-shape text LLM response is a failed
chunk, never an empty success.

A text chunk's answer must be ``{"rulings": [ ... ]}``.  Before #5011,
``{"rulings": null}``, a missing ``rulings`` key, or non-object items parsed
as a successful chunk with zero rulings.  On a multi-chunk document the
chunk's rulings were silently dropped and the rest cached as complete; on a
single-chunk document the empty result passed for a verdict that the
document holds no rulings, which on reingest replaced the key's split rows
with one whole-document row.

An explicit ``{"rulings": []}`` stays a valid chunk with no rulings.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from framework.llm_extractor import (
    ExtractionIncompleteError,
    LlmExtractor,
    PartialExtractionError,
    TokenUsage,
)
from framework.llm_utils import rulings_array_shape_error
from ingestion import llm_extract
from ingestion.llm_providers import LLMResponse

GOOD = json.dumps({"rulings": [{"extracted_case_number": "C25-01093", "ruling_text": "Granted."}]})

#: Valid JSON with no valid rulings array, and the shape each one reports.
WRONG_SHAPES = {
    "rulings_null": ('{"rulings": null}', "rulings_NoneType"),
    "missing_rulings": ("{}", "missing_rulings_key"),
    "misnamed_rulings": ('{"ruling": []}', "missing_rulings_key"),
    "rulings_string": ('{"rulings": "none"}', "rulings_str"),
    "rulings_object": ('{"rulings": {"case": "C25-01093"}}', "rulings_dict"),
    "non_object_item": ('{"rulings": ["x"]}', "non_object_ruling"),
    "mixed_items": (
        '{"rulings": [{"extracted_case_number": "C25-01093"}, null]}',
        "non_object_ruling",
    ),
    "top_level_list": ('[{"extracted_case_number": "C25-01093"}]', "top_level_list"),
    "top_level_scalar": ('"no rulings"', "scalar"),
}


def _resp(text: str) -> LLMResponse:
    return LLMResponse(text=text, input_tokens=10, output_tokens=10, finish_reason="STOP")


def _google_extractor(cache: object | None = None) -> LlmExtractor:
    with patch("framework.llm_extractor._create_google_client", return_value=MagicMock()):
        ext = LlmExtractor(provider="google", model="gemini-2.5-flash-lite")
    ext._cache = cache
    return ext


def _cache() -> MagicMock:
    cache = MagicMock()
    cache.get.return_value = None
    return cache


def _two_chunk_text(ext: LlmExtractor) -> str:
    """A document the extractor sends as two chunks, each large enough to
    be retried in halves when it fails."""
    page = "CASE C25-01093 tentative ruling paragraph.\n\n" * 60
    ext._split_text = MagicMock(return_value=([page, page], True))  # type: ignore[method-assign]
    return page + "\f" + page


# ---------------------------------------------------------------------------
# The shape check
# ---------------------------------------------------------------------------


class TestRulingsArrayShape:
    @pytest.mark.parametrize("name", sorted(WRONG_SHAPES))
    def test_wrong_shape_reported(self, name: str) -> None:
        raw, shape = WRONG_SHAPES[name]
        assert rulings_array_shape_error(json.loads(raw)) == shape

    def test_explicit_empty_rulings_is_valid(self) -> None:
        assert rulings_array_shape_error({"rulings": []}) is None

    def test_rulings_of_objects_is_valid(self) -> None:
        assert rulings_array_shape_error(json.loads(GOOD)) is None


# ---------------------------------------------------------------------------
# LlmExtractor: the text path
# ---------------------------------------------------------------------------


class TestTextParseWrongShape:
    @pytest.mark.parametrize("name", sorted(WRONG_SHAPES))
    def test_wrong_shape_parse_is_failed_chunk(self, name: str) -> None:
        assert LlmExtractor._parse_response(WRONG_SHAPES[name][0], None) is None

    def test_wrong_shape_parse_explicit_empty_is_valid_chunk(self) -> None:
        result = LlmExtractor._parse_response('{"rulings": []}', None)
        assert result is not None
        assert result.rulings == []

    @pytest.mark.parametrize("name", ["rulings_null", "missing_rulings", "non_object_item"])
    def test_wrong_shape_single_chunk_strict_raises_and_caches_nothing(self, name: str) -> None:
        cache = _cache()
        ext = _google_extractor(cache)
        calls = MagicMock(return_value=_resp(WRONG_SHAPES[name][0]))

        with (
            patch("ingestion.llm_providers.call_llm", calls),
            pytest.raises(ExtractionIncompleteError),
        ):
            ext.extract("CASE C25-01093 short tentative ruling.", strict=True)

        cache.put.assert_not_called()

    def test_wrong_shape_single_chunk_is_retried_in_halves_then_fails(self) -> None:
        """A large failed chunk is retried in halves, like any failed chunk."""
        ext = _google_extractor(_cache())
        calls = MagicMock(return_value=_resp('{"rulings": null}'))

        with (
            patch("ingestion.llm_providers.call_llm", calls),
            pytest.raises(ExtractionIncompleteError),
        ):
            ext.extract("paragraph text.\n\n" * 400, strict=True)

        assert calls.call_count == 3
        ext._cache.put.assert_not_called()

    def test_wrong_shape_half_recovers_on_retry(self) -> None:
        """One half answering properly after a malformed whole is complete."""
        ext = _google_extractor(_cache())
        empty = '{"rulings": []}'
        calls = MagicMock(side_effect=[_resp("{}"), _resp(GOOD), _resp(empty)])

        with patch("ingestion.llm_providers.call_llm", calls):
            rulings = ext.extract("paragraph text.\n\n" * 400, strict=True)

        assert [r.extracted_case_number for r in rulings] == ["C25-01093"]
        ext._cache.put.assert_called_once()

    def test_wrong_shape_single_chunk_not_strict_returns_empty_uncached(self) -> None:
        cache = _cache()
        ext = _google_extractor(cache)

        with patch("ingestion.llm_providers.call_llm", return_value=_resp('{"rulings": null}')):
            assert ext.extract("CASE C25-01093 short.", strict=False) == []

        cache.put.assert_not_called()

    @pytest.mark.parametrize("name", ["rulings_null", "missing_rulings", "non_object_item"])
    def test_wrong_shape_multi_chunk_is_partial_and_caches_nothing(self, name: str) -> None:
        cache = _cache()
        ext = _google_extractor(cache)
        text = _two_chunk_text(ext)
        # Chunk 0 answers; chunk 1 is malformed on its first call and on
        # both halves of its retry.
        bad = _resp(WRONG_SHAPES[name][0])
        calls = MagicMock(side_effect=[_resp(GOOD), bad, bad, bad])

        with (
            patch("ingestion.llm_providers.call_llm", calls),
            pytest.raises(PartialExtractionError) as exc_info,
        ):
            ext.extract(text, strict=True)

        assert [r.extracted_case_number for r in exc_info.value.rulings] == ["C25-01093"]
        cache.put.assert_not_called()

    def test_wrong_shape_multi_chunk_not_strict_is_never_cached(self) -> None:
        cache = _cache()
        ext = _google_extractor(cache)
        text = _two_chunk_text(ext)
        bad = _resp('{"rulings": null}')

        with patch(
            "ingestion.llm_providers.call_llm",
            MagicMock(side_effect=[_resp(GOOD), bad, bad, bad]),
        ):
            rulings = ext.extract(text, strict=False)

        assert [r.extracted_case_number for r in rulings] == ["C25-01093"]
        cache.put.assert_not_called()

    def test_wrong_shape_explicit_empty_single_chunk_is_valid(self) -> None:
        """``{"rulings": []}`` is the model's verdict: no rulings, no error."""
        ext = _google_extractor(_cache())

        with patch("ingestion.llm_providers.call_llm", return_value=_resp('{"rulings": []}')):
            assert ext.extract("Calendar: all matters continued.", strict=True) == []

    def test_wrong_shape_explicit_empty_multi_chunk_is_complete(self) -> None:
        cache = _cache()
        ext = _google_extractor(cache)
        text = _two_chunk_text(ext)
        calls = MagicMock(side_effect=[_resp(GOOD), _resp('{"rulings": []}')])

        with patch("ingestion.llm_providers.call_llm", calls):
            rulings = ext.extract(text, strict=True)

        assert [r.extracted_case_number for r in rulings] == ["C25-01093"]
        cache.put.assert_called_once()

    def test_wrong_shape_logged(self) -> None:
        with patch("framework.llm_extractor.logger") as logger:
            LlmExtractor._parse_response('{"rulings": null}', None)
        shape_calls = [
            c.kwargs
            for c in logger.warning.call_args_list
            if c.args == ("llm_extractor.chunk_shape_error",)
        ]
        assert shape_calls and shape_calls[0]["shape"] == "rulings_NoneType"

    def test_wrong_shape_anthropic_chunk_is_failure(self) -> None:
        with patch.object(anthropic, "Anthropic"):
            ext = LlmExtractor(api_key="test-key")
        ext._client.messages.create.return_value = SimpleNamespace(
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
            content=[SimpleNamespace(text='{"rulings": null}')],
        )

        assert ext._extract_chunk("text", usage=TokenUsage()) is None


# ---------------------------------------------------------------------------
# ingestion.llm_extract: per-field extraction
# ---------------------------------------------------------------------------


class TestFieldExtractionWrongShape:
    @pytest.mark.parametrize("name", sorted(WRONG_SHAPES))
    def test_wrong_shape_field_parse_is_failed_chunk(self, name: str) -> None:
        assert llm_extract._parse_response(WRONG_SHAPES[name][0], None) is None

    def test_wrong_shape_field_parse_explicit_empty_is_valid(self) -> None:
        result = llm_extract._parse_response('{"judge_name": "Pegg", "rulings": []}', None)
        assert result is not None
        assert result.rulings == []

    def test_wrong_shape_field_extraction_is_failure_and_not_cached(self) -> None:
        cache = _cache()
        with (
            patch.object(llm_extract, "_get_llm_cache", return_value=cache),
            patch.object(llm_extract, "call_llm", return_value=_resp('{"rulings": null}')),
        ):
            result = llm_extract.extract_fields_llm("Some ruling text.", "html")

        assert result is None
        cache.put.assert_not_called()
