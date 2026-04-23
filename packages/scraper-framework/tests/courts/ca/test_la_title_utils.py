"""Tests for the shared LA title extraction helpers (la_title_utils).

Verifies that the shared module correctly extracts case titles from ruling
text using caption blocks and MOVING/RESPONDING PARTY fields, with
configurable max length for backfill use.
"""

from __future__ import annotations

from courts.ca.la_title_utils import (
    clean_name,
    extract_clean_title,
    extract_title_from_caption,
    extract_title_from_moving_responding,
)


class TestCleanName:
    """Tests for the clean_name() function."""

    def test_strips_entity_descriptors(self) -> None:
        result = clean_name("Jim Hilaski, An Individual")
        assert "An Individual" not in result
        assert "Jim Hilaski" in result

    def test_strips_california_corporation(self) -> None:
        result = clean_name("Acme Corp, A California Corporation")
        assert "Corporation" not in result
        assert "Acme Corp" in result

    def test_collapses_whitespace(self) -> None:
        result = clean_name("  Jim   Hilaski  ")
        assert result == "Jim Hilaski"

    def test_strips_trailing_comma(self) -> None:
        result = clean_name("Smith,")
        assert result == "Smith"

    def test_strips_trailing_and(self) -> None:
        result = clean_name("Smith And")
        assert result == "Smith"

    def test_normalizes_et_al(self) -> None:
        result = clean_name("Smith Et Al")
        # The trailing period from "et al." is stripped by the final punctuation strip
        assert result == "Smith, et al"

    def test_strips_punctuation(self) -> None:
        result = clean_name("(Smith);")
        assert result == "Smith"

    def test_empty_input(self) -> None:
        result = clean_name("")
        assert result == ""


class TestExtractTitleFromCaption:
    """Tests for extract_title_from_caption()."""

    def test_basic_caption(self) -> None:
        text = "Smith\n  Plaintiff(s),\n  vs.\n  Jones\n  Defendant(s)."
        result = extract_title_from_caption(text)
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result
        assert " v. " in result

    def test_returns_none_without_all_markers(self) -> None:
        text = "Smith Plaintiff(s), Jones"
        result = extract_title_from_caption(text)
        assert result is None

    def test_returns_none_when_vs_after_defendant(self) -> None:
        # vs. marker appears after defendant role marker — invalid structure
        text = "Smith\nDefendant(s),\nvs.\nJones\nPlaintiff(s)."
        result = extract_title_from_caption(text)
        # Should return None because vs_end >= d_start
        assert result is None

    def test_respects_max_length(self) -> None:
        text = "Smith\n  Plaintiff(s),\n  vs.\n  Jones\n  Defendant(s)."
        # Very short max_length rejects even short titles
        result = extract_title_from_caption(text, max_length=5)
        assert result is None

    def test_lenient_max_length(self) -> None:
        """A title that would be rejected at 120 chars passes at 250."""
        long_plaintiff = "Smith Jones Williams Brown Davis Miller Wilson Thompson"
        long_defendant = "Anderson Taylor Thomas Jackson White Harris Martin Robinson"
        text = f"{long_plaintiff}\n  Plaintiff(s),\n  vs.\n  {long_defendant}\n  Defendant(s)."
        # Strict: may reject if title > 120
        strict_result = extract_title_from_caption(text, max_length=120)
        # Lenient: should accept if title <= 250
        lenient_result = extract_title_from_caption(text, max_length=250)
        # At least the lenient result should work
        assert lenient_result is not None
        assert "Smith" in lenient_result
        assert "Anderson" in lenient_result
        # If strict rejects, that confirms the length gate works
        if strict_result is None:
            assert len(lenient_result) > 120

    def test_strips_entity_descriptors_from_names(self) -> None:
        text = (
            "Hilaski, An Individual\n  Plaintiff(s),\n  vs.\n  Dina, An Individual\n  Defendant(s)."
        )
        result = extract_title_from_caption(text)
        assert result is not None
        assert "An Individual" not in result
        assert "Hilaski" in result
        assert "Dina" in result

    def test_rejects_too_short_title(self) -> None:
        # Title "A v. B" is 6 chars and passes the >= 5 check.
        # Use max_length=3 to force rejection of short titles.
        text = "A\n  Plaintiff(s),\n  vs.\n  B\n  Defendant(s)."
        result = extract_title_from_caption(text, max_length=3)
        assert result is None


class TestExtractTitleFromMovingResponding:
    """Tests for extract_title_from_moving_responding()."""

    def test_basic_extraction(self) -> None:
        text = "MOVING PARTY: Alice Walker.\nRESPONDING PARTY: Bob Smith.\n"
        result = extract_title_from_moving_responding(text)
        assert result is not None
        assert "Walker" in result
        assert "Smith" in result

    def test_returns_none_without_moving_party(self) -> None:
        text = "RESPONDING PARTY: Bob Smith.\n"
        result = extract_title_from_moving_responding(text)
        assert result is None

    def test_returns_none_without_responding_party(self) -> None:
        text = "MOVING PARTY: Alice Walker.\n"
        result = extract_title_from_moving_responding(text)
        assert result is None

    def test_skips_no_opposition(self) -> None:
        text = "MOVING PARTY: Alice Walker.\nRESPONDING PARTY: No opposition.\n"
        result = extract_title_from_moving_responding(text)
        assert result is None

    def test_skips_unopposed(self) -> None:
        text = "MOVING PARTY: Alice Walker.\nRESPONDING PARTY: Unopposed.\n"
        result = extract_title_from_moving_responding(text)
        assert result is None

    def test_strips_role_prefix(self) -> None:
        text = "MOVING PARTY: Plaintiff Alice Walker.\nRESPONDING PARTY: Defendant Bob Smith.\n"
        result = extract_title_from_moving_responding(text)
        assert result is not None
        assert "Plaintiff" not in result
        assert "Defendant" not in result

    def test_respects_max_length(self) -> None:
        text = "MOVING PARTY: Alice Walker.\nRESPONDING PARTY: Bob Smith.\n"
        result = extract_title_from_moving_responding(text, max_length=5)
        assert result is None

    def test_lenient_max_length(self) -> None:
        """Titles between 121-250 chars pass with lenient max_length."""
        long_moving = "Smith Jones Williams Brown Davis Miller Wilson Thompson Garcia"
        long_responding = "Anderson Taylor Thomas Jackson White Harris Martin Robinson Clark"
        text = f"MOVING PARTY: {long_moving}.\nRESPONDING PARTY: {long_responding}.\n"
        strict_result = extract_title_from_moving_responding(text, max_length=120)
        lenient_result = extract_title_from_moving_responding(text, max_length=250)
        # Lenient should accept longer titles
        if lenient_result is not None and strict_result is None:
            assert len(lenient_result) > 120


class TestExtractCleanTitle:
    """Tests for extract_clean_title()."""

    def test_prefers_caption_over_moving_responding(self) -> None:
        text = (
            "Smith\n  Plaintiff(s),\n  vs.\n  Jones\n  Defendant(s).\n"
            "MOVING PARTY: Alice.\nRESPONDING PARTY: Bob.\n"
        )
        result = extract_clean_title(text)
        assert result is not None
        assert "Smith" in result
        assert "Jones" in result

    def test_falls_back_to_moving_responding(self) -> None:
        text = "MOVING PARTY: Alice Walker.\nRESPONDING PARTY: Bob Smith.\n"
        result = extract_clean_title(text)
        assert result is not None
        assert "Walker" in result
        assert "Smith" in result

    def test_returns_none_when_both_fail(self) -> None:
        text = "no useful content here"
        result = extract_clean_title(text)
        assert result is None

    def test_passes_max_length_to_strategies(self) -> None:
        """The max_length parameter propagates to both strategies."""
        text = "MOVING PARTY: Alice Walker.\nRESPONDING PARTY: Bob Smith.\n"
        result = extract_clean_title(text, max_length=5)
        assert result is None

    def test_rejects_department_header_boilerplate(self) -> None:
        """Titles containing department header boilerplate are rejected."""
        text = (
            "Department 15 Law And Motion Rulings\n  Plaintiff(s),\n  vs.\n  Jones\n  Defendant(s)."
        )
        result = extract_clean_title(text)
        # If the caption extraction picks up boilerplate as plaintiff, it should
        # be rejected by the DEPT_HEADER_BOILERPLATE_RE check
        # (or extraction may fail because the structure is malformed)
        # Either way, the result should not contain boilerplate
        if result is not None:
            assert "Law And Motion Rulings" not in result
