"""Regression tests for #5098: an OCR tile cut must never go through ink.

The #5046 tile fallback cuts a region the model will not finish at the
emptiest pixel line near its middle.  Nothing checked that the line was
blank: a large-font heading, a grey-shaded form box, or tightly spaced lines
were cut through their glyphs.  The model reads half-glyphs as nothing
(``""`` with ``STOP``), the tile path took that for a blank region, and the
page was reported complete with its text missing: the silent loss #4995 and
#5000 set out to eliminate.

Now a cut is made only at a blank line: near the middle when there is one,
else anywhere that leaves both halves at least ``_OCR_TILE_MIN_PX``.  A
region with no blank line is unsplittable, and an unsplittable region the
model still answers with nothing leaves the page unread (incomplete).
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pymupdf
from PIL import Image, ImageDraw

from ingestion import llm_extract
from ingestion.llm_providers import LLMResponse


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as image:
        return image.size


def _ok(text: str) -> LLMResponse:
    return LLMResponse(text=text, input_tokens=10, output_tokens=10, finish_reason="STOP")


def _withheld(reason: str = "RECITATION") -> LLMResponse:
    return LLMResponse(text="", input_tokens=10, output_tokens=0, finish_reason=reason)


def _row_is_blank(image: Image.Image, y: int) -> bool:
    return all(image.getpixel((x, y)) == 255 for x in range(image.size[0]))


def _column_is_blank(image: Image.Image, x: int) -> bool:
    return all(image.getpixel((x, y)) == 255 for y in range(image.size[1]))


def _band_image(
    width: int = 600, height: int = 200, top: int = 30, bottom: int = 170
) -> Image.Image:
    """White, with one solid band of ink over rows ``top``..``bottom`` that
    covers the whole middle cut window (rows 50-150 of 200)."""
    image = Image.new("L", (width, height), 255)
    ImageDraw.Draw(image).rectangle((0, top, width - 1, bottom - 1), fill=0)
    return image


class TestCutNeverThroughInk:
    def test_band_over_the_middle_is_cut_at_a_blank_row_outside_it(self) -> None:
        image = _band_image()

        split = llm_extract._split_image(_png(image))

        assert split is not None
        halves, joiner = split
        assert joiner == "\n"
        top, bottom = (_size(h) for h in halves)
        assert top[1] + bottom[1] == 200
        cut = top[1]
        assert not 30 <= cut < 170, f"cut at row {cut} runs through the band"
        assert _row_is_blank(image, cut)
        assert min(top[1], bottom[1]) >= llm_extract._OCR_TILE_MIN_PX

    def test_grey_shaded_box_with_no_blank_line_is_unsplittable(self) -> None:
        """A form box shaded darker than the ink level reads as ink in every
        row and column: there is nowhere to cut."""
        shaded = Image.new("L", (600, 200), 120)

        assert llm_extract._split_image(_png(shaded)) is None

    def test_tightly_spaced_lines_without_a_blank_row_are_unsplittable(self) -> None:
        solid = Image.new("L", (600, 120), 0)

        assert llm_extract._split_image(_png(solid)) is None

    def test_single_line_without_a_space_is_unsplittable(self) -> None:
        line = Image.new("L", (600, 30), 255)
        ImageDraw.Draw(line).rectangle((0, 5, 599, 24), fill=0)

        assert llm_extract._split_image(_png(line)) is None

    def test_tall_single_line_is_cut_between_words(self) -> None:
        """A large-font heading taller than ``_OCR_SINGLE_LINE_MAX_PX`` with
        no blank row is still one line: cut it at a space between words."""
        heading = Image.new("L", (600, 100), 255)
        draw = ImageDraw.Draw(heading)
        for x in range(0, 600, 150):
            draw.rectangle((x, 0, x + 119, 99), fill=0)  # glyph blocks fill every row

        split = llm_extract._split_image(_png(heading))

        assert split is not None
        halves, joiner = split
        assert joiner == " "
        left, right = (_size(h) for h in halves)
        assert left[1] == right[1] == 100
        assert _column_is_blank(heading, left[0])

    def test_single_line_cut_column_is_blank(self) -> None:
        line = Image.new("L", (600, 30), 255)
        draw = ImageDraw.Draw(line)
        # One long word over the middle window, a space only near the left.
        draw.rectangle((0, 5, 99, 24), fill=0)
        draw.rectangle((110, 5, 599, 24), fill=0)

        split = llm_extract._split_image(_png(line))

        assert split is not None
        halves, joiner = split
        assert joiner == " "
        cut = _size(halves[0])[0]
        assert 100 <= cut < 110
        assert _column_is_blank(line, cut)

    def test_blank_row_near_the_middle_still_wins(self) -> None:
        """The widened search only applies when the middle window has no
        blank line: a page of ordinary lines is cut as before."""
        image = _band_image(top=30, bottom=95)
        ImageDraw.Draw(image).rectangle((0, 105, 599, 169), fill=0)

        split = llm_extract._split_image(_png(image))

        assert split is not None
        cut = _size(split[0][0])[1]
        assert 95 <= cut < 105


def _letter_page(width: int = 1275, height: int = 1650) -> Image.Image:
    """A letter page at 150 DPI: 24 px text lines, 40 px apart."""
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for y in range(20, height - 40, 40):
        draw.rectangle((60, y, width - 60, y + 23), fill=0)
    return image


def _assert_cut_between_lines(image: Image.Image) -> None:
    split = llm_extract._split_image(_png(image))
    assert split is not None
    halves, joiner = split
    assert joiner == "\n", "a page of many lines must be cut top/bottom"
    cut = _size(halves[0])[1]
    # Between text lines: rows 20+40k .. 20+40k+23 are text.
    assert (cut - 20) % 40 >= 24, f"cut at row {cut} runs through a text line"


class TestRulesAreNotText:
    """Table grids, box borders and a scanner's dark edge ink every row,
    but cutting across them cuts no glyph."""

    def test_table_rules_do_not_make_a_page_one_line(self) -> None:
        image = _letter_page()
        draw = ImageDraw.Draw(image)
        for x in range(100, 1275, 100):  # 12 full-height 4 px column rules
            draw.rectangle((x, 0, x + 3, 1649), fill=0)

        _assert_cut_between_lines(image)

    def test_dark_scan_edge_does_not_make_a_page_one_line(self) -> None:
        image = _letter_page()
        ImageDraw.Draw(image).rectangle((0, 0, 39, 1649), fill=0)

        _assert_cut_between_lines(image)

    def test_page_with_no_blank_row_is_never_cut_left_right(self) -> None:
        """A page-sized region read as one line is not a heading."""
        image = Image.new("L", (1275, 1650), 255)
        draw = ImageDraw.Draw(image)
        for x in range(0, 1275, 150):
            draw.rectangle((x, 0, x + 119, 1649), fill=0)

        assert llm_extract._split_image(_png(image)) is None

    def test_wide_shaded_column_is_not_a_rule(self) -> None:
        """A full-height shaded box wider than a rule may hold text: rows
        through it are not blank."""
        image = _letter_page(width=600, height=200)
        ImageDraw.Draw(image).rectangle((200, 0, 399, 199), fill=120)

        assert llm_extract._split_image(_png(image)) is None

    def test_underline_does_not_block_a_cut_between_words(self) -> None:
        line = Image.new("L", (600, 30), 255)
        draw = ImageDraw.Draw(line)
        for x in range(10, 560, 50):
            draw.rectangle((x, 3, x + 39, 22), fill=0)
        draw.rectangle((0, 26, 599, 27), fill=0)  # a full-width underline

        split = llm_extract._split_image(_png(line))

        assert split is not None
        halves, joiner = split
        assert joiner == " "
        cut = _size(halves[0])[0]
        assert (cut - 10) % 50 >= 40, f"cut at column {cut} runs through a word"


class TestUnsplittableEmptyAnswerIsIncomplete:
    def test_unsplittable_region_answered_empty_leaves_the_page_unread(self) -> None:
        """Withheld as a recitation, no blank line to cut at, and the
        bar-separated last resort answers nothing: the page is not blank."""
        shaded = _png(Image.new("L", (600, 200), 120))
        calls = MagicMock(side_effect=[_withheld("RECITATION"), _ok("")])
        failures: dict[int, str] = {}
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(
                shaded, "image/png", page_index=3, timeout=5, failures=failures
            )

        assert text is None
        assert failures == {3: "RECITATION"}
        assert calls.call_count == 2
        assert calls.call_args_list[1].kwargs["system_prompt"] == (
            llm_extract._OCR_WORDS_SYSTEM_PROMPT
        )

    def test_unsplittable_max_tokens_region_leaves_the_page_unread(self) -> None:
        shaded = _png(Image.new("L", (600, 200), 120))
        calls = MagicMock(return_value=_withheld("MAX_TOKENS"))
        failures: dict[int, str] = {}
        with patch.object(llm_extract, "call_llm_with_images", calls):
            text = llm_extract._ocr_single_page(
                shaded, "image/png", page_index=0, timeout=5, failures=failures
            )

        assert text is None
        assert calls.call_count == 1
        assert failures == {0: "MAX_TOKENS"}

    def test_page_with_no_blank_line_is_reported_incomplete(self) -> None:
        """End to end: the scanned page is a grey-shaded box.  Before #5098
        its halves came back ``""`` and the page passed for blank."""
        doc = pymupdf.open()
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 8, 8), False)
        pix.clear_with(120)
        page = doc.new_page(width=400, height=500)
        page.insert_image(page.rect, pixmap=pix)
        pdf = doc.tobytes()
        doc.close()

        def call(**kwargs: object) -> LLMResponse:
            if kwargs["system_prompt"] == llm_extract._OCR_WORDS_SYSTEM_PROMPT:
                return _ok("")
            if "part of a court document page" in str(kwargs["text_message"]):
                return _ok("")  # half-glyphs read as nothing
            return _withheld("RECITATION")

        with patch.object(llm_extract, "call_llm_with_images", MagicMock(side_effect=call)):
            result = llm_extract.ocr_pdf_document(pdf)

        assert result is not None
        assert result.text == ""
        assert result.incomplete == "page(s) [1] failed (1=RECITATION)"
