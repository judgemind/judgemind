"""Tests for the Santa Clara concatenated-title guard (#2562).

Santa Clara multi-case PDFs sometimes produce merged case_title strings that
concatenate adjacent calendar lines — e.g.

    "Liangbei Wang v. NetEase, Inc., et al. Manuel Panilag v. Armando "
    "Contreras et al. Jin Yin, et al vs Xiaoxiao Lu, et al."

contains three distinct calendar entries fused into a single ``case_title``.
The existing deterministic rule ``check_no_multiple_adversarial_patterns``
flags these but still writes them to the DB (``flag`` -> logged + written).
#2562 asks for a post-processing step that truncates the title to the first
``A v. B`` caption so the DB stores one clean title per ruling, AND for a
deterministic rule that can reject or flag any remaining concatenated titles
with enough context to let the worker skip the write.

This module exercises two new pieces of behaviour:

1. ``_truncate_concatenated_case_titles`` — a post-processor (runs alongside
   ``_deduplicate_ruling_texts``) that rewrites a multi-``v.`` title in
   place to keep only the first caption.  Applied in the main extractor
   path and both cache-hit filter helpers.

2. ``_truncate_concatenated_title`` (single-string helper, used by the
   post-processor) — deterministic transformation of one title string.

The fixture in this module is intentionally hand-crafted to reproduce the
contamination shape observed in dev DB rows (#2562 issue body).  It is
"reproducible" in the AC1 sense because it targets the post-processing
layer: the same inputs that the live LLM has produced in production
exercise the new code path here without requiring a multi-second
LLM round-trip.
"""

from __future__ import annotations

import pytest

from framework.llm_extractor import (
    _deduplicate_ruling_texts,
    _truncate_concatenated_case_titles,
    _truncate_concatenated_title,
)
from framework.llm_schema import ExtractedRuling

# ---------------------------------------------------------------------------
# Single-string truncation helper
# ---------------------------------------------------------------------------


class TestTruncateConcatenatedTitle:
    """Tests for the single-string truncation helper."""

    def test_clean_v_title_unchanged(self) -> None:
        """A clean 'A v. B' title is preserved verbatim."""
        assert _truncate_concatenated_title("Smith v. Jones") == "Smith v. Jones"

    def test_clean_vs_title_unchanged(self) -> None:
        """A clean 'A vs. B' title is preserved verbatim."""
        assert _truncate_concatenated_title("Carlos Perea vs. General Motors") == (
            "Carlos Perea vs. General Motors"
        )

    def test_et_al_preserved(self) -> None:
        """'et al.' on a single caption is preserved."""
        assert _truncate_concatenated_title("Smith et al. v. Acme Corp") == (
            "Smith et al. v. Acme Corp"
        )

    def test_trailing_defendant_et_al_preserved(self) -> None:
        """'et al.' on the defendant side of a single caption is preserved."""
        assert (
            _truncate_concatenated_title("Smith v. Acme Corp, et al.")
            == "Smith v. Acme Corp, et al."
        )

    def test_none_returns_none(self) -> None:
        """None input is passed through."""
        assert _truncate_concatenated_title(None) is None

    def test_empty_string_unchanged(self) -> None:
        """Empty string is passed through."""
        assert _truncate_concatenated_title("") == ""

    def test_whitespace_only_unchanged(self) -> None:
        """Whitespace-only input is passed through."""
        assert _truncate_concatenated_title("   ") == "   "

    def test_two_v_concatenated_keeps_first(self) -> None:
        """'A v. B C v. D' -> keeps first 'A v. B' caption."""
        title = "Liangbei Wang v. NetEase, Inc., et al. Manuel Panilag v. Armando Contreras"
        result = _truncate_concatenated_title(title)
        assert result is not None
        # Must retain first caption's adversarial separator.
        assert "Wang" in result
        assert "NetEase" in result
        # Must strip the second caption entirely.
        assert "Panilag" not in result
        assert "Contreras" not in result

    def test_three_patterns_vs_and_v_mixed(self) -> None:
        """Mixed 'v.' / 'vs.' across three captions -> keep only first."""
        title = (
            "Liangbei Wang v. NetEase, Inc., et al. Manuel Panilag v. Armando "
            "Contreras et al. Jin Yin, et al vs Xiaoxiao Lu, et al."
        )
        result = _truncate_concatenated_title(title)
        assert result is not None
        # Must keep the first caption including 'et al.'.
        assert "Wang" in result
        assert "NetEase" in result
        # Must drop all later captions.
        assert "Panilag" not in result
        assert "Contreras" not in result
        assert "Jin Yin" not in result
        assert "Xiaoxiao" not in result
        # No trailing garbage.
        assert result == result.rstrip()

    def test_vs_no_period_concatenated(self) -> None:
        """Bare 'vs' (no period) splits correctly when concatenated."""
        title = "A Corp vs B Inc. X Corp vs Y Inc."
        result = _truncate_concatenated_title(title)
        assert result is not None
        assert "A Corp" in result
        assert "B Inc" in result
        assert "X Corp" not in result
        assert "Y Inc" not in result

    def test_vs_uppercase_concatenated(self) -> None:
        """Uppercase 'VS.' splits correctly when mixed with 'v.'."""
        title = "TAYLOR VS. AMAZON MSC21-02349 Amazon.com, Inc. v. Damien Sean Lamont Ross"
        result = _truncate_concatenated_title(title)
        assert result is not None
        assert "TAYLOR" in result
        assert "AMAZON" in result
        # Second caption must be dropped.  Check for a token unique to the second caption.
        assert "Damien" not in result
        assert "Ross" not in result


# ---------------------------------------------------------------------------
# List-level post-processor
# ---------------------------------------------------------------------------


class TestTruncateConcatenatedCaseTitles:
    """Tests for the list-level post-processor."""

    def test_empty_list(self) -> None:
        """Empty input -> empty output."""
        assert _truncate_concatenated_case_titles([]) == []

    def test_single_clean_title_preserved(self) -> None:
        """A single clean title is unchanged."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="25CV458952",
                extracted_case_title="Carlos Perea vs. General Motors",
                ruling_text="X" * 300,
            ),
        ]
        result = _truncate_concatenated_case_titles(rulings)
        assert result[0].extracted_case_title == "Carlos Perea vs. General Motors"

    def test_concatenated_title_is_truncated(self) -> None:
        """A title with 2+ 'v.' patterns is truncated to the first caption."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="24CV400001",
                extracted_case_title=(
                    "Liangbei Wang v. NetEase, Inc., et al. Manuel "
                    "Panilag v. Armando Contreras et al. Jin Yin, et al "
                    "vs Xiaoxiao Lu, et al."
                ),
                ruling_text="A" * 300,
            ),
        ]
        result = _truncate_concatenated_case_titles(rulings)
        truncated = result[0].extracted_case_title
        assert truncated is not None
        assert "Wang" in truncated
        assert "NetEase" in truncated
        assert "Panilag" not in truncated
        assert "Xiaoxiao" not in truncated

    def test_preserves_all_other_fields(self) -> None:
        """Truncation preserves every non-title field."""
        from framework.llm_schema import (
            ExtractedParty,
            ExtractionOutcome,
            FieldConfidence,
        )

        parties = [ExtractedParty(name="Wang", role="plaintiff")]
        confidence = FieldConfidence(case_number="high", case_title="low")
        rulings = [
            ExtractedRuling(
                extracted_case_number="24CV400001",
                extracted_case_title="Wang v. NetEase A v. B",
                extracted_judge_name="Judge Smith",
                department="1",
                hearing_date="2026-03-24",
                ruling_text="Y" * 400,
                extracted_parties=parties,
                motion_type="msj",
                outcome=ExtractionOutcome.GRANTED,
                case_type="civil",
                confidence=confidence,
            ),
        ]
        result = _truncate_concatenated_case_titles(rulings)
        r = result[0]
        assert r.extracted_case_title is not None
        assert r.extracted_case_title != "Wang v. NetEase A v. B"
        assert "Wang" in r.extracted_case_title
        assert r.extracted_case_number == "24CV400001"
        assert r.extracted_judge_name == "Judge Smith"
        assert r.department == "1"
        assert r.hearing_date == "2026-03-24"
        assert r.ruling_text == "Y" * 400
        assert r.extracted_parties == parties
        assert r.motion_type == "msj"
        assert r.outcome == ExtractionOutcome.GRANTED
        assert r.case_type == "civil"
        assert r.confidence == confidence

    def test_mix_of_clean_and_concatenated(self) -> None:
        """A list with both clean and concatenated titles: only concatenated changes."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="25CV468424",
                extracted_case_title="Freelancer International v. Walter Bright",
                ruling_text="A" * 300,
            ),
            ExtractedRuling(
                extracted_case_number="25CV468425",
                extracted_case_title=(
                    "Freelancer International v. Walter Bright Manuel Panilag v. Armando"
                ),
                ruling_text="B" * 300,
            ),
        ]
        result = _truncate_concatenated_case_titles(rulings)
        # First is unchanged.
        assert result[0].extracted_case_title == "Freelancer International v. Walter Bright"
        # Second is truncated.
        t2 = result[1].extracted_case_title
        assert t2 is not None
        assert "Freelancer" in t2
        assert "Walter Bright" in t2
        assert "Panilag" not in t2
        assert "Armando" not in t2

    def test_none_title_passes_through(self) -> None:
        """A ruling with a None title is passed through."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="25CV000001",
                extracted_case_title=None,
                ruling_text="A" * 300,
            ),
        ]
        result = _truncate_concatenated_case_titles(rulings)
        assert result[0].extracted_case_title is None


# ---------------------------------------------------------------------------
# Integration: post-processor + dedup both applied together
# ---------------------------------------------------------------------------


class TestPostProcessingChain:
    """Verify the two post-processors compose correctly (#2562)."""

    def test_dedup_and_truncate_together(self) -> None:
        """Duplicate ruling_text + concatenated title: both are resolved."""
        same_text = "Z" * 400
        rulings = [
            ExtractedRuling(
                extracted_case_number="25CV458952",
                extracted_case_title="Carlos Perea vs. General Motors",
                ruling_text=same_text,
            ),
            ExtractedRuling(
                extracted_case_number="25CV458953",
                extracted_case_title="Carlos Perea vs. General Motors Other Party v. Another",
                ruling_text=same_text,
            ),
        ]
        # Apply truncation first, then dedup (matches the real pipeline order).
        rulings = _truncate_concatenated_case_titles(rulings)
        rulings = _deduplicate_ruling_texts(rulings)
        # First keeps its text and its already-clean title.
        assert rulings[0].extracted_case_title == "Carlos Perea vs. General Motors"
        assert rulings[0].ruling_text == same_text
        # Second has its duplicate text nulled AND its title truncated.
        assert rulings[1].ruling_text is None
        assert rulings[1].extracted_case_title is not None
        assert "Carlos Perea" in rulings[1].extracted_case_title
        assert "Other Party" not in rulings[1].extracted_case_title
        assert "Another" not in rulings[1].extracted_case_title


# ---------------------------------------------------------------------------
# Contract: the multi-adversarial deterministic rule must NOT flag the output
# ---------------------------------------------------------------------------


class TestPostProcessingDefeatsDeterministicFlag:
    """After truncation, the deterministic rule no longer flags the ruling."""

    def test_truncated_title_does_not_flag(self) -> None:
        """Running truncation removes the multi-v. concatenation."""
        from validation.deterministic import check_no_multiple_adversarial_patterns

        title = (
            "Liangbei Wang v. NetEase, Inc., et al. Manuel Panilag v. "
            "Armando Contreras et al. Jin Yin, et al vs Xiaoxiao Lu, et al."
        )
        # Before truncation: flagged.
        assert check_no_multiple_adversarial_patterns(title).result == "flag"
        # After truncation: must pass (1 or 0 adversarial patterns remain).
        truncated = _truncate_concatenated_title(title)
        assert truncated is not None
        pass_result = check_no_multiple_adversarial_patterns(truncated)
        assert pass_result.result == "pass", (
            f"truncated title {truncated!r} still flags: {pass_result.reason}"
        )


# ---------------------------------------------------------------------------
# Regression: fixture-driven example from dev-DB observation (issue #2562)
# ---------------------------------------------------------------------------


# A small handful of title strings that were observed in dev-DB rows or in
# the umbrella #2371 issue body.  Each pair is (input_title, must_contain,
# must_not_contain).  The guard must keep first-caption tokens and drop
# later-caption tokens.
_DEV_DB_CONTAMINATED_TITLES: list[tuple[str, list[str], list[str]]] = [
    (
        "Liangbei Wang v. NetEase, Inc., et al. Manuel Panilag v. Armando "
        "Contreras et al. Jin Yin, et al vs Xiaoxiao Lu, et al.",
        ["Wang", "NetEase"],
        ["Panilag", "Contreras", "Jin Yin", "Xiaoxiao"],
    ),
    (
        "TAYLOR VS. AMAZON MSC21-02349 Amazon.com, Inc. v. Damien Sean Lamont Ross",
        ["TAYLOR", "AMAZON"],
        ["Damien", "Ross"],
    ),
    (
        "Smith v. Jones Doe v. Roe",
        ["Smith", "Jones"],
        ["Doe", "Roe"],
    ),
]


@pytest.mark.parametrize(
    "title,must_contain,must_not_contain",
    _DEV_DB_CONTAMINATED_TITLES,
)
def test_dev_db_contaminated_titles(
    title: str,
    must_contain: list[str],
    must_not_contain: list[str],
) -> None:
    """Dev-DB observed contaminated titles are truncated to the first caption."""
    truncated = _truncate_concatenated_title(title)
    assert truncated is not None
    for token in must_contain:
        assert token in truncated, (
            f"{title!r} -> {truncated!r} missing required first-caption token {token!r}"
        )
    for token in must_not_contain:
        assert token not in truncated, (
            f"{title!r} -> {truncated!r} still contains later-caption token {token!r}"
        )
