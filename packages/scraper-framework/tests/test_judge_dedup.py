"""Tests for judge deduplication and roster-based name matching (#2140).

Covers:
  - _normalize_for_comparison: position-invariant name normalization
  - fuzzy_match_roster_name: multi-strategy fuzzy matching
  - _get_roster_names: roster lookup with court_code conversion
  - resolve_judge roster integration: preventing duplicate creation
  - dedup_judges script: detection and merge logic
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ingestion.db import (
    _get_roster_names,
    _levenshtein_distance,
    _normalize_for_comparison,
    clear_roster_cache,
    fuzzy_match_roster_name,
    normalize_judge_name,
    resolve_judge,
)

# ---------------------------------------------------------------------------
# _levenshtein_distance
# ---------------------------------------------------------------------------


class TestLevenshteinDistance:
    """Unit tests for _levenshtein_distance."""

    def test_identical_strings(self) -> None:
        assert _levenshtein_distance("hello", "hello") == 0

    def test_empty_strings(self) -> None:
        assert _levenshtein_distance("", "") == 0

    def test_one_empty(self) -> None:
        assert _levenshtein_distance("abc", "") == 3

    def test_single_substitution(self) -> None:
        assert _levenshtein_distance("cat", "bat") == 1

    def test_single_insertion(self) -> None:
        assert _levenshtein_distance("cat", "cats") == 1

    def test_single_deletion(self) -> None:
        assert _levenshtein_distance("cats", "cat") == 1

    def test_symmetric(self) -> None:
        assert _levenshtein_distance("abc", "xyz") == _levenshtein_distance("xyz", "abc")

    def test_mccarthey_vs_mccartney(self) -> None:
        """The typo example from issue #2140."""
        # "mccarthey" -> "mccartney": swap h/n = 1 edit
        assert _levenshtein_distance("mccarthey", "mccartney") == 1


# ---------------------------------------------------------------------------
# _normalize_for_comparison
# ---------------------------------------------------------------------------


class TestNormalizeForComparison:
    """Unit tests for _normalize_for_comparison."""

    def test_strips_all_initials_and_sorts(self) -> None:
        result = _normalize_for_comparison("John A. Smith")
        assert result == "john smith"

    def test_initial_placement_invariant(self) -> None:
        """H. Shaina Colover and Shaina H. Colover should produce the same result."""
        a = _normalize_for_comparison("H. Shaina Colover")
        b = _normalize_for_comparison("Shaina H. Colover")
        assert a == b

    def test_removes_periods(self) -> None:
        result = _normalize_for_comparison("Jr. Smith")
        assert "." not in result

    def test_sorts_words(self) -> None:
        result = _normalize_for_comparison("Zebra Alpha")
        assert result == "alpha zebra"

    def test_lowercases(self) -> None:
        result = _normalize_for_comparison("JOHN SMITH")
        assert result == "john smith"

    def test_multi_word_name(self) -> None:
        result = _normalize_for_comparison("Andre De La Cruz")
        assert result == "andre cruz de la"

    def test_partial_name(self) -> None:
        result = _normalize_for_comparison("De La Cruz")
        assert result == "cruz de la"


# ---------------------------------------------------------------------------
# fuzzy_match_roster_name
# ---------------------------------------------------------------------------


class TestFuzzyMatchRosterName:
    """Unit tests for fuzzy_match_roster_name."""

    def test_exact_match(self) -> None:
        result = fuzzy_match_roster_name("John Smith", ["John Smith", "Jane Doe"])
        assert result is not None
        assert result[0] == "John Smith"
        assert result[1] == 1.0

    def test_case_insensitive_exact(self) -> None:
        result = fuzzy_match_roster_name("john smith", ["John Smith"])
        assert result is not None
        assert result[0] == "John Smith"
        assert result[1] == 1.0

    def test_initial_placement_match(self) -> None:
        """Issue #2140 example: H. Shaina Colover vs Shaina H. Colover."""
        result = fuzzy_match_roster_name("H. Shaina Colover", ["Shaina H. Colover"])
        assert result is not None
        assert result[0] == "Shaina H. Colover"
        assert result[1] == 0.95

    def test_typo_match(self) -> None:
        """Issue #2140 example: Jennifer Mccarthey vs Jennifer Mccartney."""
        result = fuzzy_match_roster_name("Jennifer Mccarthey", ["Jennifer Mccartney"])
        assert result is not None
        assert result[0] == "Jennifer Mccartney"
        assert result[1] >= 0.65

    def test_partial_name_subset(self) -> None:
        """Issue #2140 example: De La Cruz vs Andre De La Cruz."""
        result = fuzzy_match_roster_name("De La Cruz", ["Andre De La Cruz"])
        assert result is not None
        assert result[0] == "Andre De La Cruz"
        assert result[1] > 0.0

    def test_no_match_different_names(self) -> None:
        result = fuzzy_match_roster_name("John Smith", ["Jane Doe", "Bob Brown"])
        assert result is None

    def test_empty_roster(self) -> None:
        result = fuzzy_match_roster_name("John Smith", [])
        assert result is None

    def test_empty_candidate(self) -> None:
        result = fuzzy_match_roster_name("", ["John Smith"])
        assert result is None

    def test_best_match_selected(self) -> None:
        """When multiple roster names match, the best match wins."""
        result = fuzzy_match_roster_name(
            "John Smith",
            ["John Smithe", "Jon Smith", "John Smyth"],
        )
        assert result is not None
        # All are close but "Jon Smith" and "John Smithe" are dist=1
        assert result[1] >= 0.65

    def test_no_false_positive_on_short_names(self) -> None:
        """Single-word subsets should not match (require >= 2 words)."""
        result = fuzzy_match_roster_name("Smith", ["John Smith"])
        # "Smith" is only 1 word in normalized form, so subset matching
        # requires >= 2 words. Levenshtein also won't match well.
        # This should return None or very low confidence.
        if result is not None:
            # If it matched via Levenshtein, the distance is high enough
            # that confidence should be low
            assert result[1] < 0.7

    def test_middle_initial_difference(self) -> None:
        """Names differing only by middle initial should match."""
        result = fuzzy_match_roster_name("Carmen R. Luege", ["Carmen Luege"])
        assert result is not None
        assert result[0] == "Carmen Luege"
        assert result[1] >= 0.85

    def test_exact_match_preferred_over_sorted_word(self) -> None:
        """An exact match (1.0) should be preferred over a sorted-word match (0.95).

        Regression test for adversarial review finding: if a sorted-word match
        appears first in the roster, it should not short-circuit and miss the
        exact match that appears later.
        """
        # "John A. Smith" normalizes the same as "John Smith" (initials stripped),
        # so "John Smith" would be a 0.95 sorted-word match. But the exact match
        # "John A. Smith" should win at 1.0.
        result = fuzzy_match_roster_name("John A. Smith", ["John Smith", "John A. Smith"])
        assert result is not None
        assert result[0] == "John A. Smith"
        assert result[1] == 1.0


# ---------------------------------------------------------------------------
# _get_roster_names
# ---------------------------------------------------------------------------


class TestGetRosterNames:
    """Unit tests for _get_roster_names."""

    def test_returns_names_from_snapshot(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # First query: court_code lookup
        # Second query: snapshot mapping
        mock_cur.fetchone.side_effect = [
            ("ca-orange",),  # court_code
            ({"D1": "John Smith", "D2": "Jane Doe", "D3": "John Smith"},),  # mapping
        ]

        result = _get_roster_names(mock_conn, "court-uuid-1")

        # Should return unique names
        assert sorted(result) == ["Jane Doe", "John Smith"]

    def test_converts_court_code_format(self) -> None:
        """Verifies court_code hyphens are converted to underscores for snapshot lookup."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.side_effect = [
            ("ca-los-angeles",),  # court_code with hyphens
            ({"D1": "Judge A"},),
        ]

        _get_roster_names(mock_conn, "court-uuid-1")

        # Verify the second query used underscores
        second_call_args = mock_cur.execute.call_args_list[1]
        assert "ca_los_angeles" in str(second_call_args)

    def test_returns_empty_for_missing_court(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.return_value = None

        result = _get_roster_names(mock_conn, "nonexistent-court")
        assert result == []

    def test_returns_empty_for_missing_snapshot(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.side_effect = [
            ("ca-orange",),  # court_code exists
            None,  # but no snapshot
        ]

        result = _get_roster_names(mock_conn, "court-uuid-1")
        assert result == []

    def test_handles_json_string_mapping(self) -> None:
        """Handles case where mapping is stored as a JSON string, not dict."""
        import json

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mapping = {"D1": "Judge Alpha"}
        mock_cur.fetchone.side_effect = [
            ("ca-orange",),
            (json.dumps(mapping),),  # JSON string instead of dict
        ]

        result = _get_roster_names(mock_conn, "court-uuid-1")
        assert result == ["Judge Alpha"]


# ---------------------------------------------------------------------------
# _get_roster_names — caching
# ---------------------------------------------------------------------------


class TestGetRosterNamesCache:
    """Tests for the TTL cache in _get_roster_names."""

    def test_second_call_uses_cache(self) -> None:
        """Second call for the same court_id does not hit the database."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.side_effect = [
            ("ca-orange",),
            ({"D1": "John Smith"},),
        ]

        # First call populates the cache
        result1 = _get_roster_names(mock_conn, "court-uuid-cache")
        assert result1 == ["John Smith"]
        assert mock_cur.execute.call_count == 2

        # Second call should use the cache (no additional queries)
        result2 = _get_roster_names(mock_conn, "court-uuid-cache")
        assert result2 == ["John Smith"]
        assert mock_cur.execute.call_count == 2  # unchanged

    def test_different_court_ids_cached_separately(self) -> None:
        """Each court_id has its own cache entry."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.side_effect = [
            ("ca-orange",),
            ({"D1": "Judge A"},),
            ("ca-la",),
            ({"D1": "Judge B"},),
        ]

        result_a = _get_roster_names(mock_conn, "court-a")
        result_b = _get_roster_names(mock_conn, "court-b")

        assert result_a == ["Judge A"]
        assert result_b == ["Judge B"]
        assert mock_cur.execute.call_count == 4  # 2 queries per court

    def test_clear_roster_cache_all(self) -> None:
        """clear_roster_cache() without args clears the entire cache."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.side_effect = [
            ("ca-orange",),
            ({"D1": "Judge A"},),
        ]

        _get_roster_names(mock_conn, "court-clear-all")
        assert mock_cur.execute.call_count == 2

        # Clear the cache
        clear_roster_cache()

        # Re-populate mock for the next call
        mock_cur.fetchone.side_effect = [
            ("ca-orange",),
            ({"D1": "Judge A"},),
        ]

        # Should hit the DB again
        _get_roster_names(mock_conn, "court-clear-all")
        assert mock_cur.execute.call_count == 4

    def test_clear_roster_cache_single_court(self) -> None:
        """clear_roster_cache(court_id) clears only that entry."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.side_effect = [
            ("ca-orange",),
            ({"D1": "Judge A"},),
            ("ca-la",),
            ({"D1": "Judge B"},),
        ]

        _get_roster_names(mock_conn, "court-x")
        _get_roster_names(mock_conn, "court-y")
        assert mock_cur.execute.call_count == 4

        # Clear only court-x
        clear_roster_cache("court-x")

        mock_cur.fetchone.side_effect = [
            ("ca-orange",),
            ({"D1": "Judge A Updated"},),
        ]

        # court-x should re-query
        result_x = _get_roster_names(mock_conn, "court-x")
        assert result_x == ["Judge A Updated"]
        assert mock_cur.execute.call_count == 6

        # court-y should still be cached
        result_y = _get_roster_names(mock_conn, "court-y")
        assert result_y == ["Judge B"]
        assert mock_cur.execute.call_count == 6  # unchanged

    def test_cache_expires_after_ttl(self) -> None:
        """Cached values are refreshed after the TTL expires."""
        import time

        from ingestion.db import _roster_cache

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_cur.fetchone.side_effect = [
            ("ca-orange",),
            ({"D1": "Judge Old"},),
        ]

        result1 = _get_roster_names(mock_conn, "court-ttl")
        assert result1 == ["Judge Old"]
        assert mock_cur.execute.call_count == 2

        # Backdate the cache entry so it appears expired
        _roster_cache["court-ttl"] = (
            time.monotonic() - 400,  # 400 seconds ago, well past 300s TTL
            ["Judge Old"],
        )

        mock_cur.fetchone.side_effect = [
            ("ca-orange",),
            ({"D1": "Judge New"},),
        ]

        result2 = _get_roster_names(mock_conn, "court-ttl")
        assert result2 == ["Judge New"]
        assert mock_cur.execute.call_count == 4

    def test_empty_result_is_cached(self) -> None:
        """Empty results (no court or no snapshot) are also cached."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # No court found
        mock_cur.fetchone.return_value = None

        result1 = _get_roster_names(mock_conn, "court-empty")
        assert result1 == []
        assert mock_cur.execute.call_count == 1

        # Second call should use cache
        result2 = _get_roster_names(mock_conn, "court-empty")
        assert result2 == []
        assert mock_cur.execute.call_count == 1  # unchanged


# ---------------------------------------------------------------------------
# resolve_judge — roster integration
# ---------------------------------------------------------------------------


class TestResolveJudgeRosterIntegration:
    """Tests that resolve_judge uses the roster to prevent duplicates."""

    def _make_mock_conn(self) -> tuple[MagicMock, MagicMock]:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn, mock_cur

    def test_roster_match_existing_judge(self) -> None:
        """When a roster match finds an existing judge, returns that judge_id."""
        mock_conn, mock_cur = self._make_mock_conn()

        # Step 1: No alias match
        # Step 2: No exact canonical match
        # Step 3: No near-duplicate (fetchall returns empty)
        # Step 3b (roster): court_code lookup, snapshot, then judge exists
        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            # Step 3b: _get_roster_names
            ("ca-orange",),  # court_code
            ({"D1": "Jennifer Mccartney"},),  # snapshot
            # Step 3b: check if roster-matched judge exists
            ("existing-judge-uuid",),  # YES - judge with roster name exists
        ]
        mock_cur.fetchall.return_value = []  # Step 3: no near-duplicates

        result = resolve_judge(mock_conn, "Jennifer Mccarthey", "court-uuid-1")

        assert result == "existing-judge-uuid"
        # Should have inserted an alias with source='roster_match'
        all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
        assert "roster_match" in all_sql

    def test_roster_match_no_existing_judge_uses_incoming_canonical(self) -> None:
        """When roster match found but no existing judge, incoming spelling wins (#3858)."""
        mock_conn, mock_cur = self._make_mock_conn()

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            # Step 3b: _get_roster_names
            ("ca-orange",),  # court_code
            ({"D1": "Jennifer Mccartney"},),  # snapshot
            # Step 3b: check if roster-matched judge exists
            None,  # NO - no existing judge with roster name
            # Step 4: INSERT new judge returns id
            ("new-judge-uuid",),
        ]
        mock_cur.fetchall.return_value = []  # Step 3: no near-duplicates

        result = resolve_judge(mock_conn, "Jennifer Mccarthey", "court-uuid-1")

        assert result == "new-judge-uuid"
        # INSERT INTO judges must use incoming canonical, not the roster typo
        insert_calls = [
            c for c in mock_cur.execute.call_args_list if "INSERT INTO judges" in str(c)
        ]
        assert len(insert_calls) == 1
        insert_sql = str(insert_calls[0])
        assert "Jennifer Mccarthey" in insert_sql
        assert "Jennifer Mccartney" not in insert_sql
        # Roster typo stored as roster_match alias, not as canonical
        all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
        assert "roster_match" in all_sql
        assert "Jennifer Mccartney" in all_sql

    def test_no_roster_falls_through_to_create(self) -> None:
        """When no roster data exists, falls through to creating a new judge."""
        mock_conn, mock_cur = self._make_mock_conn()

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            # Step 3b: _get_roster_names (no court_code found)
            None,  # no court
            # Step 4: INSERT new judge
            ("new-judge-uuid",),
        ]
        mock_cur.fetchall.return_value = []  # Step 3: no near-duplicates

        result = resolve_judge(mock_conn, "John Smith", "court-uuid-1")

        assert result == "new-judge-uuid"

    def test_existing_alias_short_circuits_roster(self) -> None:
        """When an alias already exists, roster is never consulted."""
        mock_conn, mock_cur = self._make_mock_conn()

        # Step 1: alias found — returns (judge_id, canonical_name) now (#3503)
        mock_cur.fetchone.return_value = ("existing-judge-uuid", "John Smith")

        result = resolve_judge(mock_conn, "John Smith", "court-uuid-1")

        assert result == "existing-judge-uuid"
        # Should only have executed one query (the alias lookup)
        assert mock_cur.execute.call_count == 1

    def test_low_confidence_roster_match_skipped(self) -> None:
        """Low-confidence roster matches fall through to creating a new judge.

        Prevents merging distinct judges with similar names, e.g.
        "Jane Smythe" should not be merged with "Jane Smith" if the
        confidence is below the threshold.
        """
        mock_conn, mock_cur = self._make_mock_conn()

        # The roster has "Robert Smith" which would be a low-confidence
        # match for "Robert Jones" (very different last names).
        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            # Step 3b: _get_roster_names
            ("ca-orange",),  # court_code
            ({"D1": "Robert Smith"},),  # snapshot
            # Roster match confidence will be low (different names),
            # so it falls through to Step 4
            ("new-judge-uuid",),  # Step 4: INSERT new judge
        ]
        mock_cur.fetchall.return_value = []  # Step 3: no near-duplicates

        result = resolve_judge(mock_conn, "Robert Jones", "court-uuid-1")

        assert result == "new-judge-uuid"
        # Should NOT have a roster_match alias inserted
        all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
        assert "roster_match" not in all_sql


# ---------------------------------------------------------------------------
# dedup_judges script logic
# ---------------------------------------------------------------------------


class TestDedupJudgesDetection:
    """Tests for the dedup detection logic."""

    def test_detects_duplicate_pair(self) -> None:
        """Two judges matching the same roster entry are detected as duplicates."""
        # Import here to avoid module-level import issues
        import sys

        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

        # We need to test the detection logic from the script
        # Since it imports from ingestion.db, we can test fuzzy_match_roster_name
        roster = ["Jennifer Mccartney"]

        match_a = fuzzy_match_roster_name("Jennifer Mccartney", roster)
        match_b = fuzzy_match_roster_name("Jennifer Mccarthey", roster)

        assert match_a is not None
        assert match_b is not None
        # Both should match the same roster name
        assert match_a[0] == match_b[0] == "Jennifer Mccartney"

    def test_initial_placement_detected(self) -> None:
        """Initial placement variants map to the same roster entry."""
        roster = ["Shaina H. Colover"]

        match_a = fuzzy_match_roster_name("H. Shaina Colover", roster)
        match_b = fuzzy_match_roster_name("Shaina H. Colover", roster)

        assert match_a is not None
        assert match_b is not None
        assert match_a[0] == match_b[0]

    def test_partial_name_detected(self) -> None:
        """Partial names match their full roster counterpart."""
        roster = ["Andre De La Cruz"]

        match_full = fuzzy_match_roster_name("Andre De La Cruz", roster)
        match_partial = fuzzy_match_roster_name("De La Cruz", roster)

        assert match_full is not None
        assert match_partial is not None
        assert match_full[0] == match_partial[0]

    def test_no_false_positive_different_judges(self) -> None:
        """Different judges should not be detected as duplicates."""
        roster = ["John Smith", "Jane Doe"]

        match_a = fuzzy_match_roster_name("John Smith", roster)
        match_b = fuzzy_match_roster_name("Jane Doe", roster)

        assert match_a is not None
        assert match_b is not None
        assert match_a[0] != match_b[0]


# ---------------------------------------------------------------------------
# normalize_judge_name — edge cases relevant to dedup
# ---------------------------------------------------------------------------


class TestNormalizeJudgeNameDedup:
    """Tests for normalize_judge_name edge cases relevant to dedup."""

    def test_last_first_format(self) -> None:
        """Last, First format is normalized correctly."""
        assert normalize_judge_name("Smith, John") == "John Smith"

    def test_honorific_stripped(self) -> None:
        assert normalize_judge_name("Hon. John Smith") == "John Smith"

    def test_title_case(self) -> None:
        assert normalize_judge_name("JOHN SMITH") == "John Smith"

    def test_multi_part_last_name(self) -> None:
        """Multi-part names like De La Cruz are handled."""
        result = normalize_judge_name("Andre De La Cruz")
        assert result == "Andre De La Cruz"

    def test_initial_with_period(self) -> None:
        result = normalize_judge_name("John A. Smith")
        assert result == "John A. Smith"

    def test_normalizes_consistently(self) -> None:
        """The same input always produces the same output."""
        name = "Jennifer Mccartney"
        r1 = normalize_judge_name(name)
        r2 = normalize_judge_name(name)
        assert r1 == r2
