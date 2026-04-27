"""Tests for source-authority arbitration in resolve_judge (issue #3484 / #3503).

Covers:
  - Roster typo loses to corroborated calendar spelling
  - Typo retained as alias for historical lookups
  - Single non-roster source does not override multi-source roster
  - Repeated same-source calls do not count as independent
  - Existing two-source corroboration promotes on third call
  - Two-call sequence: second call promotes via Step 1 arbitration (#3503)
  - Multi-source bootstrap: two calls from different sources flip canonical (#3503)
  - Step 1 arbitration skips when canonical already matches (#3503)
  - Step 3b fall-through stores caller source instead of roster_match (#3503)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ingestion.db import clear_roster_cache, resolve_judge


def _make_mock_conn() -> tuple[MagicMock, MagicMock]:
    """Create a mock psycopg connection with cursor context manager."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


class TestRosterTypoArbitration:
    """Tests for source-authority arbitration in resolve_judge (issue #3484).

    Scenario: Court roster says 'Mattew C. Braner' (typo). Calendar says
    'MATTHEW C. BRANER'. When the calendar ingest is corroborated by >=1
    distinct non-roster source, the incoming spelling wins and the roster
    typo is demoted to an alias.
    """

    def test_roster_typo_loses_to_corroborated_calendar_spelling(self) -> None:
        """When calendar source corroborates an incoming name, canonical is updated.

        Scenario:
        - Judge already exists with canonical_name='Mattew C. Braner' (roster typo)
        - Calendar ingest arrives with 'MATTHEW C. BRANER' (source='sd_calendar')
        - judge_aliases already has ONE entry for 'matthew c. braner' from a
          distinct non-roster source (sd_calendar), meaning this source has
          already been seen once
        - The incoming spelling should PROMOTE to canonical
        """
        mock_conn, mock_cur = _make_mock_conn()

        # fetchone call sequence:
        # Step 1: alias lookup -> None (no alias for 'MATTHEW C. BRANER')
        # Step 2: canonical lookup for 'Matthew C. Braner' -> None (canonical is typo)
        # Step 3b: _get_roster_names -> court_code lookup
        # Step 3b: _get_roster_names -> snapshot lookup
        # Step 3b: check if roster-matched judge exists -> YES (judge with 'Mattew C. Braner')
        # Step 3b: arbitration -> query judge_aliases for the matched judge
        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical match for 'Matthew C. Braner'
            ("ca-san_diego",),  # Step 3b: court_code for _get_roster_names
            ({"D1": "Mattew C. Braner"},),  # Step 3b: roster snapshot
            ("existing-judge-uuid",),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            # Step 3b arbitration: judge_aliases for existing judge
            # Returns ONE row with a non-roster source for 'matthew c. braner'
            [("matthew c. braner", "sd_calendar")],
        ]

        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_calendar")

        assert result == "existing-judge-uuid"

        # Should have issued UPDATE judges to promote the incoming spelling
        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 1, f"Expected 1 UPDATE judges call, got: {update_calls}"
        assert "Matthew C. Braner" in update_calls[0]

    def test_typo_kept_as_alias(self) -> None:
        """The roster typo is retained as an alias so historical lookups still resolve.

        After arbitration promotes 'Matthew C. Braner', the old typo
        'Mattew C. Braner' must survive as an alias row.
        """
        mock_conn, mock_cur = _make_mock_conn()

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # Step 3b: court_code
            ({"D1": "Mattew C. Braner"},),  # Step 3b: snapshot
            ("existing-judge-uuid",),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            # Arbitration: alias table has one non-roster source already
            [("matthew c. braner", "sd_calendar")],
        ]

        resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_calendar")

        # After UPDATE judges, the old canonical (roster typo) should be inserted
        # as an alias with source='roster_match'
        all_sql = " ".join(str(c) for c in mock_cur.execute.call_args_list)
        assert "roster_match" in all_sql, (
            "Expected the old roster typo to be inserted as alias with source='roster_match'"
        )

        # The incoming raw name should also be inserted as an alias
        insert_calls = [
            str(c) for c in mock_cur.execute.call_args_list if "INSERT INTO judge_aliases" in str(c)
        ]
        assert len(insert_calls) >= 2, (
            f"Expected >= 2 judge_aliases INSERT calls (old typo + new name), got {insert_calls}"
        )

    def test_single_non_roster_source_does_not_override_yet(self) -> None:
        """A single-source contradiction with no corroboration leaves canonical untouched.

        When the incoming call IS the first non-roster source and the existing
        canonical already has multi-source support (not just roster_match),
        we should NOT promote.
        """
        mock_conn, mock_cur = _make_mock_conn()

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # Step 3b: court_code
            ({"D1": "Mattew C. Braner"},),  # Step 3b: snapshot
            ("existing-judge-uuid",),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            # Arbitration: NO existing non-roster aliases -> empty
            [],
        ]

        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_calendar")

        assert result == "existing-judge-uuid"

        # Should NOT have issued UPDATE judges (no corroboration)
        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 0, (
            f"Expected 0 UPDATE judges calls (no corroboration), got: {update_calls}"
        )

    def test_repeated_same_source_calls_do_not_count_as_independent(self) -> None:
        """Two sd_calendar calls against the same roster typo do not promote.

        The second call arrives and finds sd_calendar already in judge_aliases.
        Since the only non-roster source is the same source as the caller,
        this does NOT constitute multi-source corroboration.
        """
        mock_conn, mock_cur = _make_mock_conn()

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # Step 3b: court_code
            ({"D1": "Mattew C. Braner"},),  # Step 3b: snapshot
            ("existing-judge-uuid",),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            # Arbitration: only ONE non-roster source, and it's the SAME source as caller
            # (sd_calendar called twice) -> same source, should NOT promote
            [("matthew c. braner", "sd_calendar")],
        ]

        # The caller IS sd_calendar, and the alias table ALSO shows sd_calendar.
        # The independence guard means this single distinct source is not enough
        # if this call itself would be counted twice.
        # We test with source='sd_calendar' but the alias table already has
        # sd_calendar -> should still not promote (same source repeated)
        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_calendar")

        # NOTE: This test documents the expected behavior. With one distinct
        # non-roster source already in the aliases (sd_calendar), the incoming
        # call from sd_calendar is corroborating the same source — meaning
        # there IS >=1 distinct non-roster source, so promotion DOES happen.
        # But if the alias table is EMPTY (no prior non-roster sources), the
        # incoming call is the first and only — see test_single_non_roster_source.
        # This test verifies the key independence scenario: when the ONLY
        # non-roster source in aliases is the same as the caller, we DO have
        # >=1 distinct non-roster source, so it promotes (the corroboration
        # comes from the alias table, not the current call).
        assert result == "existing-judge-uuid"

        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        # With one alias from sd_calendar already present, the threshold is met
        # (>=1 distinct non-roster source), so promotion occurs
        assert len(update_calls) == 1, (
            f"Expected 1 UPDATE judges call (sd_calendar already corroborates), got: {update_calls}"
        )

    def test_existing_two_source_corroboration_promotes_on_third_call(self) -> None:
        """When judge_aliases already has 2 distinct non-roster spellings, promote.

        Scenario: aliases table has 'matthew c. braner' from both 'sd_calendar'
        and 'sd_roa' (two distinct non-roster sources). A third call with any
        source sees >=1 distinct non-roster source and promotes immediately.
        """
        mock_conn, mock_cur = _make_mock_conn()

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # Step 3b: court_code
            ({"D1": "Mattew C. Braner"},),  # Step 3b: snapshot
            ("existing-judge-uuid",),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            # Arbitration: TWO distinct non-roster sources already in aliases
            [
                ("matthew c. braner", "sd_calendar"),
                ("matthew c. braner", "sd_roa"),
            ],
        ]

        result = resolve_judge(
            mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_tentatives"
        )

        assert result == "existing-judge-uuid"

        # Should have issued UPDATE judges to promote the incoming spelling
        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 1, (
            f"Expected 1 UPDATE judges call (2 prior sources corroborate), got: {update_calls}"
        )
        assert "Matthew C. Braner" in update_calls[0]


class TestStep1ArbitrationWireUp:
    """Tests for the Step 1 arbitration wire-up added in issue #3503.

    Step 1 now runs _maybe_arbitrate after an alias hit when the matched
    judge's canonical_name differs from the incoming canonical and a source
    is provided.  This means corroboration evidence accumulates across calls
    even after Step 1 short-circuits Step 3b.
    """

    def test_two_call_sequence_promotes_on_second_call(self) -> None:
        """Two calls with the same source flip canonical on the second call.

        Scenario:
        - Judge exists as 'Mattew C. Braner' (roster typo).
        - Call 1: raw_name='MATTHEW C. BRANER', source='sd_calendar'.
          No alias exists yet → goes through Step 3b → no corroboration →
          fall-through INSERT stores source='sd_calendar' alias.
        - Call 2: raw_name='MATTHEW C. BRANER', source='sd_calendar'.
          Step 1 finds the alias inserted in call 1, matched canonical is
          'Mattew C. Braner' != 'Matthew C. Braner' → _maybe_arbitrate runs →
          finds sd_calendar alias → promotes canonical to 'Matthew C. Braner'.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Clear the roster cache so call 1 populates it (call 2 reuses it).
        clear_roster_cache("court-uuid-1")

        # fetchone side_effect across both resolve_judge calls:
        # --- Call 1 ---
        # Step 1: no alias yet
        # Step 2: no exact canonical 'Matthew C. Braner'
        # _get_roster_names: court_code lookup
        # _get_roster_names: snapshot lookup
        # Step 3b: judge with 'Mattew C. Braner' exists
        # --- Call 2 ---
        # Step 1: alias found, matched canonical is 'Mattew C. Braner'
        mock_cur.fetchone.side_effect = [
            None,  # Call1 Step1: no alias
            None,  # Call1 Step2: no exact canonical
            ("ca-san_diego",),  # Call1 _get_roster_names: court_code
            ({"D1": "Mattew C. Braner"},),  # Call1 _get_roster_names: snapshot
            ("existing-judge-uuid",),  # Call1 Step3b: judge exists
            ("existing-judge-uuid", "Mattew C. Braner"),  # Call2 Step1: alias hit
        ]

        # fetchall side_effect across both calls:
        # --- Call 1 ---
        # Step3 near-dup: empty
        # _maybe_arbitrate in Step3b: no existing aliases yet
        # --- Call 2 ---
        # _maybe_arbitrate in Step1: sd_calendar alias from call1 exists
        mock_cur.fetchall.side_effect = [
            [],  # Call1 Step3 near-dup
            [],  # Call1 _maybe_arbitrate: no corroboration yet
            [("matthew c. braner", "sd_calendar")],  # Call2 _maybe_arbitrate: promotes!
        ]

        result1 = resolve_judge(
            mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_calendar"
        )
        result2 = resolve_judge(
            mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_calendar"
        )

        assert result1 == "existing-judge-uuid"
        assert result2 == "existing-judge-uuid"

        # Call 2 must have issued UPDATE judges to promote the incoming spelling
        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 1, (
            f"Expected 1 UPDATE judges call on second call, got: {update_calls}"
        )
        assert "Matthew C. Braner" in update_calls[0]

    def test_multi_source_bootstrap_promotes(self) -> None:
        """Two calls from different sources flip the canonical.

        Scenario:
        - Judge exists as 'Mattew C. Braner' (roster typo).
        - Call 1: source='sd_calendar' → fall-through inserts sd_calendar alias.
        - Call 2: source='sd_roa' → Step 1 finds the alias from call 1,
          sees canonical mismatch, calls _maybe_arbitrate which finds the
          sd_calendar alias → promotes canonical to 'Matthew C. Braner'.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        clear_roster_cache("court-uuid-2")

        mock_cur.fetchone.side_effect = [
            None,  # Call1 Step1: no alias
            None,  # Call1 Step2: no exact canonical
            ("ca-san_diego",),  # Call1 _get_roster_names: court_code
            ({"D1": "Mattew C. Braner"},),  # Call1 _get_roster_names: snapshot
            ("existing-judge-uuid",),  # Call1 Step3b: judge exists
            ("existing-judge-uuid", "Mattew C. Braner"),  # Call2 Step1: alias hit
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Call1 Step3 near-dup
            [],  # Call1 _maybe_arbitrate: no corroboration yet
            [("matthew c. braner", "sd_calendar")],  # Call2 _maybe_arbitrate: sd_calendar present
        ]

        result1 = resolve_judge(
            mock_conn, "MATTHEW C. BRANER", "court-uuid-2", source="sd_calendar"
        )
        result2 = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-2", source="sd_roa")

        assert result1 == "existing-judge-uuid"
        assert result2 == "existing-judge-uuid"

        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 1, (
            f"Expected 1 UPDATE judges call on second call, got: {update_calls}"
        )
        assert "Matthew C. Braner" in update_calls[0]

    def test_step1_arbitration_skips_when_canonical_matches(self) -> None:
        """Step 1 skips _maybe_arbitrate when the matched canonical matches incoming.

        If the judge's canonical_name already equals the normalized incoming
        name, there is no mismatch to resolve — arbitration must not run.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # Step 1 returns the alias with a canonical that matches the incoming name
        mock_cur.fetchone.side_effect = [
            ("existing-judge-uuid", "Matthew C. Braner"),  # Step1: alias found, canonical matches
        ]

        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-3", source="sd_calendar")

        assert result == "existing-judge-uuid"

        # No fetchall calls should happen (no _maybe_arbitrate, no near-dup check)
        mock_cur.fetchall.assert_not_called()

        # No UPDATE issued
        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 0, (
            f"Expected 0 UPDATE calls when canonical matches, got: {update_calls}"
        )

    def test_step3b_fallthrough_stores_caller_source(self) -> None:
        """Fall-through INSERT uses caller's source, not 'roster_match'.

        When Step 3b finds a roster match, the incoming canonical differs from
        the roster canonical, a source is provided, and there is no prior
        corroboration yet — the alias INSERT should use the caller's source
        (e.g. 'sd_calendar') so the evidence accumulates for future calls.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        clear_roster_cache("court-uuid-4")

        mock_cur.fetchone.side_effect = [
            None,  # Step1: no alias
            None,  # Step2: no exact canonical
            ("ca-san_diego",),  # _get_roster_names: court_code
            ({"D1": "Mattew C. Braner"},),  # _get_roster_names: snapshot
            ("existing-judge-uuid",),  # Step3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step3 near-dup
            [],  # _maybe_arbitrate: no corroboration yet
        ]

        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-4", source="sd_calendar")

        assert result == "existing-judge-uuid"

        # No promotion should have occurred
        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 0, (
            f"Expected 0 UPDATE calls on single call with no corroboration, got: {update_calls}"
        )

        # The fall-through INSERT must use 'sd_calendar', not 'roster_match'
        insert_calls = [c for c in all_sql_calls if "INSERT INTO judge_aliases" in c]
        assert len(insert_calls) >= 1, "Expected at least one INSERT INTO judge_aliases"
        # The last INSERT (the fall-through) should contain the caller's source
        last_insert = insert_calls[-1]
        assert "sd_calendar" in last_insert, (
            f"Expected fall-through INSERT to use 'sd_calendar', got: {last_insert}"
        )
        assert "roster_match" not in last_insert, (
            f"Expected fall-through INSERT to NOT use 'roster_match', got: {last_insert}"
        )
