"""Tests for framework.enrichment — enrichment layer with fuzzy matching.

Covers:
  - Case number normalization
  - Levenshtein distance computation
  - Party overlap (Jaccard similarity)
  - Case number fuzzy matching with DB mocks
  - Judge resolution against aliases and directory snapshots
  - Party resolution against aliases
  - Court portal lookup interface (stub)
  - Full enrichment pipeline
  - Logging for corrections and low-confidence resolutions
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from framework.enrichment import (
    CourtPortalLookup,
    EnrichmentEngine,
    EnrichmentResult,
    _normalize_for_db_lookup,
    compute_party_overlap,
    levenshtein_distance,
    normalize_case_number,
)

# ---------------------------------------------------------------------------
# Case number normalization
# ---------------------------------------------------------------------------


class TestNormalizeCaseNumber:
    """Unit tests for normalize_case_number."""

    def test_strips_whitespace(self) -> None:
        assert normalize_case_number("  CIV123  ") == "CIV123"

    def test_removes_hyphens(self) -> None:
        assert normalize_case_number("CIV-123-456") == "CIV123456"

    def test_removes_internal_spaces(self) -> None:
        assert normalize_case_number("CIV 123 456") == "CIV123456"

    def test_standardizes_cv_prefix(self) -> None:
        assert normalize_case_number("cv-2024-001") == "CIV2024001"

    def test_standardizes_civil_prefix(self) -> None:
        assert normalize_case_number("civil2024001") == "CIV2024001"

    def test_standardizes_cr_prefix(self) -> None:
        assert normalize_case_number("cr-2024-001") == "CR2024001"

    def test_standardizes_crim_prefix(self) -> None:
        assert normalize_case_number("crim2024001") == "CR2024001"

    def test_standardizes_fl_prefix(self) -> None:
        assert normalize_case_number("fam-2024-001") == "FL2024001"

    def test_standardizes_pr_prefix(self) -> None:
        assert normalize_case_number("prob2024001") == "PR2024001"

    def test_standardizes_sc_prefix(self) -> None:
        assert normalize_case_number("sm2024001") == "SC2024001"

    def test_standardizes_jv_prefix(self) -> None:
        assert normalize_case_number("juv2024001") == "JV2024001"

    def test_unknown_prefix_uppercased(self) -> None:
        assert normalize_case_number("abc-123") == "ABC123"

    def test_pure_numeric(self) -> None:
        assert normalize_case_number("123456") == "123456"

    def test_already_normalized(self) -> None:
        assert normalize_case_number("CIV2024001") == "CIV2024001"


# ---------------------------------------------------------------------------
# Levenshtein distance
# ---------------------------------------------------------------------------
# DB normalization helper
# ---------------------------------------------------------------------------


class TestNormalizeForDbLookup:
    """Unit tests for _normalize_for_db_lookup — matches ingestion.db.upsert_case normalization."""

    def test_matches_db_normalization(self) -> None:
        """Verify our normalization matches what upsert_case stores."""
        # upsert_case does: case_number.strip().lower().replace(" ", "").replace("-", "")
        raw = " CIV-2024 001 "
        assert _normalize_for_db_lookup(raw) == "civ2024001"

    def test_lowercase(self) -> None:
        assert _normalize_for_db_lookup("CIV2024001") == "civ2024001"

    def test_strips_hyphens(self) -> None:
        assert _normalize_for_db_lookup("CIV-2024-001") == "civ2024001"

    def test_strips_spaces(self) -> None:
        assert _normalize_for_db_lookup("CIV 2024 001") == "civ2024001"


# ---------------------------------------------------------------------------
# Levenshtein distance
# ---------------------------------------------------------------------------


class TestLevenshteinDistance:
    """Unit tests for levenshtein_distance."""

    def test_identical_strings(self) -> None:
        assert levenshtein_distance("abc", "abc") == 0

    def test_single_insertion(self) -> None:
        assert levenshtein_distance("abc", "abcd") == 1

    def test_single_deletion(self) -> None:
        assert levenshtein_distance("abcd", "abc") == 1

    def test_single_substitution(self) -> None:
        assert levenshtein_distance("abc", "aXc") == 1

    def test_transposition(self) -> None:
        # Transposition requires 2 ops in standard Levenshtein
        assert levenshtein_distance("ab", "ba") == 2

    def test_empty_strings(self) -> None:
        assert levenshtein_distance("", "") == 0

    def test_one_empty(self) -> None:
        assert levenshtein_distance("abc", "") == 3
        assert levenshtein_distance("", "xyz") == 3

    def test_case_number_typo(self) -> None:
        # Transposed digit: CIV2024001 vs CIV2024010
        assert levenshtein_distance("CIV2024001", "CIV2024010") == 2

    def test_missing_prefix(self) -> None:
        # Missing prefix character
        assert levenshtein_distance("CIV2024001", "IV2024001") == 1

    def test_wrong_check_digit(self) -> None:
        # Wrong last digit
        assert levenshtein_distance("CIV2024001", "CIV2024002") == 1

    def test_completely_different(self) -> None:
        dist = levenshtein_distance("ABC", "XYZ")
        assert dist == 3

    def test_fallback_works_without_rapidfuzz(self) -> None:
        """Verify the stdlib fallback produces correct results."""
        with patch.dict("sys.modules", {"rapidfuzz": None, "rapidfuzz.distance": None}):
            # Force re-import or call the fallback path
            # The function tries rapidfuzz first and falls back
            # Just verify the function returns correct results
            assert levenshtein_distance("kitten", "sitting") == 3


# ---------------------------------------------------------------------------
# Party overlap
# ---------------------------------------------------------------------------


class TestComputePartyOverlap:
    """Unit tests for compute_party_overlap (Jaccard similarity)."""

    def test_identical_lists(self) -> None:
        assert compute_party_overlap(["Alice", "Bob"], ["Alice", "Bob"]) == 1.0

    def test_no_overlap(self) -> None:
        assert compute_party_overlap(["Alice"], ["Bob"]) == 0.0

    def test_partial_overlap(self) -> None:
        result = compute_party_overlap(["Alice", "Bob"], ["Alice", "Charlie"])
        assert result == pytest.approx(1 / 3)  # 1 common / 3 unique

    def test_case_insensitive(self) -> None:
        assert compute_party_overlap(["alice"], ["ALICE"]) == 1.0

    def test_both_empty(self) -> None:
        assert compute_party_overlap([], []) == 0.0

    def test_one_empty(self) -> None:
        assert compute_party_overlap(["Alice"], []) == 0.0
        assert compute_party_overlap([], ["Bob"]) == 0.0

    def test_strips_whitespace(self) -> None:
        assert compute_party_overlap(["  Alice  "], ["Alice"]) == 1.0

    def test_ignores_empty_names(self) -> None:
        assert compute_party_overlap(["Alice", ""], ["Alice"]) == 1.0

    def test_superset(self) -> None:
        result = compute_party_overlap(["Alice", "Bob", "Charlie"], ["Alice", "Bob"])
        assert result == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# EnrichmentEngine — Case number matching
# ---------------------------------------------------------------------------


def _make_mock_conn() -> MagicMock:
    """Create a mock psycopg connection with cursor context manager."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


class TestMatchCaseNumber:
    """Tests for EnrichmentEngine.match_case_number."""

    def test_exact_match(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        # Exact match returns a row
        cursor.fetchone.return_value = ("case-uuid-123",)

        engine = EnrichmentEngine(conn)
        result = engine.match_case_number("CIV-2024-001", "court-uuid")

        assert result.match_type == "exact"
        assert result.case_id == "case-uuid-123"
        assert result.confidence == 1.0

    def test_no_match_returns_new(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        # No exact match, no fuzzy candidates
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []

        engine = EnrichmentEngine(conn)
        result = engine.match_case_number("CIV-2024-001", "court-uuid")

        assert result.match_type == "new"
        assert result.case_id == ""
        assert result.confidence == 0.0

    def test_fuzzy_match_with_party_overlap(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        # No exact match
        cursor.fetchone.return_value = None
        # Fuzzy candidates: one case with similar number and overlapping parties
        cursor.fetchall.return_value = [
            ("case-uuid-456", "CIV2024002", ["alice", "bob"]),
        ]

        engine = EnrichmentEngine(conn)
        result = engine.match_case_number(
            "CIV-2024-001",
            "court-uuid",
            extracted_parties=["Alice", "Bob"],
        )

        assert result.match_type == "fuzzy"
        assert result.case_id == "case-uuid-456"
        assert result.levenshtein_distance == 1
        assert result.party_overlap > 0
        assert result.confidence > 0
        assert result.corrected_from == "CIV-2024-001"

    def test_fuzzy_match_rejected_without_party_overlap(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = [
            ("case-uuid-456", "CIV2024002", ["charlie", "dave"]),
        ]

        engine = EnrichmentEngine(conn)
        result = engine.match_case_number(
            "CIV-2024-001",
            "court-uuid",
            extracted_parties=["Alice", "Bob"],
        )

        # No party overlap -> rejected
        assert result.match_type == "new"

    def test_fuzzy_match_accepted_without_parties(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = [
            ("case-uuid-456", "CIV2024002", []),
        ]

        engine = EnrichmentEngine(conn)
        result = engine.match_case_number(
            "CIV-2024-001",
            "court-uuid",
            extracted_parties=[],
        )

        # No parties to compare -> fuzzy match accepted on distance alone
        assert result.match_type == "fuzzy"
        assert result.levenshtein_distance == 1

    def test_fuzzy_match_too_distant_rejected(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = [
            ("case-uuid-789", "CIV2024999", ["alice"]),
        ]

        engine = EnrichmentEngine(conn, max_levenshtein=2)
        result = engine.match_case_number(
            "CIV-2024-001",
            "court-uuid",
            extracted_parties=["Alice"],
        )

        # Distance > 2 -> rejected
        assert result.match_type == "new"

    def test_best_fuzzy_match_selected(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None
        # Two candidates: one with distance 2 and one with distance 1
        cursor.fetchall.return_value = [
            ("case-uuid-a", "CIV2024099", ["alice"]),  # distance=2
            ("case-uuid-b", "CIV2024002", ["alice"]),  # distance=1
        ]

        engine = EnrichmentEngine(conn)
        result = engine.match_case_number(
            "CIV-2024-001",
            "court-uuid",
            extracted_parties=["Alice"],
        )

        # The closer candidate (distance=1) should win
        assert result.case_id == "case-uuid-b"
        assert result.confidence > 0.5


# ---------------------------------------------------------------------------
# EnrichmentEngine — Fuzzy candidate query filtering
# ---------------------------------------------------------------------------


class TestFuzzyCaseCandidates:
    """Tests for _fuzzy_case_candidates prefix filtering and row cap."""

    def test_prefix_filter_applied_for_long_case_numbers(self) -> None:
        """Verify that the SQL includes a prefix filter when the case number >= 4 chars."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchall.return_value = []

        engine = EnrichmentEngine(conn)
        # "civ2024001" is 10 chars — well above the 4-char minimum for prefix filtering
        engine._fuzzy_case_candidates("civ2024001", "court-uuid")

        # The query should have been called with 6 params (court_id, min_len, max_len,
        # prefix_length, prefix, limit) instead of the old 3 (court_id, min_len, max_len)
        call_args = cursor.execute.call_args
        params = call_args[0][1]
        assert len(params) == 6
        # Prefix should be "ci" (first 2 chars of "civ2024001")
        assert params[4] == "ci"

    def test_prefix_filter_skipped_for_short_case_numbers(self) -> None:
        """Verify that short case numbers (< 4 chars) skip the prefix filter."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchall.return_value = []

        engine = EnrichmentEngine(conn)
        # "abc" is only 3 chars — below the 4-char threshold
        engine._fuzzy_case_candidates("abc", "court-uuid")

        call_args = cursor.execute.call_args
        params = call_args[0][1]
        # Without prefix filter: (court_id, min_len, max_len, limit) = 4 params
        assert len(params) == 4

    def test_limit_applied_to_query(self) -> None:
        """Verify that the LIMIT clause is present in the query."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchall.return_value = []

        engine = EnrichmentEngine(conn, max_fuzzy_candidates=100)
        engine._fuzzy_case_candidates("civ2024001", "court-uuid")

        call_args = cursor.execute.call_args
        query_sql = call_args[0][0]
        params = call_args[0][1]
        assert "LIMIT" in query_sql
        # The last param is the limit value
        assert params[-1] == 100

    def test_default_max_fuzzy_candidates_is_500(self) -> None:
        """Verify the default cap is 500."""
        conn = _make_mock_conn()
        engine = EnrichmentEngine(conn)
        assert engine._max_fuzzy_candidates == 500

    def test_custom_max_fuzzy_candidates(self) -> None:
        """Verify the cap is configurable via constructor."""
        conn = _make_mock_conn()
        engine = EnrichmentEngine(conn, max_fuzzy_candidates=200)
        assert engine._max_fuzzy_candidates == 200

    def test_warning_logged_when_cap_reached(self) -> None:
        """Verify a warning is logged when the result set hits the cap."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        # Return exactly max_fuzzy_candidates rows to trigger the warning
        cap = 3
        cursor.fetchall.return_value = [(f"uuid-{i}", f"CIV202400{i}", []) for i in range(cap)]

        engine = EnrichmentEngine(conn, max_fuzzy_candidates=cap)
        with patch("framework.enrichment.logger") as mock_logger:
            engine._fuzzy_case_candidates("civ2024001", "court-uuid")
            mock_logger.warning.assert_called_once()
            call_kwargs = mock_logger.warning.call_args[1]
            assert call_kwargs["cap"] == cap

    def test_no_warning_when_below_cap(self) -> None:
        """Verify no warning when results are below the cap."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchall.return_value = [
            ("uuid-1", "CIV2024001", []),
        ]

        engine = EnrichmentEngine(conn, max_fuzzy_candidates=500)
        with patch("framework.enrichment.logger") as mock_logger:
            engine._fuzzy_case_candidates("civ2024001", "court-uuid")
            mock_logger.warning.assert_not_called()

    def test_prefix_filter_uses_correct_prefix(self) -> None:
        """Verify the prefix is extracted correctly from different case numbers."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchall.return_value = []

        engine = EnrichmentEngine(conn)

        # "24nncv02551" -> prefix "24"
        engine._fuzzy_case_candidates("24nncv02551", "court-uuid")
        params = cursor.execute.call_args[0][1]
        assert params[4] == "24"

        # "civ2024001" -> prefix "ci"
        engine._fuzzy_case_candidates("civ2024001", "court-uuid")
        params = cursor.execute.call_args[0][1]
        assert params[4] == "ci"

    def test_end_to_end_fuzzy_with_prefix_filter(self) -> None:
        """End-to-end: match_case_number with prefix-filtered candidates."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None  # No exact match
        cursor.fetchall.return_value = [
            ("case-uuid-456", "CIV2024002", ["alice"]),
        ]

        engine = EnrichmentEngine(conn, max_fuzzy_candidates=100)
        result = engine.match_case_number(
            "CIV-2024-001",
            "court-uuid",
            extracted_parties=["Alice"],
        )

        # Should still find the fuzzy match
        assert result.match_type == "fuzzy"
        assert result.case_id == "case-uuid-456"

    def test_results_correctly_parsed(self) -> None:
        """Verify that _fuzzy_case_candidates parses DB rows correctly."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchall.return_value = [
            ("uuid-1", "CIV2024001", ["Alice", "Bob"]),
            ("uuid-2", "CIV2024002", None),
        ]

        engine = EnrichmentEngine(conn)
        results = engine._fuzzy_case_candidates("civ2024001", "court-uuid")

        assert len(results) == 2
        assert results[0] == ("uuid-1", "CIV2024001", ["Alice", "Bob"])
        assert results[1] == ("uuid-2", "CIV2024002", [])

    def test_exact_boundary_prefix_filter_min_len(self) -> None:
        """Verify prefix filter is applied at exactly the minimum length (4 chars)."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchall.return_value = []

        engine = EnrichmentEngine(conn)
        # Exactly 4 chars — should use prefix filter
        engine._fuzzy_case_candidates("abcd", "court-uuid")
        params = cursor.execute.call_args[0][1]
        assert len(params) == 6  # prefix filter applied
        assert params[4] == "ab"  # first 2 chars

    def test_length_filter_still_applied(self) -> None:
        """Verify the length filter (LENGTH BETWEEN) is still present."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchall.return_value = []

        engine = EnrichmentEngine(conn, max_levenshtein=2)
        engine._fuzzy_case_candidates("civ2024001", "court-uuid")

        call_args = cursor.execute.call_args
        query_sql = call_args[0][0]
        params = call_args[0][1]
        assert "BETWEEN" in query_sql
        # min_len = max(1, 10 - 2) = 8, max_len = 10 + 2 = 12
        assert params[1] == 8
        assert params[2] == 12


# ---------------------------------------------------------------------------
# EnrichmentEngine — Judge resolution
# ---------------------------------------------------------------------------


class TestResolveJudge:
    """Tests for EnrichmentEngine.resolve_judge."""

    def test_alias_match(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = ("judge-uuid-123", "John A. Smith")

        engine = EnrichmentEngine(conn)
        result = engine.resolve_judge("Hon. John Smith", "court-uuid")

        assert result.match_type == "alias"
        assert result.judge_id == "judge-uuid-123"
        assert result.canonical_name == "John A. Smith"
        assert result.confidence == 1.0

    def test_no_alias_returns_new(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None

        engine = EnrichmentEngine(conn)
        result = engine.resolve_judge("New Judge Name", "court-uuid")

        assert result.match_type == "new"
        assert result.judge_id is None
        assert result.confidence == 0.0

    def test_empty_name_returns_invalid(self) -> None:
        conn = _make_mock_conn()
        engine = EnrichmentEngine(conn)

        result = engine.resolve_judge("", "court-uuid")
        assert result.match_type == "invalid"
        assert result.judge_id is None

    def test_whitespace_name_returns_invalid(self) -> None:
        conn = _make_mock_conn()
        engine = EnrichmentEngine(conn)

        result = engine.resolve_judge("   ", "court-uuid")
        assert result.match_type == "invalid"

    def test_directory_match_no_mismatch(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        # First call: alias lookup returns a match
        # Second call: directory snapshot
        cursor.fetchone.side_effect = [
            ("judge-uuid-123", "John Smith"),  # alias match
            ({"Dept 1": "John Smith"},),  # directory snapshot
        ]

        engine = EnrichmentEngine(conn)
        hearing = datetime(2024, 6, 15)
        result = engine.resolve_judge(
            "John Smith", "court-uuid", department="Dept 1", hearing_date=hearing
        )

        assert result.match_type == "alias"
        assert result.directory_mismatch is False
        assert result.confidence == 1.0

    def test_directory_mismatch_flagged(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.side_effect = [
            ("judge-uuid-123", "John Smith"),  # alias match
            ({"Dept 1": "Jane Doe"},),  # directory shows different judge
        ]

        engine = EnrichmentEngine(conn)
        hearing = datetime(2024, 6, 15)
        result = engine.resolve_judge(
            "John Smith", "court-uuid", department="Dept 1", hearing_date=hearing
        )

        assert result.match_type == "alias"
        assert result.directory_mismatch is True
        assert result.directory_judge == "Jane Doe"
        assert result.confidence == 0.7

    def test_directory_case_insensitive_dept_lookup(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.side_effect = [
            ("judge-uuid-123", "John Smith"),  # alias match
            ({"dept 1": "John Smith"},),  # directory with lowercase dept key
        ]

        engine = EnrichmentEngine(conn)
        hearing = datetime(2024, 6, 15)
        result = engine.resolve_judge(
            "John Smith", "court-uuid", department="Dept 1", hearing_date=hearing
        )

        assert result.directory_mismatch is False

    def test_no_directory_snapshot_no_mismatch(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.side_effect = [
            ("judge-uuid-123", "John Smith"),  # alias match
            None,  # no directory snapshot
        ]

        engine = EnrichmentEngine(conn)
        hearing = datetime(2024, 6, 15)
        result = engine.resolve_judge(
            "John Smith", "court-uuid", department="Dept 1", hearing_date=hearing
        )

        assert result.directory_mismatch is False
        assert result.confidence == 1.0

    def test_null_directory_mapping_handled(self) -> None:
        """Verify that a NULL mapping column doesn't crash the engine."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.side_effect = [
            ("judge-uuid-123", "John Smith"),  # alias match
            (None,),  # directory snapshot with NULL mapping
        ]

        engine = EnrichmentEngine(conn)
        hearing = datetime(2024, 6, 15)
        result = engine.resolve_judge(
            "John Smith", "court-uuid", department="Dept 1", hearing_date=hearing
        )

        # Should not crash; no directory data means no mismatch
        assert result.directory_mismatch is False
        assert result.confidence == 1.0

    def test_malformed_directory_mapping_handled(self) -> None:
        """Verify that a malformed JSON mapping doesn't crash the engine."""
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.side_effect = [
            ("judge-uuid-123", "John Smith"),  # alias match
            ("not valid json{{{",),  # malformed JSON mapping
        ]

        engine = EnrichmentEngine(conn)
        hearing = datetime(2024, 6, 15)
        result = engine.resolve_judge(
            "John Smith", "court-uuid", department="Dept 1", hearing_date=hearing
        )

        # Should not crash; gracefully degrade
        assert result.directory_mismatch is False
        assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# EnrichmentEngine — Party resolution
# ---------------------------------------------------------------------------


class TestResolveParty:
    """Tests for EnrichmentEngine.resolve_party."""

    def test_alias_match(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = ("party-uuid-123", "Alice Smith")

        engine = EnrichmentEngine(conn)
        result = engine.resolve_party("ALICE SMITH")

        assert result.match_type == "alias"
        assert result.party_id == "party-uuid-123"
        assert result.canonical_name == "Alice Smith"
        assert result.confidence == 1.0

    def test_no_alias_returns_new(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None

        engine = EnrichmentEngine(conn)
        result = engine.resolve_party("Brand New Party")

        assert result.match_type == "new"
        assert result.party_id == ""
        assert result.canonical_name == "Brand New Party"

    def test_normalize_strips_individual_suffix(self) -> None:
        result = EnrichmentEngine._normalize_party_name("John Doe, an individual")
        assert result == "John Doe"

    def test_normalize_strips_corporation_suffix(self) -> None:
        result = EnrichmentEngine._normalize_party_name("Acme Corp, a corporation")
        assert result == "Acme Corp"

    def test_normalize_strips_llc_descriptor(self) -> None:
        result = EnrichmentEngine._normalize_party_name("Big LLC, a limited liability company")
        assert result == "Big Llc"

    def test_normalize_strips_et_al(self) -> None:
        result = EnrichmentEngine._normalize_party_name("John Doe et al.")
        assert result == "John Doe"

    def test_normalize_strips_individually(self) -> None:
        result = EnrichmentEngine._normalize_party_name("Jane Doe, individually")
        assert result == "Jane Doe"

    def test_normalize_collapses_whitespace(self) -> None:
        result = EnrichmentEngine._normalize_party_name("John   Doe")
        assert result == "John Doe"

    def test_normalize_title_cases(self) -> None:
        result = EnrichmentEngine._normalize_party_name("JOHN DOE")
        assert result == "John Doe"

    def test_normalize_empty_string(self) -> None:
        result = EnrichmentEngine._normalize_party_name("")
        assert result == ""


# ---------------------------------------------------------------------------
# Court portal lookup interface (stub)
# ---------------------------------------------------------------------------


class TestCourtPortalLookup:
    """Tests for the CourtPortalLookup abstract interface."""

    def test_cannot_instantiate_abstract(self) -> None:
        with pytest.raises(TypeError):
            CourtPortalLookup()  # type: ignore[abstract]

    def test_concrete_subclass_works(self) -> None:
        class StubPortal(CourtPortalLookup):
            def lookup_case(self, case_number: str, court_id: str) -> dict[str, object] | None:
                return {
                    "case_number": case_number,
                    "case_title": "Test Case",
                    "parties": [],
                    "filing_date": "2024-01-01",
                    "case_type": "civil",
                }

            def is_available(self) -> bool:
                return True

        portal = StubPortal()
        assert portal.is_available() is True
        result = portal.lookup_case("CIV2024001", "court-uuid")
        assert result is not None
        assert result["case_number"] == "CIV2024001"

    def test_unavailable_portal(self) -> None:
        class OfflinePortal(CourtPortalLookup):
            def lookup_case(self, case_number: str, court_id: str) -> dict[str, object] | None:
                return None

            def is_available(self) -> bool:
                return False

        portal = OfflinePortal()
        assert portal.is_available() is False
        assert portal.lookup_case("CIV2024001", "court-uuid") is None


# ---------------------------------------------------------------------------
# Full enrichment pipeline
# ---------------------------------------------------------------------------


class TestEnrich:
    """Tests for EnrichmentEngine.enrich — the full pipeline."""

    def test_enrich_exact_case_no_judge_no_parties(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = ("case-uuid-123",)

        engine = EnrichmentEngine(conn)
        result = engine.enrich(
            case_number="CIV-2024-001",
            court_id="court-uuid",
        )

        assert isinstance(result, EnrichmentResult)
        assert result.case_match is not None
        assert result.case_match.match_type == "exact"
        assert result.judge_resolution is None
        assert result.party_resolutions == []
        assert result.flags == []

    def test_enrich_with_judge_and_parties(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        # Call sequence:
        # 1. exact case match -> found
        # 2. judge alias lookup -> found
        # 3. party alias lookup -> not found (new)
        cursor.fetchone.side_effect = [
            ("case-uuid-123",),  # exact case match
            ("judge-uuid-456", "Jane Doe"),  # judge alias match
            None,  # party alias lookup -> no match
        ]

        engine = EnrichmentEngine(conn)
        result = engine.enrich(
            case_number="CIV-2024-001",
            court_id="court-uuid",
            judge_name="Hon. Jane Doe",
            parties=["Bob Smith"],
        )

        assert result.case_match.match_type == "exact"
        assert result.judge_resolution is not None
        assert result.judge_resolution.match_type == "alias"
        assert len(result.party_resolutions) == 1
        assert result.party_resolutions[0].match_type == "new"

    def test_enrich_fuzzy_case_generates_flag(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = [
            ("case-uuid-456", "CIV2024002", ["alice"]),
        ]

        engine = EnrichmentEngine(conn)
        result = engine.enrich(
            case_number="CIV-2024-001",
            court_id="court-uuid",
            parties=["Alice"],
        )

        assert result.case_match.match_type == "fuzzy"
        assert len(result.flags) >= 1
        assert "case_number_corrected" in result.flags[0]

    def test_enrich_directory_mismatch_generates_flag(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.side_effect = [
            ("case-uuid-123",),  # exact case match
            ("judge-uuid-123", "John Smith"),  # judge alias match
            ({"Dept 1": "Jane Doe"},),  # directory shows different judge
        ]

        engine = EnrichmentEngine(conn)
        hearing = datetime(2024, 6, 15)
        result = engine.enrich(
            case_number="CIV-2024-001",
            court_id="court-uuid",
            judge_name="John Smith",
            department="Dept 1",
            hearing_date=hearing,
        )

        assert result.judge_resolution.directory_mismatch is True
        assert len(result.flags) >= 1
        assert "judge_directory_mismatch" in result.flags[0]

    def test_enrich_skips_empty_party_names(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        # First call: exact case match -> found
        # Second call: party alias lookup for "Alice" -> not found
        cursor.fetchone.side_effect = [
            ("case-uuid-123",),  # exact case match
            None,  # party alias lookup for Alice -> no match
        ]

        engine = EnrichmentEngine(conn)
        result = engine.enrich(
            case_number="CIV-2024-001",
            court_id="court-uuid",
            parties=["Alice", "", "  "],
        )

        # Only Alice should produce a resolution; empty names skipped
        assert len(result.party_resolutions) == 1
        assert result.party_resolutions[0].canonical_name == "Alice"


# ---------------------------------------------------------------------------
# Logging tests
# ---------------------------------------------------------------------------


class TestEnrichmentLogging:
    """Verify that corrections and low-confidence resolutions are logged."""

    def test_fuzzy_match_logs_correction(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = [
            ("case-uuid-456", "CIV2024002", ["alice"]),
        ]

        engine = EnrichmentEngine(conn)
        with patch("framework.enrichment.logger") as mock_logger:
            engine.match_case_number("CIV-2024-001", "court-uuid", extracted_parties=["Alice"])
            # Should log an info message about the fuzzy match
            mock_logger.info.assert_called()
            call_args = mock_logger.info.call_args
            assert "fuzzy match" in call_args[0][0].lower()

    def test_directory_mismatch_logs_warning(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.side_effect = [
            ("judge-uuid-123", "John Smith"),  # alias match
            ({"Dept 1": "Jane Doe"},),  # directory mismatch
        ]

        engine = EnrichmentEngine(conn)
        with patch("framework.enrichment.logger") as mock_logger:
            engine.resolve_judge(
                "John Smith",
                "court-uuid",
                department="Dept 1",
                hearing_date=datetime(2024, 6, 15),
            )
            mock_logger.warning.assert_called()
            call_args = mock_logger.warning.call_args
            assert "mismatch" in call_args[0][0].lower()

    def test_new_case_logs_debug(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []

        engine = EnrichmentEngine(conn)
        with patch("framework.enrichment.logger") as mock_logger:
            engine.match_case_number("CIV-2024-001", "court-uuid")
            mock_logger.debug.assert_called()

    def test_enrichment_flags_logged(self) -> None:
        conn = _make_mock_conn()
        cursor = conn.cursor().__enter__()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = [
            ("case-uuid-456", "CIV2024002", ["alice"]),
        ]

        engine = EnrichmentEngine(conn)
        with patch("framework.enrichment.logger") as mock_logger:
            engine.enrich(
                case_number="CIV-2024-001",
                court_id="court-uuid",
                parties=["Alice"],
            )
            # Should log enrichment flags
            info_calls = [c for c in mock_logger.info.call_args_list]
            flag_logged = any("flags" in str(c) for c in info_calls)
            assert flag_logged
