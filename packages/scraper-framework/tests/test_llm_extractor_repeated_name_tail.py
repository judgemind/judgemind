"""Tests for the repeated-name tail sanitizer (#3684).

Some LLM extractions produce a ``extracted_case_title`` that contains the
party name(s) repeated verbatim at the tail — for example::

    "Hearns vs. FCA US, LLC. Tenaya Hearns Tenaya Hearns Tenaya Hearns Tenaya Hearns"

should be sanitized to::

    "Hearns vs. FCA US, LLC."

This module exercises two new pieces of behaviour:

1. ``_truncate_repeated_name_tail`` — single-string helper that strips a
   trailing run of repeated N-grams (N in 1–3) made up of capitalized tokens.

2. ``_truncate_repeated_name_tails`` — list-level post-processor that wraps
   the helper, rewrites via ``model_copy``, and logs a warning on each change.
"""

from __future__ import annotations

import pytest

from framework.llm_extractor import (
    _truncate_repeated_name_tail,
    _truncate_repeated_name_tails,
)
from framework.llm_schema import ExtractedRuling

# ---------------------------------------------------------------------------
# Table-driven unit tests for the single-string helper
# ---------------------------------------------------------------------------

_CASES: list[tuple[str | None, str | None]] = [
    # Case 1: 4-rep 2-gram tail is stripped to the caption
    (
        "Hearns vs. FCA US, LLC. Tenaya Hearns Tenaya Hearns Tenaya Hearns Tenaya Hearns",
        "Hearns vs. FCA US, LLC.",
    ),
    # Case 2: 2-rep 2-gram tail (Maintenance Corporation repeated)
    (
        "Lawrence vs. The Cape Series at Aliso Viejo "
        "Maintenance Corporation Maintenance Corporation",
        "Lawrence vs. The Cape Series at Aliso Viejo Maintenance Corporation",
    ),
    # Case 3: 2-rep 2-gram tail (Alauney Davis repeated)
    (
        "Claim of: Alauney Davis Alauney Davis",
        "Claim of: Alauney Davis",
    ),
    # Case 4 (no-op): "Smith v. Smith" — legitimate same-surname caption, no tail repeat
    (
        "Smith v. Smith",
        "Smith v. Smith",
    ),
    # Case 5 (no-op): "Smith Smith v. Jones" — doubled name at HEAD, not tail
    (
        "Smith Smith v. Jones",
        "Smith Smith v. Jones",
    ),
    # Case 7: None returns None
    (None, None),
    # Case 7b: empty string returns unchanged
    ("", ""),
    # Case 7c: whitespace-only returns unchanged
    ("   ", "   "),
]


@pytest.mark.parametrize("title,expected", _CASES)
def test_truncate_repeated_name_tail_parametrized(title: str | None, expected: str | None) -> None:
    """All table-driven cases for the single-string helper."""
    result = _truncate_repeated_name_tail(title)
    assert result == expected, (
        f"_truncate_repeated_name_tail({title!r}) -> {result!r}, want {expected!r}"
    )


def test_truncate_repeated_name_tail_idempotent_case1() -> None:
    """Applying the helper twice == applying once (case 1)."""
    title = "Hearns vs. FCA US, LLC. Tenaya Hearns Tenaya Hearns Tenaya Hearns Tenaya Hearns"
    once = _truncate_repeated_name_tail(title)
    twice = _truncate_repeated_name_tail(once)
    assert once == twice, f"Not idempotent: once={once!r}, twice={twice!r}"


def test_truncate_repeated_name_tail_idempotent_case2() -> None:
    """Applying the helper twice == applying once (case 2)."""
    title = (
        "Lawrence vs. The Cape Series at Aliso Viejo "
        "Maintenance Corporation Maintenance Corporation"
    )
    once = _truncate_repeated_name_tail(title)
    twice = _truncate_repeated_name_tail(once)
    assert once == twice, f"Not idempotent: once={once!r}, twice={twice!r}"


def test_truncate_repeated_name_tail_idempotent_case3() -> None:
    """Applying the helper twice == applying once (case 3)."""
    title = "Claim of: Alauney Davis Alauney Davis"
    once = _truncate_repeated_name_tail(title)
    twice = _truncate_repeated_name_tail(once)
    assert once == twice, f"Not idempotent: once={once!r}, twice={twice!r}"


# ---------------------------------------------------------------------------
# Post-processor level test
# ---------------------------------------------------------------------------


class TestTruncateRepeatedNameTails:
    """Tests for the list-level post-processor."""

    def test_empty_list(self) -> None:
        """Empty input -> empty output."""
        assert _truncate_repeated_name_tails([]) == []

    def test_clean_title_preserved(self) -> None:
        """A ruling with a clean title is unchanged."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="24OC12345",
                extracted_case_title="Hearns vs. FCA US, LLC.",
                ruling_text="X" * 300,
            ),
        ]
        result = _truncate_repeated_name_tails(rulings)
        assert result[0].extracted_case_title == "Hearns vs. FCA US, LLC."

    def test_doubled_tail_is_rewritten(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A ruling with a doubled-name tail is rewritten and a warning is logged."""
        original_title = (
            "Hearns vs. FCA US, LLC. Tenaya Hearns Tenaya Hearns Tenaya Hearns Tenaya Hearns"
        )
        rulings = [
            ExtractedRuling(
                extracted_case_number="24OC12345",
                extracted_case_title=original_title,
                ruling_text="A" * 300,
            ),
        ]
        result = _truncate_repeated_name_tails(rulings)

        truncated = result[0].extracted_case_title
        assert truncated == "Hearns vs. FCA US, LLC."
        # Check that a structlog warning was emitted with the right key
        # (structlog writes to stdout; caplog does not capture it)
        captured = capsys.readouterr()
        assert "truncate_repeated_name_tail" in captured.out, (
            f"Expected 'truncate_repeated_name_tail' in stdout log output, got: {captured.out!r}"
        )

    def test_preserves_all_other_fields(self) -> None:
        """Truncation preserves every non-title field."""
        original_title = "Claim of: Alauney Davis Alauney Davis"
        rulings = [
            ExtractedRuling(
                extracted_case_number="24CC99999",
                extracted_case_title=original_title,
                extracted_judge_name="Judge Brown",
                department="7",
                hearing_date="2026-04-01",
                ruling_text="Y" * 400,
            ),
        ]
        result = _truncate_repeated_name_tails(rulings)
        r = result[0]
        assert r.extracted_case_title == "Claim of: Alauney Davis"
        assert r.extracted_case_number == "24CC99999"
        assert r.extracted_judge_name == "Judge Brown"
        assert r.department == "7"
        assert r.hearing_date == "2026-04-01"
        assert r.ruling_text == "Y" * 400

    def test_none_title_passes_through(self) -> None:
        """A ruling with a None title is passed through."""
        rulings = [
            ExtractedRuling(
                extracted_case_number="25OC00001",
                extracted_case_title=None,
                ruling_text="A" * 300,
            ),
        ]
        result = _truncate_repeated_name_tails(rulings)
        assert result[0].extracted_case_title is None
