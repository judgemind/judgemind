"""Regression tests for #5007: deterministic LLM failures on specific inputs.

Three counties hit the same class of failure: the model fails the same way
on every retry of the same input, so a plain retry never helps.

* Orange multimodal pages came back empty because Gemini withheld them as a
  recitation (``finish_reason=RECITATION``: the page quotes a statute
  verbatim).  The per-page retry re-sent the identical request.
* An Orange page and the Santa Clara (#4994) / Contra Costa (#5025) text
  splits came back as JSON with an unescaped ``"`` (or ``\\s``) copied from
  the source.  ``finish_reason`` was ``STOP``: not truncation.  Splitting
  the chunk in half kept the stray quote in one half.

The fixes pinned here:

1. Every LLM response carries ``finish_reason`` / ``block_reason`` /
   safety ratings, and every empty, cut-off, or unparseable response logs
   them (instrumentation).
2. A page's retry is a recovery request: JSON mode plus the page's own text
   layer (and twice the output budget after ``MAX_TOKENS``).
3. ``parse_llm_json`` repairs a stray unescaped quote or backslash.
4. A cut-off or withheld response is never a complete result, even when its
   text parses, so it is never cached or written.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import pymupdf
import pytest

from framework import llm_extractor as llm_extractor_mod
from framework.llm_extractor import (
    PAGE_TEXT_LAYER_HEADER,
    LlmExtractor,
    PartialExtractionError,
    TokenUsage,
    _finish_reasons_suffix,
    _pdf_page_text_layers,
)
from framework.llm_utils import parse_llm_json
from ingestion import llm_extract
from ingestion.llm_providers import (
    LLMResponse,
    _anthropic_finish_reason,
    _google_response,
    response_diagnostics,
)

PAGE_OK_JSON = json.dumps(
    {
        "page_header": None,
        "rulings": [
            {
                "entry_number": "",
                "case_number": "",
                "case_title": "",
                "ruling_text": "Defendants also argue that these damages fail.",
            }
        ],
    }
)


def _resp(text: str, finish_reason: str | None = "STOP", **kwargs: object) -> LLMResponse:
    return LLMResponse(
        text=text, input_tokens=2099, output_tokens=len(text), finish_reason=finish_reason, **kwargs
    )


def _warnings(logger: MagicMock, event: str) -> list[dict[str, object]]:
    """The keyword fields of each ``logger.warning(event, ...)`` call.  The
    module logger is patched: structlog's cached loggers can escape
    ``capture_logs`` once another test configured structlog."""
    return [c.kwargs for c in logger.warning.call_args_list if c.args == (event,)]


def _recitation() -> LLMResponse:
    return LLMResponse(text="", input_tokens=2099, output_tokens=0, finish_reason="RECITATION")


def _make_extractor(cache: object | None = None) -> LlmExtractor:
    with patch.object(anthropic, "Anthropic"):
        ext = LlmExtractor(api_key="test-key")
    ext._base_delay = 0.0
    ext._max_retries = 1
    ext._cache = cache
    return ext


def _make_google_extractor() -> LlmExtractor:
    with patch("framework.llm_extractor._create_google_client", return_value=MagicMock()):
        ext = LlmExtractor(provider="google", model="gemini-2.5-flash-lite")
    ext._cache = None
    return ext


def _pdf_with_text(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


# ---------------------------------------------------------------------------
# 1. Provider responses carry the finish reason
# ---------------------------------------------------------------------------


class TestProviderFinishReason:
    def test_google_response_reads_finish_reason_safety_and_block(self) -> None:
        response = SimpleNamespace(
            text=None,
            usage_metadata=SimpleNamespace(prompt_token_count=2099, candidates_token_count=None),
            candidates=[
                SimpleNamespace(
                    finish_reason=SimpleNamespace(name="RECITATION"),
                    safety_ratings=[
                        SimpleNamespace(
                            category=SimpleNamespace(name="HARM_CATEGORY_HARASSMENT"),
                            probability=SimpleNamespace(name="NEGLIGIBLE"),
                            blocked=None,
                        ),
                        SimpleNamespace(
                            category=SimpleNamespace(name="HARM_CATEGORY_HATE_SPEECH"),
                            probability=SimpleNamespace(name="MEDIUM"),
                            blocked=True,
                        ),
                    ],
                )
            ],
            prompt_feedback=SimpleNamespace(block_reason="BlockedReason.OTHER"),
        )

        result = _google_response(response)

        assert result.text == ""
        assert result.input_tokens == 2099
        assert result.output_tokens == 0
        assert result.finish_reason == "RECITATION"
        assert result.block_reason == "OTHER"
        assert result.safety_ratings == ["HARM_CATEGORY_HATE_SPEECH:MEDIUM:blocked"]
        assert result.incomplete
        assert not result.truncated

    def test_google_response_tolerates_text_property_raising(self) -> None:
        class _Blocked:
            usage_metadata = None
            candidates: list = []
            prompt_feedback = SimpleNamespace(block_reason="BLOCKED_REASON_UNSPECIFIED")

            @property
            def text(self) -> str:
                raise ValueError("no parts")

        result = _google_response(_Blocked())

        assert result.text == ""
        assert result.finish_reason is None
        assert result.block_reason is None
        assert not result.incomplete

    def test_google_call_returns_finish_reason(self) -> None:
        from ingestion.llm_providers import call_llm

        sdk_response = SimpleNamespace(
            text='{"a": 1',
            usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=32768),
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS", safety_ratings=None)],
            prompt_feedback=None,
        )
        client = MagicMock()
        client.models.generate_content.return_value = sdk_response

        result = call_llm("sys", "user", provider="google", client=client)

        assert result is not None
        assert result.finish_reason == "MAX_TOKENS"
        assert result.truncated
        assert result.incomplete

    def test_google_vision_json_mode_sets_response_mime_type(self) -> None:
        from ingestion.llm_providers import call_llm_with_images

        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(
            text="{}",
            usage_metadata=None,
            candidates=[SimpleNamespace(finish_reason="STOP", safety_ratings=[])],
            prompt_feedback=None,
        )

        call_llm_with_images(
            "sys", "msg", [(b"img", "image/png")], provider="google", client=client, json_mode=True
        )
        call_llm_with_images(
            "sys", "msg", [(b"img", "image/png")], provider="google", client=client
        )

        first, second = client.models.generate_content.call_args_list
        assert first.kwargs["config"].response_mime_type == "application/json"
        assert second.kwargs["config"].response_mime_type is None

    @pytest.mark.parametrize(
        ("stop_reason", "expected"),
        [
            ("end_turn", "STOP"),
            ("stop_sequence", "STOP"),
            ("max_tokens", "MAX_TOKENS"),
            ("refusal", "SAFETY"),
            ("pause_turn", "PAUSE_TURN"),
            (None, None),
        ],
    )
    def test_anthropic_finish_reason_normalized(
        self, stop_reason: str | None, expected: str | None
    ) -> None:
        assert _anthropic_finish_reason(SimpleNamespace(stop_reason=stop_reason)) == expected

    def test_diagnostics_and_safe_wrapper(self) -> None:
        response = _resp("abc", finish_reason="STOP")

        assert response.diagnostics() == {
            "finish_reason": "STOP",
            "block_reason": None,
            "safety_ratings": [],
            "input_tokens": 2099,
            "output_tokens": 3,
            "thinking_tokens": None,
            "response_chars": 3,
        }
        assert response_diagnostics(response) == response.diagnostics()
        assert response_diagnostics(MagicMock()) == {}
        assert not response.incomplete
        assert LLMResponse(
            text="", input_tokens=0, output_tokens=0, block_reason="SAFETY"
        ).incomplete


# ---------------------------------------------------------------------------
# 2. Multimodal pages: finish reason logged, recovery request on retry
# ---------------------------------------------------------------------------


class TestPageFinishReason:
    def test_page_finish_reason_logged_and_recitation_recovered_with_text_layer(self) -> None:
        ext = _make_extractor()
        calls = MagicMock(side_effect=[_recitation(), _resp(PAGE_OK_JSON)])

        with (
            patch("ingestion.llm_providers.call_llm_with_images", calls),
            patch.object(llm_extractor_mod, "logger") as logger,
        ):
            page = ext._extract_single_page(
                b"img",
                "image/png",
                usage=TokenUsage(),
                page_index=48,
                document_id="doc-1",
                page_text="Defendants also argue that these damages fail.",
            )

        assert page.status == "ok"
        assert len(page.rows) == 1
        failed = _warnings(logger, "llm_extractor.page_response_failed")
        assert len(failed) == 1
        assert failed[0]["finish_reason"] == "RECITATION"
        assert failed[0]["page_index"] == 48
        assert failed[0]["document_id"] == "doc-1"
        assert failed[0]["input_tokens"] == 2099
        assert failed[0]["response_chars"] == 0
        first, retry = calls.call_args_list
        assert first.kwargs["json_mode"] is False
        assert PAGE_TEXT_LAYER_HEADER not in first.kwargs["text_message"]
        assert retry.kwargs["json_mode"] is True
        assert PAGE_TEXT_LAYER_HEADER in retry.kwargs["text_message"]
        assert "Defendants also argue" in retry.kwargs["text_message"]

    def test_page_finish_reason_recorded_when_recovery_also_fails(self) -> None:
        ext = _make_extractor()

        with patch(
            "ingestion.llm_providers.call_llm_with_images",
            side_effect=[_recitation(), _recitation()],
        ):
            page = ext._extract_single_page(b"img", "image/png", usage=TokenUsage())

        assert page.status == "parse_error"
        assert page.finish_reason == "RECITATION"

    def test_page_finish_reason_max_tokens_is_failure_even_if_text_parses(self) -> None:
        ext = _make_extractor()
        ext._max_output_tokens = 32768
        calls = MagicMock(
            side_effect=[_resp(PAGE_OK_JSON, finish_reason="MAX_TOKENS"), _resp(PAGE_OK_JSON)]
        )

        with patch("ingestion.llm_providers.call_llm_with_images", calls):
            page = ext._extract_single_page(b"img", "image/png", usage=TokenUsage())

        assert page.status == "ok"
        first, retry = calls.call_args_list
        assert first.kwargs["max_tokens"] == 32768
        assert retry.kwargs["max_tokens"] == 65536

    def test_page_finish_reason_in_partial_error_and_nothing_cached(self) -> None:
        cache = MagicMock()
        cache.get.return_value = None
        cache.get_page.return_value = None
        ext = _make_extractor(cache)
        pages = [(b"\x89PNG_page0", "image/png"), (b"\x89PNG_page1", "image/png")]

        def _by_image(**kwargs: object) -> LLMResponse:
            images = kwargs["images"]
            assert isinstance(images, list)
            return _resp(PAGE_OK_JSON) if images[0][0] == pages[0][0] else _recitation()

        with (
            patch("framework.llm_extractor._render_pdf_pages", return_value=pages),
            patch("framework.llm_extractor._pdf_page_count", return_value=2),
            patch("ingestion.llm_providers.call_llm_with_images", side_effect=_by_image),
            pytest.raises(PartialExtractionError) as excinfo,
        ):
            ext.extract_from_pdf(b"%PDF-fake", strict=True)

        assert "(finish_reason: 1=RECITATION)" in str(excinfo.value)
        assert excinfo.value.failed_parts == [1]
        cache.put.assert_not_called()
        # Only the page that parsed is cached.
        assert cache.put_page.call_count == 1

    def test_finish_reasons_suffix(self) -> None:
        assert _finish_reasons_suffix({}) == ""
        assert _finish_reasons_suffix({3: None}) == ""
        assert _finish_reasons_suffix({31: "RECITATION", 41: "RECITATION"}) == (
            " (finish_reason: 31=RECITATION, 41=RECITATION)"
        )


class TestPdfPageTextLayers:
    def test_text_layers_reflowed_per_page(self) -> None:
        pdf = _pdf_with_text(["First page text", "Second page"])

        layers = _pdf_page_text_layers(pdf, 5)

        assert layers == ["First page text", "Second page"]
        assert _pdf_page_text_layers(pdf, 1) == ["First page text"]

    def test_unreadable_pdf_gives_no_layers(self) -> None:
        assert _pdf_page_text_layers(b"not a pdf", 3) == []


# ---------------------------------------------------------------------------
# 3. Malformed JSON: a stray quote or backslash is repaired
# ---------------------------------------------------------------------------

_STRAY_QUOTE_JSON = (
    '{"rulings": [{"extracted_case_number": "25PR201409", '
    '"ruling_text": "the purported will (\u201c . . . I changed my will to read thusly:") '
    'suggests a past change."}, {"extracted_case_number": "25PR199483", '
    '"ruling_text": "Motion to be relieved as counsel GRANTED."}]}'
)


class TestMalformedJsonRepair:
    def test_malformed_json_chunk_stray_quote_repaired(self) -> None:
        parsed = parse_llm_json(_STRAY_QUOTE_JSON)

        assert [r["extracted_case_number"] for r in parsed["rulings"]] == [
            "25PR201409",
            "25PR199483",
        ]
        assert 'thusly:") suggests' in parsed["rulings"][0]["ruling_text"]

    def test_malformed_json_chunk_quote_before_comma_repaired(self) -> None:
        text = '{"a": "He said "yes", and left.", "b": "c"}'

        assert parse_llm_json(text) == {"a": 'He said "yes", and left.', "b": "c"}

    def test_malformed_json_chunk_invalid_escape_repaired(self) -> None:
        text = '{"a": "path C:\\server\\share", "b": 1}'

        assert parse_llm_json(text) == {"a": "path C:\\server\\share", "b": 1}

    def test_malformed_json_chunk_missing_comma_not_repaired(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_llm_json('{"a": "x" "b": "y"}')

    def test_malformed_json_chunk_truncated_not_repaired(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_llm_json('{"a": "unterminated text')

    def test_malformed_json_chunk_escaped_quote_not_rewritten(self) -> None:
        # A decode error right after an already-escaped quote is not a stray.
        with pytest.raises(json.JSONDecodeError):
            parse_llm_json('{"a": \\"x" y}')

    def test_malformed_json_chunk_extractor_recovers_without_retry(self) -> None:
        ext = _make_google_extractor()
        calls = MagicMock(return_value=_resp(_STRAY_QUOTE_JSON))

        with patch("ingestion.llm_providers.call_llm", calls):
            rulings = ext.extract("Estate of Robbins ... " * 200, strict=True)

        assert calls.call_count == 1
        assert sorted(r.extracted_case_number for r in rulings) == ["25PR199483", "25PR201409"]


class TestLongSingleOrder:
    def test_long_single_order_keeps_case_number(self) -> None:
        """#4994: a ~20k-char single order whose ruling text quotes a will.

        The response carried an unescaped quote at the same offset on every
        attempt; the retries in halves then dropped the first order's case
        number.  Repaired, one call returns both orders.
        """
        long_text = "The Public Guardian contends " + ("the will is not valid. " * 900)
        response = json.dumps(
            {
                "rulings": [
                    {"extracted_case_number": "25PR201409", "ruling_text": "PLACEHOLDER"},
                    {"extracted_case_number": "25PR199483", "ruling_text": "Granted."},
                ]
            }
        ).replace("PLACEHOLDER", long_text + '(\u201c . . . thusly:") ends here')
        ext = _make_google_extractor()

        with patch("ingestion.llm_providers.call_llm", return_value=_resp(response)) as calls:
            rulings = ext.extract("ocr text " * 3000, strict=True)

        assert calls.call_count == 1
        by_case = {r.extracted_case_number: r for r in rulings}
        assert set(by_case) == {"25PR201409", "25PR199483"}
        assert len(by_case["25PR201409"].ruling_text or "") > 20000


# ---------------------------------------------------------------------------
# 4. A cut-off or withheld text response is never complete
# ---------------------------------------------------------------------------


class TestIncompleteTextResponses:
    def test_text_chunk_max_tokens_is_retried_in_halves(self) -> None:
        ext = _make_google_extractor()
        good = json.dumps({"rulings": [{"extracted_case_number": "C25-01093"}]})
        calls = MagicMock(
            side_effect=[_resp(good, finish_reason="MAX_TOKENS"), _resp(good), _resp(good)]
        )

        with (
            patch("ingestion.llm_providers.call_llm", calls),
            patch.object(llm_extractor_mod, "logger") as logger,
        ):
            rulings = ext.extract("paragraph text.\n\n" * 400, strict=True)

        assert calls.call_count == 3
        assert [r.extracted_case_number for r in rulings] == ["C25-01093"]
        failed = _warnings(logger, "llm_extractor.chunk_response_failed")
        assert failed[0]["finish_reason"] == "MAX_TOKENS"

    def test_anthropic_chunk_max_tokens_is_failure(self) -> None:
        ext = _make_extractor()
        good = json.dumps({"rulings": [{"extracted_case_number": "C25-01093"}]})
        ext._client.messages.create.return_value = SimpleNamespace(
            stop_reason="max_tokens",
            usage=SimpleNamespace(input_tokens=10, output_tokens=4096),
            content=[SimpleNamespace(text=good)],
        )

        result = ext._extract_chunk("text", usage=TokenUsage())

        assert result is None

    def test_ocr_page_recitation_is_failure_not_blank(self) -> None:
        with patch.object(llm_extract, "call_llm_with_images", return_value=_recitation()):
            text = llm_extract._ocr_single_page(b"img", "image/png", page_index=3, timeout=10)

        assert text is None

    def test_field_extraction_chunk_max_tokens_is_failure(self) -> None:
        good = json.dumps({"judge_name": "Pegg", "rulings": []})

        with (
            patch.object(llm_extract, "_get_llm_cache", return_value=None),
            patch.object(
                llm_extract, "call_llm", return_value=_resp(good, finish_reason="MAX_TOKENS")
            ),
            patch.object(llm_extract, "logger") as logger,
        ):
            result = llm_extract.extract_fields_llm("Some ruling text.", "html")

        assert result is None
        failed = _warnings(logger, "llm_extract.chunk_response_failed")
        assert failed[0]["finish_reason"] == "MAX_TOKENS"
