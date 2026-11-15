"""Regression tests for #5046: OCR pages Gemini will not finish in one call.

#5010 OCRs the scanned pages of a text PDF.  On dev, 4 San Francisco PDFs
failed on every attempt because one page's vision call came back unusable:

* ``RECITATION`` (0 output tokens): the page is a Judicial Council / CEB
  form, and Gemini withholds a transcription that matches published text.
* ``MAX_TOKENS`` with 0 or 11 counted output tokens: on a dense child
  support table, gemini-2.5-flash-lite loops on spaces (or a digit) until
  the output budget is gone.  It was not thinking tokens (the model reported
  no ``thoughts_token_count``), but a thinking model would make the same
  page fail the same way, so OCR calls now turn thinking off.

Both failures are deterministic for the whole page image but not for a part
of it.  A page whose answer is incomplete is read again as two halves, cut
between lines (or, for a single line, between words), recursively; a strip
still withheld at the end is read as bar-separated words.  A page no tile
can read is still a failed page, with its cause in the recorded reason.
"""

from __future__ import annotations

import io
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pymupdf
from PIL import Image, ImageDraw

from ingestion import llm_extract, llm_providers
from ingestion.llm_providers import LLMResponse, _google_response, _google_thinking_config

LINE_PX = 20  # an inked text line
GAP_PX = 10  # white rows between lines


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _page_image(width: int = 600, height: int = 800) -> Image.Image:
    """A white page of black text lines, ``LINE_PX`` tall, ``GAP_PX`` apart."""
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for y in range(GAP_PX, height - LINE_PX, LINE_PX + GAP_PX):
        draw.rectangle((20, y, width - 20, y + LINE_PX - 1), fill=0)
    return image


def _page_png(width: int = 600, height: int = 800) -> bytes:
    return _png(_page_image(width, height))


def _line_png(width: int = 600, height: int = 30) -> bytes:
    """One line of text: black word blocks with white spaces between them."""
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for x in range(10, width - 40, 50):
        draw.rectangle((x, 5, x + 39, 5 + LINE_PX - 1), fill=0)
    return _png(image)


def _size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as image:
        return image.size


def _ok(text: str) -> LLMResponse:
    return LLMResponse(text=text, input_tokens=10, output_tokens=10, finish_reason="STOP")


def _withheld(reason: str = "RECITATION") -> LLMResponse:
    return LLMResponse(text="", input_tokens=10, output_tokens=0, finish_reason=reason)


def _answers(*responses: LLMResponse) -> MagicMock:
    """A ``call_llm_with_images`` stand-in answering *responses* in call order."""
    queue = iter(responses)
    return MagicMock(side_effect=lambda **_: next(queue))


def _sizes(calls: MagicMock) -> list[tuple[int, int]]:
    return [_size(c.kwargs["images"][0][0]) for c in calls.call_args_list]


# ---------------------------------------------------------------------------
# Thinking budget
# ---------------------------------------------------------------------------


class TestOcrThinkingBudget:
    def test_ocr_thinking_budget_is_zero_on_every_ocr_call(self) -> None:
        calls = _answers(_withheld(), _ok("top"), _ok("bottom"))
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(_page_png(), "image/png", page_index=0, timeout=5)

        assert text == "top\nbottom"
        assert [c.kwargs["thinking_budget"] for c in calls.call_args_list] == [0, 0, 0]

    def test_ocr_thinking_budget_reaches_the_gemini_request(self) -> None:
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(
            text="x", usage_metadata=None, candidates=[], prompt_feedback=None
        )

        llm_providers.call_llm_with_images(
            "sys",
            "msg",
            [(b"png", "image/png")],
            provider="google",
            model="gemini-2.5-flash",
            client=client,
            thinking_budget=0,
        )

        config = client.models.generate_content.call_args.kwargs["config"]
        assert config.thinking_config.thinking_budget == 0

    def test_ocr_thinking_budget_skipped_where_unsupported(self) -> None:
        assert _google_thinking_config("gemini-2.5-flash-lite", 0).thinking_budget == 0
        assert _google_thinking_config("gemini-2.5-pro", 0) is None
        assert _google_thinking_config("gemini-2.5-flash", None) is None

        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(
            text="x", usage_metadata=None, candidates=[], prompt_feedback=None
        )
        llm_providers.call_llm_with_images(
            "sys",
            "msg",
            [(b"png", "image/png")],
            provider="google",
            model="gemini-2.5-pro",
            client=client,
            thinking_budget=0,
        )
        config = client.models.generate_content.call_args.kwargs["config"]
        assert config.thinking_config is None

    def test_thinking_tokens_are_reported(self) -> None:
        response = SimpleNamespace(
            text="",
            usage_metadata=SimpleNamespace(
                prompt_token_count=354, candidates_token_count=None, thoughts_token_count=1198
            ),
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS", safety_ratings=None)],
            prompt_feedback=None,
        )

        parsed = _google_response(response)

        assert parsed.thinking_tokens == 1198
        assert parsed.diagnostics()["thinking_tokens"] == 1198
        assert parsed.output_tokens == 0


# ---------------------------------------------------------------------------
# RECITATION / MAX_TOKENS fallback: read the page in tiles
# ---------------------------------------------------------------------------


class TestOcrRecitationFallback:
    def test_ocr_recitation_fallback_reads_the_page_in_halves(self) -> None:
        calls = _answers(_withheld("RECITATION"), _ok("TOP HALF"), _ok("BOTTOM"))
        failures: dict[int, str] = {}
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(
                _page_png(), "image/png", page_index=20, timeout=5, failures=failures
            )

        assert text == "TOP HALF\nBOTTOM"
        assert failures == {}
        sizes = _sizes(calls)
        assert sizes[0] == (600, 800)
        assert [w for w, _ in sizes[1:]] == [600, 600]
        assert sum(h for _, h in sizes[1:]) == 800
        # The tile request says it is part of a page.
        assert "part of a court document page" in calls.call_args_list[1].kwargs["text_message"]

    def test_ocr_max_tokens_loop_falls_back_to_halves(self) -> None:
        calls = _answers(_withheld("MAX_TOKENS"), _ok("2026 Guideline"), _ok("674"))
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(_page_png(), "image/png", page_index=31, timeout=5)

        assert text == "2026 Guideline\n674"

    def test_ocr_recitation_fallback_recurses_into_a_failing_half(self) -> None:
        calls = _answers(
            _withheld(),  # full page
            _ok("top"),  # top half
            _withheld(),  # bottom half
            _ok("bottom-1"),  # bottom half, cut again between lines
            _ok("bottom-2"),
        )
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(_page_png(), "image/png", page_index=0, timeout=5)

        assert text == "top\nbottom-1\nbottom-2"
        sizes = _sizes(calls)
        # Several lines are always cut top/bottom, never through the lines.
        assert all(w == 600 for w, _ in sizes)
        assert sizes[3][1] + sizes[4][1] == sizes[2][1]

    def test_ocr_recitation_fallback_single_line_is_cut_between_words(self) -> None:
        calls = _answers(_withheld(), _ok("spouse or domestic"), _ok("partner under the"))
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(_line_png(), "image/png", page_index=0, timeout=5)

        assert text == "spouse or domestic partner under the"
        sizes = _sizes(calls)
        assert sizes[1][1] == sizes[2][1] == 30
        assert sizes[1][0] + sizes[2][0] == 600

    def test_ocr_recitation_fallback_last_resort_reads_bar_separated_words(self) -> None:
        tiny = _png(Image.new("L", (15, 15), 0))  # too small to cut
        calls = _answers(_withheld(), _ok("spouse | or | domestic |\n| partner | under"))
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(tiny, "image/png", page_index=0, timeout=5)

        assert text == "spouse or domestic\npartner under"
        words_call = calls.call_args_list[1].kwargs
        assert words_call["system_prompt"] == llm_extract._OCR_WORDS_SYSTEM_PROMPT

    def test_ocr_recitation_fallback_unreadable_page_stays_failed(self) -> None:
        calls = MagicMock(return_value=_withheld("RECITATION"))
        failures: dict[int, str] = {}
        with patch.object(llm_extract, "call_llm_with_images", calls):
            # A letter page at the 150 DPI OCR resolution.
            text = llm_extract._ocr_single_page(
                _page_png(1275, 1650), "image/png", page_index=4, timeout=5, failures=failures
            )

        assert text is None
        assert failures == {4: "RECITATION"}
        # One call per cut (the first failing tile ends it), plus the
        # bar-separated last resort.
        assert calls.call_count <= llm_extract._OCR_TILE_MAX_DEPTH + 2
        last = calls.call_args_list[-1].kwargs
        assert last["system_prompt"] == llm_extract._OCR_WORDS_SYSTEM_PROMPT

    def test_max_tokens_at_the_last_cut_is_not_read_as_words(self) -> None:
        tiny = _png(Image.new("L", (15, 15), 0))
        calls = MagicMock(return_value=_withheld("MAX_TOKENS"))
        failures: dict[int, str] = {}
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(
                tiny, "image/png", page_index=1, timeout=5, failures=failures
            )

        assert text is None
        assert calls.call_count == 1
        assert failures == {1: "MAX_TOKENS"}

    def test_ocr_recitation_fallback_blank_tile_is_skipped(self) -> None:
        calls = _answers(_withheld(), _ok("top"), _ok(""))
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(_page_png(), "image/png", page_index=0, timeout=5)

        assert text == "top"

    def test_no_answer_is_not_tiled(self) -> None:
        calls = MagicMock(return_value=None)
        failures: dict[int, str] = {}
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(
                _page_png(), "image/png", page_index=0, timeout=5, failures=failures
            )

        assert text is None
        assert calls.call_count == 1
        assert failures == {}

    def test_call_error_is_a_failed_page(self) -> None:
        calls = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(_page_png(), "image/png", page_index=0, timeout=5)

        assert text is None
        assert calls.call_count == 1

    def test_undecodable_image_is_read_as_words_once(self) -> None:
        calls = MagicMock(return_value=_withheld())
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(b"img", "image/png", page_index=0, timeout=5)

        assert text is None
        assert calls.call_count == 2  # the page, then the bar-separated last resort

    def test_block_reason_is_the_recorded_cause(self) -> None:
        blocked = LLMResponse(text="", input_tokens=1, output_tokens=0, block_reason="OTHER")
        failures: dict[int, str] = {}
        with patch.object(llm_extract, "call_llm_with_images", MagicMock(return_value=blocked)):
            llm_extract._ocr_single_page(
                b"img", "image/png", page_index=2, timeout=5, failures=failures
            )

        assert failures == {2: "OTHER"}

    def test_failed_page_reason_names_the_cause(self) -> None:
        doc = pymupdf.open()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
        pix.clear_with(200)
        for _ in range(2):
            page = doc.new_page(width=400, height=500)
            page.insert_image(pymupdf.Rect(10, 10, 390, 490), pixmap=pix)
        pdf = doc.tobytes()
        doc.close()

        def call(*, text_message: str, **_: Any) -> LLMResponse:
            if "(page 2)" in text_message:
                return _withheld("RECITATION")
            return _ok("page one")

        with patch.object(llm_extract, "call_llm_with_images", MagicMock(side_effect=call)):
            result = llm_extract.ocr_pdf_document(pdf)

        assert result is not None
        assert result.text == "page one"
        assert result.incomplete == "page(s) [2] failed (2=RECITATION)"


# ---------------------------------------------------------------------------
# The OCR run's deadline bounds the tiles
# ---------------------------------------------------------------------------


class TestOcrDeadline:
    def test_no_call_after_the_deadline(self) -> None:
        calls = MagicMock(return_value=_ok("x"))
        failures: dict[int, str] = {}
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(
                _page_png(),
                "image/png",
                page_index=0,
                timeout=5,
                failures=failures,
                deadline=time.monotonic() - 1,
            )

        assert text is None
        assert calls.call_count == 0
        assert failures == {0: "out of time"}

    def test_tile_calls_get_only_the_time_left(self) -> None:
        calls = _answers(_withheld(), _ok("a"), _ok("b"))
        with patch.object(llm_extract, "call_llm_with_images", calls):
            llm_extract._ocr_single_page(
                _page_png(),
                "image/png",
                page_index=0,
                timeout=30,
                deadline=time.monotonic() + 3,
            )

        assert all(c.kwargs["timeout"] <= 3 for c in calls.call_args_list)


# ---------------------------------------------------------------------------
# Where the page is cut
# ---------------------------------------------------------------------------


class TestSplitImage:
    def test_several_lines_are_cut_between_lines(self) -> None:
        image = _page_image()
        split = llm_extract._split_image(_png(image))

        assert split is not None
        halves, joiner = split
        top, bottom = (_size(h) for h in halves)
        assert joiner == "\n"
        assert top[0] == bottom[0] == 600
        assert top[1] + bottom[1] == 800
        # The cut row is white: it runs between two lines, not through one.
        assert all(image.getpixel((x, top[1])) == 255 for x in range(600))

    def test_wide_region_of_several_lines_is_still_cut_top_bottom(self) -> None:
        split = llm_extract._split_image(_page_png(width=1200, height=200))

        assert split is not None
        halves, joiner = split
        assert joiner == "\n"
        assert [_size(h)[0] for h in halves] == [1200, 1200]

    def test_single_line_is_cut_at_a_space_between_words(self) -> None:
        split = llm_extract._split_image(_line_png())

        assert split is not None
        halves, joiner = split
        assert joiner == " "
        left, right = (_size(h) for h in halves)
        assert left[1] == right[1] == 30
        # Word blocks start every 50 px and are 40 px wide: the cut is in a gap.
        assert (left[0] - 10) % 50 >= 40

    def test_box_borders_do_not_make_two_lines_one(self) -> None:
        """A form's vertical box borders ink every row a little; two lines
        inside the box must still be cut top/bottom, not through both."""
        image = Image.new("L", (1275, 56), 255)
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 4, 1200, 4 + LINE_PX - 1), fill=0)
        draw.rectangle((40, 32, 1200, 32 + LINE_PX - 1), fill=0)
        for x in (5, 1268):  # 3 px borders down the whole region
            draw.rectangle((x, 0, x + 2, 55), fill=0)

        split = llm_extract._split_image(_png(image))

        assert split is not None
        halves, joiner = split
        assert joiner == "\n"
        assert [_size(h)[0] for h in halves] == [1275, 1275]

    def test_touching_lines_taller_than_one_line_are_not_cut_left_right(self) -> None:
        solid = Image.new("L", (600, 120), 0)  # no blank row anywhere

        split = llm_extract._split_image(_png(solid))

        assert split is not None
        assert split[1] == "\n"

    def test_text_line_count(self) -> None:
        assert llm_extract._text_line_count(_page_image(600, 100).point(lambda v: v)) == 3
        assert llm_extract._text_line_count(Image.new("L", (50, 50), 255)) == 0

    def test_small_or_undecodable_image_is_not_cut(self) -> None:
        assert llm_extract._split_image(_png(Image.new("L", (15, 15), 0))) is None
        assert llm_extract._split_image(b"not an image") is None
