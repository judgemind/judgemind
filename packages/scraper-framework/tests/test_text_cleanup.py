"""Tests for ingestion.text_cleanup module."""

from __future__ import annotations

from ingestion.text_cleanup import (
    clean_ruling_text,
    collapse_whitespace,
    detect_paragraphs,
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

    def test_removes_multiline_dept_law_motion_block(self) -> None:
        """Multi-line block starting with 'DEPARTMENT N LAW AND MOTION RULINGS' is removed."""
        text = (
            "DEPARTMENT 51 LAW AND MOTION RULINGS\n"
            "1. If you wish to submit on the tentative ruling, please email\n"
            "the clerk at SMCdept51@lacourt.org.\n"
            "2. If you intend to appear, please notify by phone.\n"
            "3. Counsel should be prepared to discuss the issues.\n"
            "\n"
            "Case No. 24STCV12345\n"
            "The motion is granted."
        )
        result = strip_boilerplate(text)
        assert "DEPARTMENT 51 LAW AND MOTION" not in result
        assert "wish to submit" not in result
        assert "SMCdept51" not in result
        assert "prepared to discuss" not in result
        # The blank line and subsequent content are preserved
        assert "Case No. 24STCV12345" in result
        assert "The motion is granted." in result

    def test_removes_if_you_wish_block(self) -> None:
        """Block starting with 'If you wish to submit on the tentative...' is removed."""
        text = (
            "If you wish to submit on the tentative ruling, you must email\n"
            "the court clerk no later than 4pm the day before the hearing.\n"
            "Failure to do so will result in the matter being taken off calendar.\n"
            "\n"
            "The motion for summary judgment is denied."
        )
        result = strip_boilerplate(text)
        assert "wish to submit" not in result
        assert "email" not in result
        assert "taken off calendar" not in result
        assert "The motion for summary judgment is denied." in result

    def test_removes_if_you_intend_block(self) -> None:
        """Block starting with 'If you intend to submit on this tentative...' is removed."""
        text = (
            "If you intend to submit on this tentative ruling, please email\n"
            "the court at dept51@lacourt.org by 4:00 PM.\n"
            "\n"
            "Ruling on motion to compel."
        )
        result = strip_boilerplate(text)
        assert "intend to submit" not in result
        assert "Ruling on motion to compel." in result

    def test_multiline_block_preserves_content_after_blank_line(self) -> None:
        """Content after the blank line that ends a block is preserved."""
        text = (
            "DEPARTMENT 7 LAW AND MOTION RULINGS\n"
            "Instructions for parties.\n"
            "\n"
            "Case No. BC123456\n"
            "TENTATIVE RULING\n"
            "The motion is granted."
        )
        result = strip_boilerplate(text)
        assert "Case No. BC123456" in result
        assert "TENTATIVE RULING" in result
        assert "The motion is granted." in result


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
# detect_paragraphs
# ---------------------------------------------------------------------------


class TestDetectParagraphs:
    """Tests for paragraph boundary detection in single-newline text."""

    def test_preserves_existing_double_newlines(self) -> None:
        """Text that already has paragraph breaks should be unchanged."""
        text = "First paragraph.\n\nSecond paragraph."
        assert detect_paragraphs(text) == text

    def test_section_header_all_caps(self) -> None:
        """ALL CAPS section headers should get a paragraph break before them."""
        text = (
            "Some introductory text about the case.\n"
            "BACKGROUND\n"
            "The plaintiff filed a complaint on January 1."
        )
        result = detect_paragraphs(text)
        assert "\n\nBACKGROUND\n" in result

    def test_multiple_section_headers(self) -> None:
        """Multiple section headers should each get paragraph breaks."""
        text = (
            "Intro text here.\n"
            "DISCUSSION\n"
            "The court considers the following.\n"
            "RULING\n"
            "The motion is granted."
        )
        result = detect_paragraphs(text)
        assert "\n\nDISCUSSION\n" in result
        assert "\n\nRULING\n" in result

    def test_section_header_multiword(self) -> None:
        """Multi-word ALL CAPS headers like LEGAL STANDARD should be detected."""
        text = "Some text.\nLEGAL STANDARD\nThe standard for summary judgment is..."
        result = detect_paragraphs(text)
        assert "\n\nLEGAL STANDARD\n" in result

    def test_indentation_change(self) -> None:
        """A line indented with 4+ spaces after a non-indented line is a new paragraph."""
        text = "The court rules as follows.\n    The motion for summary judgment is granted."
        result = detect_paragraphs(text)
        assert "\n\n    The motion" in result

    def test_tab_indentation(self) -> None:
        """A tab-indented line after a non-indented line is a new paragraph."""
        text = "The court rules as follows.\n\tThe motion for summary judgment is granted."
        result = detect_paragraphs(text)
        assert "\n\n\tThe motion" in result

    def test_short_line_followed_by_capital(self) -> None:
        """A short line followed by a line starting with a capital letter is a paragraph break."""
        text = (
            "The motion is granted.\nThe court further orders that the defendant shall pay costs."
        )
        # The first line is short relative to the second, and second starts with capital
        result = detect_paragraphs(text)
        # This should insert a paragraph break
        assert "\n\n" in result

    def test_no_false_split_mid_sentence(self) -> None:
        """Lines that are continuations (not starting with capital) should not be split."""
        text = (
            "The court has reviewed the plaintiff's motion for summary\n"
            "judgment and finds that there are no triable issues of\n"
            "material fact."
        )
        result = detect_paragraphs(text)
        # Should NOT insert paragraph breaks in the middle of sentences
        assert result.count("\n\n") == 0

    def test_empty_string(self) -> None:
        assert detect_paragraphs("") == ""

    def test_single_line(self) -> None:
        text = "The motion is granted."
        assert detect_paragraphs(text) == text

    def test_separator_underscores_become_paragraph_break(self) -> None:
        """Lines of underscores should be replaced with paragraph breaks."""
        text = "First section content.\n__________________________________\nSecond section content."
        result = detect_paragraphs(text)
        assert "__" not in result
        assert "\n\n" in result
        assert "First section content." in result
        assert "Second section content." in result

    def test_separator_dashes_become_paragraph_break(self) -> None:
        """Lines of dashes should be replaced with paragraph breaks."""
        text = "Section A.\n-----------------------------------\nSection B."
        result = detect_paragraphs(text)
        assert "---" not in result
        assert "\n\n" in result

    def test_separator_equals_become_paragraph_break(self) -> None:
        """Lines of equals signs should be replaced with paragraph breaks."""
        text = "Above.\n===================================\nBelow."
        result = detect_paragraphs(text)
        assert "===" not in result
        assert "\n\n" in result

    def test_separator_asterisks_become_paragraph_break(self) -> None:
        """Lines of asterisks should be replaced with paragraph breaks."""
        text = "Part 1.\n***\nPart 2."
        result = detect_paragraphs(text)
        assert "***" not in result
        assert "\n\n" in result

    def test_separator_with_whitespace(self) -> None:
        """Separator lines with leading/trailing whitespace are still detected."""
        text = "Content.\n   _______________   \nMore content."
        result = detect_paragraphs(text)
        assert "___" not in result
        assert "\n\n" in result

    def test_short_dashes_not_treated_as_separator(self) -> None:
        """Very short dash sequences (< 3 chars) should not be treated as separators."""
        text = "Content.\n--\nMore content."
        result = detect_paragraphs(text)
        # "--" is only 2 chars, should not be treated as separator
        assert "--" in result

    def test_realistic_wall_of_text(self) -> None:
        """Simulate a real PDF-extracted ruling that lost paragraph breaks."""
        text = (
            "The motion for summary judgment filed by defendant is considered.\n"
            "BACKGROUND\n"
            "Plaintiff filed this action on January 1, 2024 alleging\n"
            "causes of action for negligence and breach of contract.\n"
            "DISCUSSION\n"
            "The court applies the standard set forth in Code of Civil\n"
            "Procedure section 437c.\n"
            "RULING\n"
            "The motion is DENIED."
        )
        result = detect_paragraphs(text)
        # Each section header should produce a paragraph break
        paragraphs = result.split("\n\n")
        assert len(paragraphs) >= 4  # intro, BACKGROUND+content, DISCUSSION+content, RULING+content


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

    def test_pipeline_strips_multiline_boilerplate_block(self) -> None:
        """Multi-line boilerplate blocks are stripped in the full pipeline."""
        raw = (
            "DEPARTMENT 51 LAW AND MOTION RULINGS\n"
            "1. If you wish to submit on the tentative ruling, email the clerk.\n"
            "2. If you intend to appear, notify the court.\n"
            "\n"
            "Case No. 24STCV12345\n"
            "\n"
            "The motion for summary judgment is granted."
        )
        result = clean_ruling_text(raw)
        assert result is not None
        assert "LAW AND MOTION" not in result
        assert "wish to submit" not in result
        assert "The motion for summary judgment is granted." in result

    def test_pipeline_handles_separator_lines(self) -> None:
        """Separator lines become paragraph breaks in the full pipeline."""
        raw = (
            "Section 1 content here.\n___________________________________\nSection 2 content here."
        )
        result = clean_ruling_text(raw)
        assert result is not None
        assert "___" not in result
        assert "Section 1 content here." in result
        assert "Section 2 content here." in result
        # Should have paragraph break between sections
        assert "\n\n" in result

    def test_pipeline_detects_paragraphs_in_wall_of_text(self) -> None:
        """Verify detect_paragraphs is wired into the cleanup pipeline."""
        raw = (
            "The court considers the motion.\n"
            "BACKGROUND\n"
            "Plaintiff filed suit in 2024.\n"
            "RULING\n"
            "The motion is denied."
        )
        result = clean_ruling_text(raw)
        assert result is not None
        # Section headers should have produced paragraph breaks
        assert "\n\n" in result
        assert "BACKGROUND" in result
        assert "RULING" in result
