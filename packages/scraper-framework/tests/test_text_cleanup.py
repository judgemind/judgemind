"""Tests for ingestion.text_cleanup module."""

from __future__ import annotations

from ingestion.text_cleanup import (
    clean_ruling_text,
    collapse_whitespace,
    fix_encoding,
    strip_boilerplate,
    strip_page_numbers,
)

# ---------------------------------------------------------------------------
# fix_encoding
# ---------------------------------------------------------------------------


class TestFixEncoding:
    """Tests for mojibake / encoding error correction."""

    def test_fixes_smart_double_quotes(self) -> None:
        text = "\u00e2\u0080\u009cHello\u00e2\u0080\u009d"
        assert fix_encoding(text) == "\u201cHello\u201d"

    def test_fixes_smart_single_quotes(self) -> None:
        text = "\u00e2\u0080\u0098don\u00e2\u0080\u0099t"
        assert fix_encoding(text) == "\u2018don\u2019t"

    def test_fixes_en_dash(self) -> None:
        text = "pages 1\u00e2\u0080\u009310"
        assert fix_encoding(text) == "pages 1\u201310"

    def test_fixes_em_dash(self) -> None:
        text = "ruling\u00e2\u0080\u0094denied"
        assert fix_encoding(text) == "ruling\u2014denied"

    def test_fixes_inverted_question_mark(self) -> None:
        text = "plaintiff\u00bfs motion"
        assert fix_encoding(text) == "plaintiff's motion"

    def test_fixes_double_encoded_section_sign(self) -> None:
        text = "\u00c2\u00a7 1234"
        assert fix_encoding(text) == "\u00a7 1234"

    def test_normalizes_non_breaking_space(self) -> None:
        text = "hello\u00a0world"
        assert fix_encoding(text) == "hello world"

    def test_preserves_clean_text(self) -> None:
        text = "The court grants the motion for summary judgment."
        assert fix_encoding(text) == text

    def test_empty_string(self) -> None:
        assert fix_encoding("") == ""


# ---------------------------------------------------------------------------
# strip_page_numbers
# ---------------------------------------------------------------------------


class TestStripPageNumbers:
    """Tests for page number artifact removal."""

    def test_removes_page_x_of_y(self) -> None:
        text = "Some text\nPage 2 of 5\nMore text"
        assert strip_page_numbers(text) == "Some text\nMore text"

    def test_removes_page_x_of_y_case_insensitive(self) -> None:
        text = "Text\nPAGE 1 OF 3\nMore"
        assert strip_page_numbers(text) == "Text\nMore"

    def test_removes_dash_number_dash(self) -> None:
        text = "Content\n- 3 -\nMore content"
        assert strip_page_numbers(text) == "Content\nMore content"

    def test_removes_double_dash_number(self) -> None:
        text = "Content\n-- 7 --\nMore"
        assert strip_page_numbers(text) == "Content\nMore"

    def test_removes_standalone_small_number(self) -> None:
        text = "Line one\n42\nLine two"
        assert strip_page_numbers(text) == "Line one\nLine two"

    def test_preserves_numbers_in_text(self) -> None:
        text = "The court awarded 42 days of continuance."
        assert strip_page_numbers(text) == text

    def test_preserves_large_standalone_numbers(self) -> None:
        # Numbers 1000+ are unlikely to be page numbers
        text = "Line one\n1234\nLine two"
        assert strip_page_numbers(text) == text

    def test_removes_multiple_page_numbers(self) -> None:
        text = "Intro\nPage 1 of 3\nBody\n- 2 -\nMore\nPage 3 of 3\nEnd"
        assert strip_page_numbers(text) == "Intro\nBody\nMore\nEnd"


# ---------------------------------------------------------------------------
# strip_boilerplate
# ---------------------------------------------------------------------------


class TestStripBoilerplate:
    """Tests for boilerplate header/instruction removal."""

    def test_removes_superior_court_header(self) -> None:
        text = "SUPERIOR COURT OF CALIFORNIA\nThe motion is granted."
        assert strip_boilerplate(text) == "The motion is granted."

    def test_removes_superior_court_state_variant(self) -> None:
        text = "SUPERIOR COURT OF THE STATE OF CALIFORNIA\nRuling text."
        assert strip_boilerplate(text) == "Ruling text."

    def test_removes_county_header(self) -> None:
        text = "COUNTY OF LOS ANGELES\nThe motion is denied."
        assert strip_boilerplate(text) == "The motion is denied."

    def test_removes_department_header(self) -> None:
        text = "DEPARTMENT 1\nThe court rules as follows."
        assert strip_boilerplate(text) == "The court rules as follows."

    def test_removes_dept_abbreviation(self) -> None:
        text = "DEPT. S22\nRuling follows."
        assert strip_boilerplate(text) == "Ruling follows."

    def test_removes_submission_instructions(self) -> None:
        text = "Parties who intend to submit on this ruling should notify.\nThe motion is granted."
        assert strip_boilerplate(text) == "The motion is granted."

    def test_preserves_substantive_content(self) -> None:
        text = "The court grants the motion for summary judgment."
        assert strip_boilerplate(text) == text


# ---------------------------------------------------------------------------
# collapse_whitespace
# ---------------------------------------------------------------------------


class TestCollapseWhitespace:
    """Tests for whitespace normalization."""

    def test_strips_trailing_whitespace(self) -> None:
        text = "Hello   \nWorld  "
        assert collapse_whitespace(text) == "Hello\nWorld"

    def test_collapses_multiple_blank_lines(self) -> None:
        text = "Para 1\n\n\n\n\nPara 2"
        result = collapse_whitespace(text)
        assert result == "Para 1\n\n\nPara 2"

    def test_preserves_double_blank_line(self) -> None:
        text = "Para 1\n\n\nPara 2"
        assert collapse_whitespace(text) == "Para 1\n\n\nPara 2"

    def test_preserves_single_blank_line(self) -> None:
        text = "Line 1\n\nLine 2"
        assert collapse_whitespace(text) == "Line 1\n\nLine 2"

    def test_strips_leading_trailing_blank_lines(self) -> None:
        text = "\n\nContent\n\n"
        assert collapse_whitespace(text) == "Content"

    def test_empty_string(self) -> None:
        assert collapse_whitespace("") == ""


# ---------------------------------------------------------------------------
# clean_ruling_text (integration)
# ---------------------------------------------------------------------------


class TestCleanRulingText:
    """Integration tests for the full cleanup pipeline."""

    def test_none_input(self) -> None:
        assert clean_ruling_text(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert clean_ruling_text("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert clean_ruling_text("   \n\n   ") is None

    def test_full_cleanup_pipeline(self) -> None:
        """Simulate a realistic ruling text with multiple issues."""
        raw = (
            "SUPERIOR COURT OF CALIFORNIA\n"
            "COUNTY OF LOS ANGELES\n"
            "DEPARTMENT 1\n"
            "\n"
            "\n"
            "\n"
            "\n"
            "The court has reviewed plaintiff\u00bfs motion for summary judgment.\n"
            "\n"
            "Page 1 of 2\n"
            "\n"
            "The motion is GRANTED. The court finds that there are no\n"
            "triable issues of material fact.\n"
            "\n"
            "- 2 -\n"
            "\n"
            "Parties who intend to submit on this ruling should notify.\n"
        )
        result = clean_ruling_text(raw)
        assert result is not None
        # Encoding fixed
        assert "\u00bf" not in result
        assert "plaintiff's motion" in result
        # Page numbers removed
        assert "Page 1 of 2" not in result
        assert "- 2 -" not in result
        # Boilerplate removed
        assert "SUPERIOR COURT" not in result
        assert "COUNTY OF" not in result
        assert "DEPARTMENT 1" not in result
        assert "intend to submit" not in result
        # Substantive content preserved
        assert "motion is GRANTED" in result
        assert "triable issues" in result
        # Excessive blank lines collapsed
        assert "\n\n\n\n" not in result

    def test_preserves_clean_ruling(self) -> None:
        """Clean text should pass through with minimal changes."""
        clean = (
            "The motion for summary judgment is denied.\n\nThe court finds triable issues exist."
        )
        result = clean_ruling_text(clean)
        assert result == clean
