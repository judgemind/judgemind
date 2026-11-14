"""Tests for source-authority arbitration in resolve_judge (#3484 / #3503 / #4922).

Covers:
  - Roster typo loses to corroborated calendar spelling
  - Typo retained as alias for historical lookups
  - A better-corroborated canonical is not overridden (#4922)
  - Step 1 arbitration promotes a better-corroborated spelling (#3503)
  - Step 1 arbitration skips when canonical already matches (#3503)
  - Step 3b fall-through stores caller source instead of roster_match (#3503)
  - Spelling choice: family document counts with a margin, then "First M.
    Last" completeness among well-supported spellings (#4928)
  - Real Postgres: the same observations in any order settle on one name
    with at most one rename (#4922)
  - Real Postgres: evidence is counted per document; one document never
    renames a judge, a real majority renames it once (#4928)
  - Real Postgres: Step 3b finds the roster judge through its aliases after
    the judge was renamed away from the roster spelling (#4926)
"""

from __future__ import annotations

import os
import random
import uuid
from unittest.mock import MagicMock

import pytest

from ingestion.db import (
    _choose_canonical_spelling,
    _decisive,
    _spelling_completeness,
    clear_roster_cache,
    resolve_judge,
)

_DSN = os.environ.get("TEST_DATABASE_URL", "")
_needs_pg = pytest.mark.skipif(not _DSN, reason="TEST_DATABASE_URL not set")


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
            ("existing-judge-uuid", "Mattew C. Braner"),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            # Step 3b arbitration: judge_aliases for existing judge
            # Returns ONE row with a non-roster source for 'matthew c. braner'
            [("matthew c. braner", 2)],
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
            ("existing-judge-uuid", "Mattew C. Braner"),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            # Arbitration: alias table has one non-roster source already
            [("matthew c. braner", 2)],
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

    def test_arbitration_alias_inserts_match_partial_unique_index(self) -> None:
        """Arbitration-promotion judge_aliases INSERTs carry the partial-index predicate.

        Regression for #3855: migration 54 creates a PARTIAL unique index
        ``idx_judge_aliases_judge_raw_source_uniq`` on
        ``(judge_id, lower(raw_name), source) WHERE source IS NOT NULL``.
        PostgreSQL only accepts a partial index as an ``ON CONFLICT`` arbiter
        when the conflict clause repeats the index predicate. The
        arbitration-promotion INSERTs in ``_maybe_arbitrate`` previously used
        ``ON CONFLICT (judge_id, lower(raw_name), source) DO NOTHING`` with no
        ``WHERE source IS NOT NULL`` — which Postgres rejects at runtime with
        ``InvalidColumnReference: there is no unique or exclusion constraint
        matching the ON CONFLICT specification``, aborting the transaction. On
        the Orange OC multimodal reingest this aborted 50% of the PDFs
        mid-split, so the null judge/dept rulings never drained. Every
        judge_aliases INSERT that targets the partial index by column list must
        also carry the ``WHERE source IS NOT NULL`` predicate.
        """
        mock_conn, mock_cur = _make_mock_conn()

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # Step 3b: court_code
            ({"D1": "Mattew C. Braner"},),  # Step 3b: snapshot
            ("existing-judge-uuid", "Mattew C. Braner"),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            # Arbitration: alias table has one non-roster source already -> promote
            [("matthew c. braner", 2)],
        ]

        resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_calendar")

        # Collect the raw SQL of every judge_aliases INSERT the promotion issued.
        alias_insert_sql = [
            call.args[0]
            for call in mock_cur.execute.call_args_list
            if call.args and "INSERT INTO judge_aliases" in call.args[0]
        ]
        assert alias_insert_sql, "Expected at least one judge_aliases INSERT during promotion"

        for sql in alias_insert_sql:
            # Only INSERTs that name the partial index by its column list need
            # the predicate; those are the ones that hit the #3855 bug.
            if "ON CONFLICT (judge_id, lower(raw_name), source)" in sql:
                assert "source IS NOT NULL" in sql, (
                    "judge_aliases ON CONFLICT targeting the partial unique index "
                    "must repeat its 'WHERE source IS NOT NULL' predicate, else "
                    "Postgres raises InvalidColumnReference (regression #3855):\n" + sql
                )

    def test_arbitration_skips_name_held_by_another_judge(self) -> None:
        """Promotion into a canonical name another judge already holds is skipped (#4826).

        Dev: 'Donald Gaffney' (276 rulings) is corroborated as 'Donald F. Gaffney',
        but a second, empty judge row already has that canonical name.  The
        UPDATE is guarded by NOT EXISTS, matches no row, and arbitration stops
        instead of raising UniqueViolation and failing the whole document.
        """
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # Step 3b: court_code
            ({"D1": "Mattew C. Braner"},),  # Step 3b: snapshot
            ("existing-judge-uuid", "Mattew C. Braner"),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            [("matthew c. braner", 2)],  # corroborated
        ]
        mock_cur.rowcount = 0  # the guarded UPDATE found the name already held

        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_calendar")

        assert result == "existing-judge-uuid"
        sql = [str(c) for c in mock_cur.execute.call_args_list]
        update = next(c for c in sql if "UPDATE judges" in c)
        assert "NOT EXISTS" in update
        # No demotion alias for the roster name: promotion did not happen.
        assert not any("'roster_match'" in c and "INSERT INTO judge_aliases" in c for c in sql)

    def test_better_corroborated_canonical_is_not_overridden(self) -> None:
        """A challenger without a decisive document lead does not win (#4922, #4928).

        The stored canonical is printed on 2 documents, the incoming spelling
        on 3: the challenger needs 2 more and twice as many.  The canonical
        stays.
        """
        mock_conn, mock_cur = _make_mock_conn()

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # Step 3b: court_code
            ({"D1": "Mattew C. Braner"},),  # Step 3b: snapshot
            ("existing-judge-uuid", "Mattew C. Braner"),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            # Arbitration: the stored spelling is on 2 documents, the
            # incoming one on 3: not a decisive lead (#4928)
            [("Mattew C. Braner", 2), ("matthew c. braner", 3)],
        ]

        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_calendar")

        assert result == "existing-judge-uuid"

        # Should NOT have issued UPDATE judges (canonical better corroborated)
        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 0, (
            f"Expected 0 UPDATE judges calls (no corroboration), got: {update_calls}"
        )

    def test_one_document_does_not_rename(self) -> None:
        """One document printing a spelling never renames the judge (#4928).

        Before, one alias from one source was enough.  Evidence is now
        counted in documents, and one document is below the minimum.
        """
        mock_conn, mock_cur = _make_mock_conn()

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # Step 3b: court_code
            ({"D1": "Mattew C. Braner"},),  # Step 3b: snapshot
            ("existing-judge-uuid", "Mattew C. Braner"),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            [],  # Arbitration: no other document printed any spelling
        ]

        result = resolve_judge(
            mock_conn,
            "MATTHEW C. BRANER",
            "court-uuid-1",
            source="sd_calendar",
            document_id="11111111-1111-1111-1111-111111111111",
        )

        assert result == "existing-judge-uuid"
        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        assert not [c for c in all_sql_calls if "UPDATE judges" in c]
        # The document's vote is still recorded, for the next document.
        assert "INSERT INTO judge_name_evidence" in all_sql_calls[-1]

    def test_two_documents_promote(self) -> None:
        """Two documents printing a spelling beat a canonical no document printed.

        The counts of spellings that differ only in case add up: they are one
        spelling.
        """
        mock_conn, mock_cur = _make_mock_conn()

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # Step 3b: court_code
            ({"D1": "Mattew C. Braner"},),  # Step 3b: snapshot
            ("existing-judge-uuid", "Mattew C. Braner"),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            [
                ("matthew c. braner", 1),
                ("MATTHEW C. BRANER", 1),
            ],
        ]

        result = resolve_judge(
            mock_conn, "MATTHEW C. BRANER", "court-uuid-1", source="sd_tentatives"
        )

        assert result == "existing-judge-uuid"

        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 1, f"Expected 1 UPDATE judges call, got: {update_calls}"
        assert "Matthew C. Braner" in update_calls[0]


class TestStep1ArbitrationWireUp:
    """Tests for the Step 1 arbitration wire-up added in issue #3503.

    Step 1 runs _maybe_arbitrate after an alias hit whenever a source is
    provided, so document evidence keeps counting even after Step 1
    short-circuits Step 3b.
    """

    def test_roster_canonical_yields_on_the_second_document(self) -> None:
        """A document spelling beats a canonical no document printed, on two documents.

        Scenario (#4922, #4928):
        - Judge exists as 'Mattew C. Braner' (roster typo, no document evidence).
        - Document 1: 'MATTHEW C. BRANER' goes through Step 3b.  It is the only
          document evidence: one document never renames the judge.
        - Document 2: same spelling, Step 1 alias hit.  Document 1's vote plus
          this one make two, so the canonical becomes 'Matthew C. Braner'.
        """
        mock_conn, mock_cur = _make_mock_conn()
        clear_roster_cache("court-uuid-1")

        mock_cur.fetchone.side_effect = [
            None,  # Doc1 Step1: no alias
            None,  # Doc1 Step2: no exact canonical
            ("ca-san_diego",),  # Doc1 _get_roster_names: court_code
            ({"D1": "Mattew C. Braner"},),  # Doc1 _get_roster_names: snapshot
            ("existing-judge-uuid", "Mattew C. Braner"),  # Doc1 Step3b: judge exists
            ("existing-judge-uuid", "Mattew C. Braner"),  # Doc2 Step1: alias hit
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Doc1 Step3 near-dup
            [],  # Doc1 _maybe_arbitrate: no other document evidence
            [("MATTHEW C. BRANER", 1)],  # Doc2 _maybe_arbitrate: document 1's vote
        ]

        result1 = resolve_judge(
            mock_conn,
            "MATTHEW C. BRANER",
            "court-uuid-1",
            source="sd_calendar",
            document_id="11111111-1111-1111-1111-111111111111",
        )
        sql_after_doc1 = [str(c) for c in mock_cur.execute.call_args_list]
        result2 = resolve_judge(
            mock_conn,
            "MATTHEW C. BRANER",
            "court-uuid-1",
            source="sd_calendar",
            document_id="22222222-2222-2222-2222-222222222222",
        )

        assert result1 == "existing-judge-uuid"
        assert result2 == "existing-judge-uuid"
        assert not [c for c in sql_after_doc1 if "UPDATE judges" in c]

        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 1, f"Expected exactly 1 UPDATE judges, got: {update_calls}"
        assert "Matthew C. Braner" in update_calls[0]
        # Each document records its own vote.
        evidence = [c for c in all_sql_calls if "INSERT INTO judge_name_evidence" in c]
        assert len(evidence) == 2

    def test_step1_alias_hit_promotes_better_corroborated_spelling(self) -> None:
        """Step 1 runs arbitration when the alias's judge has another canonical (#3503).

        The judge is stored as 'Mattew C. Braner'; the alias for 'MATTHEW C.
        BRANER' exists and two documents printed that spelling, so it wins.
        """
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [
            ("existing-judge-uuid", "Mattew C. Braner"),  # Step1: alias hit
        ]
        mock_cur.fetchall.side_effect = [
            [("matthew c. braner", 2)],
        ]

        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-2", source="sd_roa")

        assert result == "existing-judge-uuid"
        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 1, f"Expected 1 UPDATE judges call, got: {update_calls}"
        assert "Matthew C. Braner" in update_calls[0]

    def test_step1_canonical_match_keeps_the_name(self) -> None:
        """A document printing the canonical spelling leaves the name alone."""
        mock_conn, mock_cur = _make_mock_conn()

        # Step 1 returns the alias with a canonical that matches the incoming name
        mock_cur.fetchone.side_effect = [
            ("existing-judge-uuid", "Matthew C. Braner"),  # Step1: alias found, canonical matches
        ]
        mock_cur.fetchall.side_effect = [[("Matthew C. Braner", 3), ("Mattew C. Braner", 1)]]

        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-3", source="sd_calendar")

        assert result == "existing-judge-uuid"

        # No UPDATE issued
        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]
        update_calls = [c for c in all_sql_calls if "UPDATE judges" in c]
        assert len(update_calls) == 0, (
            f"Expected 0 UPDATE calls when canonical matches, got: {update_calls}"
        )

    def test_without_source_no_arbitration(self) -> None:
        """A call without a source (the reingest judge prepass) never arbitrates."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [("existing-judge-uuid", "Mattew C. Braner")]

        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-3")

        assert result == "existing-judge-uuid"
        mock_cur.fetchall.assert_not_called()
        assert not [c for c in mock_cur.execute.call_args_list if "UPDATE judges" in str(c)]

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
            ("existing-judge-uuid", "Mattew C. Braner"),  # Step3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step3 near-dup
            # _maybe_arbitrate: the stored spelling has more documents
            [("Mattew C. Braner", 2)],
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


class TestStep1Deterministic:
    """Tests that Step-1 judge alias lookup is deterministic (issue #3539).

    The Step-1 SELECT must carry an explicit ORDER BY clause so that when
    multiple judge_aliases rows match the same raw_name + court_id, the
    same row is always chosen.  Without ORDER BY the database may return
    any matching row, producing a different judge_id on successive calls.
    """

    def test_step1_deterministic_order_by_present(self) -> None:
        """Step-1 SQL contains ORDER BY ja.created_at ASC, ja.judge_id ASC before LIMIT 1.

        Structural assertion: inspect the SQL string emitted by the first
        cur.execute call and verify:
          (a) it queries judge_aliases (ja),
          (b) it contains an ORDER BY referencing both created_at and judge_id,
          (c) ORDER BY appears before LIMIT 1 in the SQL text.
        """
        mock_conn, mock_cur = _make_mock_conn()

        # Step 1 returns a deterministic row immediately.
        mock_cur.fetchone.side_effect = [
            ("judge-uuid-1", "Matthew C. Braner"),
        ]

        result = resolve_judge(mock_conn, "MATTHEW C. BRANER", "court-uuid-10")

        assert result == "judge-uuid-1"

        # The very first execute call must be the Step-1 alias lookup.
        first_call = mock_cur.execute.call_args_list[0]
        sql: str = first_call[0][0]

        assert "judge_aliases ja" in sql, (
            f"Expected Step-1 SQL to reference 'judge_aliases ja', got:\n{sql}"
        )
        assert "ORDER BY" in sql, f"Expected Step-1 SQL to contain ORDER BY, got:\n{sql}"
        assert "created_at" in sql, f"Expected ORDER BY to reference 'created_at', got:\n{sql}"
        assert "judge_id" in sql, f"Expected ORDER BY to reference 'judge_id', got:\n{sql}"
        order_by_pos = sql.index("ORDER BY")
        limit_pos = sql.index("LIMIT 1")
        assert order_by_pos < limit_pos, (
            f"Expected ORDER BY to appear before LIMIT 1 in Step-1 SQL, got:\n{sql}"
        )

    def test_step1_two_calls_consistent(self) -> None:
        """Two resolve_judge calls with the same Step-1 hit return the same judge_id.

        Both calls also emit identical Step-1 SQL (same ORDER BY clause),
        confirming the determinism guarantee is present in every invocation.
        """
        mock_conn, mock_cur = _make_mock_conn()

        # Both calls resolve via Step 1.
        mock_cur.fetchone.side_effect = [
            ("judge-uuid-42", "John A. Smith"),  # call 1 Step 1
            ("judge-uuid-42", "John A. Smith"),  # call 2 Step 1
        ]

        result1 = resolve_judge(mock_conn, "JOHN A. SMITH", "court-uuid-11")
        result2 = resolve_judge(mock_conn, "JOHN A. SMITH", "court-uuid-11")

        assert result1 == "judge-uuid-42", f"Call 1 returned unexpected judge_id: {result1}"
        assert result2 == "judge-uuid-42", f"Call 2 returned unexpected judge_id: {result2}"
        assert result1 == result2, "Both calls must return the same judge_id"

        # Both execute calls should be Step-1 alias lookups with identical SQL.
        assert mock_cur.execute.call_count >= 2, (
            f"Expected at least 2 execute calls, got {mock_cur.execute.call_count}"
        )
        sql1: str = mock_cur.execute.call_args_list[0][0][0]
        sql2: str = mock_cur.execute.call_args_list[1][0][0]

        assert sql1 == sql2, f"Step-1 SQL differed between calls:\nCall 1: {sql1}\nCall 2: {sql2}"
        assert "ORDER BY" in sql1, f"Expected Step-1 SQL to contain ORDER BY, got:\n{sql1}"


class TestStep3bBootstrapPrefersIncomingSpelling:
    """Tests for Step 3b bootstrap behavior: incoming spelling wins over roster (#3858).

    When no existing judge is found with the roster name, the judge should be
    created with the LLM-extracted (incoming) spelling as canonical, not the
    roster name. The roster name (if it differs by case-insensitive comparison)
    is stored only as an alias for future lookups.
    """

    def test_no_existing_judge_uses_incoming_not_roster_canonical(self) -> None:
        """When no judge exists with roster name, canonical uses incoming not roster.

        Scenario:
        - Court roster has 'Mattew C. Braner' (typo).
        - Incoming raw name is 'MATTHEW C. BRANER' (source='sd_calendar').
        - No existing judge found at any step.
        - Assert: INSERT INTO judges uses 'Matthew C. Braner' (normalized incoming),
          NOT 'Mattew C. Braner' (roster typo).
        - Assert: a roster_match alias IS inserted for 'Mattew C. Braner'.
        """
        mock_conn, mock_cur = _make_mock_conn()

        clear_roster_cache("court-uuid-bootstrap-1")

        # fetchone sequence:
        # Step 1: no alias for 'MATTHEW C. BRANER'
        # Step 2: no exact canonical 'Matthew C. Braner'
        # _get_roster_names: court_code lookup
        # _get_roster_names: snapshot lookup
        # Step 3b: no judge with 'Mattew C. Braner' (the roster name)
        # Step 4: INSERT INTO judges RETURNING id
        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # _get_roster_names: court_code
            ({"D1": "Mattew C. Braner"},),  # _get_roster_names: snapshot
            None,  # Step 3b: no judge with roster name exists
            ("new-judge-uuid",),  # Step 4: INSERT INTO judges RETURNING id
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
        ]

        result = resolve_judge(
            mock_conn, "MATTHEW C. BRANER", "court-uuid-bootstrap-1", source="sd_calendar"
        )

        assert result == "new-judge-uuid"

        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]

        # The INSERT INTO judges must use the incoming spelling, not the roster typo
        insert_judge_calls = [c for c in all_sql_calls if "INSERT INTO judges" in c]
        assert len(insert_judge_calls) == 1, (
            f"Expected 1 INSERT INTO judges call, got: {insert_judge_calls}"
        )
        assert "Matthew C. Braner" in insert_judge_calls[0], (
            f"Expected INSERT to use incoming 'Matthew C. Braner', got: {insert_judge_calls[0]}"
        )
        assert "Mattew C. Braner" not in insert_judge_calls[0], (
            "Expected INSERT to NOT use roster typo 'Mattew C. Braner', "
            f"got: {insert_judge_calls[0]}"
        )

        # A roster_match alias must be inserted for the roster typo
        alias_inserts = [c for c in all_sql_calls if "INSERT INTO judge_aliases" in c]
        assert any("roster_match" in c for c in alias_inserts), (
            f"Expected a roster_match alias INSERT for 'Mattew C. Braner', got: {alias_inserts}"
        )
        assert any("Mattew C. Braner" in c for c in alias_inserts), (
            f"Expected alias INSERT containing 'Mattew C. Braner', got: {alias_inserts}"
        )

    def test_no_existing_judge_no_roster_alias_when_incoming_equals_roster(self) -> None:
        """When incoming and roster names only differ by case, no second alias is inserted.

        If normalize_judge_name(roster) == canonical (case-insensitive), the
        roster name is not meaningfully different — no extra alias INSERT.
        """
        mock_conn, mock_cur = _make_mock_conn()

        clear_roster_cache("court-uuid-bootstrap-2")

        # Roster name matches incoming (same letters, different case only)
        # roster='Matthew C. Braner', incoming='MATTHEW C. BRANER' ->
        # both normalize to 'Matthew C. Braner'
        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # _get_roster_names: court_code
            ({"D1": "Matthew C. Braner"},),  # _get_roster_names: snapshot (correct spelling)
            None,  # Step 3b: no judge with roster name exists
            ("new-judge-uuid-2",),  # Step 4: INSERT INTO judges RETURNING id
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
        ]

        result = resolve_judge(
            mock_conn, "MATTHEW C. BRANER", "court-uuid-bootstrap-2", source="sd_calendar"
        )

        assert result == "new-judge-uuid-2"

        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]

        # The INSERT INTO judges must use 'Matthew C. Braner'
        insert_judge_calls = [c for c in all_sql_calls if "INSERT INTO judges" in c]
        assert len(insert_judge_calls) == 1
        assert "Matthew C. Braner" in insert_judge_calls[0]

        # No roster_match alias should be inserted (canonical == roster_normalized)
        alias_inserts = [c for c in all_sql_calls if "INSERT INTO judge_aliases" in c]
        roster_match_inserts = [c for c in alias_inserts if "roster_match" in c]
        assert len(roster_match_inserts) == 0, (
            f"Expected no roster_match alias when incoming==roster (case-insensitive), "
            f"got: {roster_match_inserts}"
        )

    def test_no_existing_judge_no_source_still_creates_with_incoming_canonical(self) -> None:
        """With source=None, incoming spelling still wins as canonical.

        When no source is provided, the fix still applies: the judge is created
        with the LLM-extracted (incoming) spelling, not the roster name.
        """
        mock_conn, mock_cur = _make_mock_conn()

        clear_roster_cache("court-uuid-bootstrap-3")

        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # _get_roster_names: court_code
            ({"D1": "Mattew C. Braner"},),  # _get_roster_names: snapshot (typo)
            None,  # Step 3b: no judge with roster name exists
            ("new-judge-uuid-3",),  # Step 4: INSERT INTO judges RETURNING id
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
        ]

        result = resolve_judge(
            mock_conn, "MATTHEW C. BRANER", "court-uuid-bootstrap-3", source=None
        )

        assert result == "new-judge-uuid-3"

        all_sql_calls = [str(c) for c in mock_cur.execute.call_args_list]

        # The INSERT INTO judges must use the incoming spelling
        insert_judge_calls = [c for c in all_sql_calls if "INSERT INTO judges" in c]
        assert len(insert_judge_calls) == 1
        assert "Matthew C. Braner" in insert_judge_calls[0], (
            f"Expected INSERT to use incoming 'Matthew C. Braner', got: {insert_judge_calls[0]}"
        )
        assert "Mattew C. Braner" not in insert_judge_calls[0], (
            "Expected INSERT to NOT use roster typo 'Mattew C. Braner', "
            f"got: {insert_judge_calls[0]}"
        )


class TestRenamedJudgeIds:
    """``resolve_judge`` reports the judges it renamed (#4920).

    A rename changes the search ``judge_name`` of every ruling of that
    judge, so the caller re-indexes them after its commit.
    """

    def test_near_duplicate_rename_is_reported(self) -> None:
        """A fuller near-duplicate printed on two documents takes the name."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [None, None]  # Step 1, Step 2: no match
        mock_cur.fetchall.side_effect = [
            [("judge-sj", "Rafael Sivilla-Jones")],  # Step 3: near-duplicate
            [("Rafael A. Sivilla-Jones", 2)],  # arbitration evidence
        ]
        mock_cur.rowcount = 1
        renamed: set[str] = set()

        result = resolve_judge(
            mock_conn,
            "Rafael A. Sivilla-Jones",
            "court-4920",
            source="ca-sd",
            renamed_judge_ids=renamed,
        )

        assert result == "judge-sj"
        assert renamed == {"judge-sj"}

    def test_near_duplicate_on_one_document_does_not_rename(self) -> None:
        """One document with an extra initial no longer renames the judge (#4928)."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [None, None]  # Step 1, Step 2: no match
        mock_cur.fetchall.side_effect = [
            [("judge-sj", "Rafael Sivilla-Jones")],  # Step 3: near-duplicate
            [("Rafael Sivilla-Jones", 4)],  # arbitration: the current spelling's documents
        ]
        renamed: set[str] = set()

        result = resolve_judge(
            mock_conn,
            "Rafael A. Sivilla-Jones",
            "court-4920",
            source="ca-sd",
            renamed_judge_ids=renamed,
            document_id="33333333-3333-3333-3333-333333333333",
        )

        assert result == "judge-sj"
        assert renamed == set()
        assert not [c for c in mock_cur.execute.call_args_list if "UPDATE judges" in str(c)]

    def test_near_duplicate_without_rename_is_not_reported(self) -> None:
        """The stored name is already the longer one: no UPDATE, no report."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [None, None]
        mock_cur.fetchall.side_effect = [[("judge-sj", "Rafael A. Sivilla-Jones")]]
        renamed: set[str] = set()

        result = resolve_judge(
            mock_conn, "Rafael Sivilla-Jones", "court-4920", renamed_judge_ids=renamed
        )

        assert result == "judge-sj"
        assert renamed == set()

    def test_arbitration_promotion_is_reported(self) -> None:
        clear_roster_cache("court-4920-arb")
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [
            None,  # Step 1: no alias
            None,  # Step 2: no exact canonical
            ("ca-san_diego",),  # Step 3b: court_code
            ({"D1": "Mattew C. Braner"},),  # Step 3b: snapshot
            ("existing-judge-uuid", "Mattew C. Braner"),  # Step 3b: judge with roster name exists
        ]
        mock_cur.fetchall.side_effect = [
            [],  # Step 3: no near-duplicates
            [("matthew c. braner", 2)],  # corroborated
        ]
        mock_cur.rowcount = 1
        renamed: set[str] = set()

        resolve_judge(
            mock_conn,
            "MATTHEW C. BRANER",
            "court-4920-arb",
            source="sd_calendar",
            renamed_judge_ids=renamed,
        )

        assert renamed == {"existing-judge-uuid"}

    def test_skipped_arbitration_is_not_reported(self) -> None:
        clear_roster_cache("court-4920-skip")
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [
            None,
            None,
            ("ca-san_diego",),
            ({"D1": "Mattew C. Braner"},),
            ("existing-judge-uuid", "Mattew C. Braner"),
        ]
        mock_cur.fetchall.side_effect = [[], [("matthew c. braner", 2)]]
        mock_cur.rowcount = 0  # the name is held by another judge
        renamed: set[str] = set()

        resolve_judge(
            mock_conn,
            "MATTHEW C. BRANER",
            "court-4920-skip",
            source="sd_calendar",
            renamed_judge_ids=renamed,
        )

        assert renamed == set()

    def test_step1_arbitration_promotion_is_reported(self) -> None:
        mock_conn, mock_cur = _make_mock_conn()
        # Step 1 hit on a judge stored under a spelling no document printed.
        mock_cur.fetchone.side_effect = [("judge-sc", "H. Shaina Colover")]
        mock_cur.fetchall.side_effect = [[("shaina clover", 2)]]
        mock_cur.rowcount = 1
        renamed: set[str] = set()

        result = resolve_judge(
            mock_conn,
            "Shaina Clover",
            "court-4920-s1",
            source="reingest-ca-orange",
            renamed_judge_ids=renamed,
        )

        assert result == "judge-sc"
        assert renamed == {"judge-sc"}


class TestChooseCanonicalSpelling:
    """The spelling choice behind arbitration (#4922, #4928).

    Evidence is ``(raw spelling, documents)``: how many distinct documents
    printed each spelling for the judge.
    """

    def test_more_complete_spelling_wins_a_real_tie(self) -> None:
        evidence = [("Shaina Clover", 10), ("H. Shaina Clover", 10)]
        for current in ("Shaina Clover", "H. Shaina Clover"):
            winner, _ = _choose_canonical_spelling(current, evidence)
            assert winner == "H. Shaina Clover"

    def test_dotted_initial_wins_a_real_tie_over_bare_initial(self) -> None:
        evidence = [("Craig L Griffin", 3), ("Craig L. Griffin", 3)]
        for current in ("Craig L Griffin", "Craig L. Griffin"):
            assert _choose_canonical_spelling(current, evidence)[0] == "Craig L. Griffin"

    def test_first_m_last_preferred_over_leading_initial(self) -> None:
        """Same words, same initials, both well supported: the "First M. Last" form wins."""
        evidence = [("H. Shaina Clover", 2), ("Shaina H. Clover", 2)]
        assert _choose_canonical_spelling("H. Shaina Clover", evidence)[0] == "Shaina H. Clover"
        assert _spelling_completeness("Shaina H. Clover") > _spelling_completeness(
            "H. Shaina Clover"
        )

    def test_middle_initial_not_dropped_for_a_less_complete_spelling(self) -> None:
        """'Joseph Widman' no longer demotes 'Joseph B. Widman' (13:12Z on dev)."""
        assert _choose_canonical_spelling("Joseph B. Widman", [("Joseph Widman", 1)])[0] == (
            "Joseph B. Widman"
        )
        evidence = [("Joseph B. Widman", 5), ("Joseph Widman", 5)]
        assert _choose_canonical_spelling("Joseph B. Widman", evidence)[0] == "Joseph B. Widman"

    def test_better_supported_family_beats_more_complete_typo(self) -> None:
        evidence = [("H. Shaina Colover", 1), ("Shaina Clover", 3), ("H. Shaina Clover", 3)]
        assert _choose_canonical_spelling("H. Shaina Colover", evidence)[0] == "H. Shaina Clover"

    def test_case_variants_are_one_spelling(self) -> None:
        evidence = [("CRAIG L. GRIFFIN", 1), ("craig l. griffin", 1)]
        winner, docs = _choose_canonical_spelling("Craig L. Griffin", evidence)
        assert winner == "Craig L. Griffin"
        assert docs["craig l. griffin"] == 2

    def test_unusable_evidence_is_ignored(self) -> None:
        evidence = [("", 3), ("Craig Griffin", 0), ("Craig Griffen", -4), ("Bob", 5)]
        assert _choose_canonical_spelling("Craig L. Griffin", evidence)[0] == "Craig L. Griffin"

    # -- #4928: one document never renames a judge -------------------------

    def test_one_typo_document_loses_to_the_current_spelling(self) -> None:
        """The audit's examples: a tie used to fall to completeness or lexical order."""
        for typo in ("Craig L. Griffen", "Craig L. M. Griffin"):
            for current_docs in (1, 5, 250):
                evidence = [("Craig L. Griffin", current_docs), (typo, 1)]
                assert _choose_canonical_spelling("Craig L. Griffin", evidence)[0] == (
                    "Craig L. Griffin"
                ), (typo, current_docs)

    def test_hallucinated_initial_needs_a_real_tie(self) -> None:
        """An extra initial wins on completeness only when about as many documents print it."""
        assert _choose_canonical_spelling(
            "Craig L. Griffin", [("Craig L. Griffin", 250), ("Craig L. M. Griffin", 2)]
        )[0] == ("Craig L. Griffin")
        assert _choose_canonical_spelling(
            "Craig L. Griffin", [("Craig L. Griffin", 5), ("Craig L. M. Griffin", 4)]
        )[0] == ("Craig L. M. Griffin")

    def test_challenger_needs_a_decisive_lead(self) -> None:
        """Two more documents and twice as many."""
        current = "Craig L. Griffin"
        assert _choose_canonical_spelling(current, [(current, 5), ("Craig L. Griffen", 9)])[0] == (
            current
        )
        assert _choose_canonical_spelling(current, [(current, 5), ("Craig L. Griffen", 10)])[0] == (
            "Craig L. Griffen"
        )
        assert _choose_canonical_spelling(current, [(current, 1), ("Craig L. Griffen", 2)])[0] == (
            current
        )
        assert _choose_canonical_spelling(current, [(current, 1), ("Craig L. Griffen", 3)])[0] == (
            "Craig L. Griffen"
        )

    def test_roster_only_name_yields_to_two_documents(self) -> None:
        roster = "H. Shaina Colover"  # no document printed it
        assert _choose_canonical_spelling(roster, [("Shaina Clover", 1)])[0] == roster
        assert _choose_canonical_spelling(roster, [("Shaina Clover", 2)])[0] == "Shaina Clover"

    def test_two_close_families_keep_the_current_name(self) -> None:
        """No family leads decisively: nothing to rename to."""
        evidence = [("Shaina Clover", 5), ("H. Shaina Colover", 4)]
        assert _choose_canonical_spelling("Zed Q. Other", evidence)[0] == "Zed Q. Other"

    def test_thin_spellings_inside_the_current_family_keep_the_name(self) -> None:
        evidence = [("Shaina Clover", 1), ("H. Shaina Clover", 1)]
        assert _choose_canonical_spelling("Shaina Clover", evidence)[0] == "Shaina Clover"

    def test_thin_spellings_of_a_new_family_pick_the_most_printed(self) -> None:
        """Moving to a family whose spellings are thin: most printed, then fullest."""
        evidence = [("Shaina Clover", 1), ("H. Shaina Clover", 1)]
        assert _choose_canonical_spelling("H. Shaina Colover", evidence)[0] == "H. Shaina Clover"
        evidence = [("Shaina Clover", 2), ("H. Shaina Clover", 1)]
        assert _choose_canonical_spelling("H. Shaina Colover", evidence)[0] == "Shaina Clover"

    def test_decisive_rule(self) -> None:
        assert not _decisive(1, 0)
        assert _decisive(2, 0)
        assert not _decisive(3, 2)
        assert _decisive(4, 2)
        assert not _decisive(9, 5)
        assert _decisive(10, 5)


# ---------------------------------------------------------------------------
# Real Postgres: order independence (#4922) and document evidence (#4928)
# ---------------------------------------------------------------------------


class _PgCourt:
    """A fresh court with one judge, for resolve_judge against real Postgres.

    *evidence* seeds ``(spelling, documents)`` pairs: that many active
    documents, each with a ``judge_name_evidence`` row for the judge.
    """

    def __init__(
        self,
        canonical: str,
        aliases: list[tuple[str, str]],
        roster: dict[str, str] | None = None,
        evidence: list[tuple[str, int]] | None = None,
        evidence_status: str = "active",
    ) -> None:
        import json

        import psycopg

        self.conn = psycopg.connect(_DSN, autocommit=False)
        code = f"ca-arb4922-{uuid.uuid4().hex[:12]}"
        # The roster snapshot is keyed by the court code in snapshot form.
        self.snapshot_court = code.replace("-", "_")
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO courts (state, county, court_name, court_code) "
                "VALUES ('CA', 'Arb4922', 'Superior Court', %s) RETURNING id",
                (code,),
            )
            self.court_id = str(cur.fetchone()[0])
            if roster is not None:
                cur.execute(
                    "INSERT INTO court_directory_snapshots "
                    "(court_id, captured_at, s3_key, mapping, content_hash) "
                    "VALUES (%s, NOW(), %s, %s::jsonb, %s)",
                    (self.snapshot_court, f"test/{code}.json", json.dumps(roster), code),
                )
            cur.execute(
                "INSERT INTO judges (canonical_name, court_id) VALUES (%s, %s::uuid) RETURNING id",
                (canonical, self.court_id),
            )
            self.judge_id = str(cur.fetchone()[0])
            for raw, source in aliases:
                cur.execute(
                    "INSERT INTO judge_aliases (judge_id, raw_name, source, confidence) "
                    "VALUES (%s::uuid, %s, %s, 1.0)",
                    (self.judge_id, raw, source),
                )
        for raw, count in evidence or []:
            for _ in range(count):
                document_id = str(uuid.uuid4())
                self.add_document(document_id, status=evidence_status)
                with self.conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO judge_name_evidence (document_id, judge_id, raw_name) "
                        "VALUES (%s::uuid, %s::uuid, %s)",
                        (document_id, self.judge_id, raw),
                    )
        self.conn.commit()
        clear_roster_cache(self.court_id)

    def add_document(self, document_id: str, status: str = "active") -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO documents (id, court_id, document_type, s3_key, s3_bucket, "
                "format, content_hash, captured_at, status) "
                "VALUES (%s::uuid, %s::uuid, 'tentative_ruling', %s, 'test', 'pdf', %s, "
                "NOW(), %s::document_status) ON CONFLICT (id) DO NOTHING",
                (document_id, self.court_id, f"test/{document_id}.pdf", document_id, status),
            )

    def observe(self, raw: str, source: str, document_id: str | None = None) -> bool:
        """One document's judge resolution; True when it renamed the judge.

        Each call is a new document unless *document_id* names one.  As in
        the worker, the document row is written after resolve_judge, in the
        same transaction.
        """
        document_id = document_id or str(uuid.uuid4())
        renamed: set[str] = set()
        judge_id = resolve_judge(
            self.conn,
            raw,
            self.court_id,
            source=source,
            renamed_judge_ids=renamed,
            document_id=document_id,
        )
        self.add_document(document_id)
        self.conn.commit()
        assert judge_id == self.judge_id, f"{raw!r} resolved to another judge"
        return bool(renamed)

    def canonical(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT canonical_name FROM judges WHERE id = %s::uuid", (self.judge_id,))
            return str(cur.fetchone()[0])

    def evidence_rows(self) -> list[tuple[str, str]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT document_id::text, raw_name FROM judge_name_evidence "
                "WHERE judge_id = %s::uuid ORDER BY 1",
                (self.judge_id,),
            )
            return [(str(r[0]), str(r[1])) for r in cur.fetchall()]

    def close(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE court_id = %s::uuid", (self.court_id,))
            # Deleting the judges cascades to their judge_name_evidence rows.
            cur.execute("DELETE FROM judges WHERE court_id = %s::uuid", (self.court_id,))
            cur.execute("DELETE FROM courts WHERE id = %s::uuid", (self.court_id,))
            cur.execute(
                "DELETE FROM court_directory_snapshots WHERE court_id = %s",
                (self.snapshot_court,),
            )
        self.conn.commit()
        self.conn.close()
        clear_roster_cache(self.court_id)

    def judge_count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM judges WHERE court_id = %s::uuid", (self.court_id,))
            return int(cur.fetchone()[0])


def _run_orders(
    start: str,
    seed_aliases: list[tuple[str, str]],
    observations: list[tuple[str, str]],
    orders: int = 12,
    evidence: list[tuple[str, int]] | None = None,
    roster: dict[str, str] | None = None,
) -> tuple[set[str], int, int]:
    """Run *observations* (one new document each) in *orders* random orders.

    Returns the set of final names and the fewest and most renames any order
    caused.
    """
    finals: set[str] = set()
    renames_per_order: list[int] = []
    for seed in range(orders):
        order = list(observations)
        random.Random(seed).shuffle(order)
        court = _PgCourt(start, seed_aliases, roster=roster, evidence=evidence)
        try:
            renames_per_order.append(sum(court.observe(raw, source) for raw, source in order))
            finals.add(court.canonical())
        finally:
            court.close()
    return finals, min(renames_per_order), max(renames_per_order)


@_needs_pg
class TestArbitrationOrderIndependencePg:
    """The same observed spellings settle on one name in any order (#4922).

    On dev, judge fb070234 (Orange, 254 rulings) flipped between 'Shaina
    Clover' and 'H. Shaina Clover' three times in an hour: both spellings
    had non-roster aliases, so every document with the other spelling
    promoted it.  Each flip re-indexed all of the judge's rulings.
    """

    # Dev state before the fix: both spellings read by the rebuild, plus the
    # roster's spelling linked to the judge.  Both spellings are printed on
    # many documents, a real tie.
    _CLOVER_SEED = [
        ("Shaina Clover", "rebuild-ca-orange"),
        ("H. Shaina Clover", "rebuild-ca-orange"),
        ("H. Shaina Colover", "roster_match"),
    ]
    _CLOVER_EVIDENCE = [("Shaina Clover", 10), ("H. Shaina Clover", 10)]
    _CLOVER_OBS = [
        ("Shaina Clover", "reingest-ca-orange"),
        ("H. Shaina Clover", "reingest-ca-orange"),
        ("Shaina Clover", "ca-orange"),
        ("H. Shaina Clover", "ca-orange"),
    ] * 2

    @pytest.mark.parametrize("start", ["Shaina Clover", "H. Shaina Clover"])
    def test_clover_spellings_settle_with_at_most_one_rename(self, start: str) -> None:
        finals, _, max_renames = _run_orders(
            start, self._CLOVER_SEED, self._CLOVER_OBS, evidence=self._CLOVER_EVIDENCE
        )
        assert finals == {"H. Shaina Clover"}
        assert max_renames <= 1
        if start == "H. Shaina Clover":
            assert max_renames == 0

    def test_alternating_clover_spellings_rename_at_most_once(self) -> None:
        """The issue's AC: alternating documents leave the name alone after one rename."""
        court = _PgCourt("Shaina Clover", self._CLOVER_SEED, evidence=self._CLOVER_EVIDENCE)
        try:
            renames = [
                court.observe(raw, "reingest-ca-orange")
                for raw in ["H. Shaina Clover", "Shaina Clover"] * 4
            ]
            assert sum(renames) == 1
            assert court.canonical() == "H. Shaina Clover"
        finally:
            court.close()

    @pytest.mark.parametrize("start", ["Craig L Griffin", "Craig L. Griffin"])
    def test_griffin_spellings_settle_with_at_most_one_rename(self, start: str) -> None:
        seed = [
            ("Craig L Griffin", "rebuild-ca-orange"),
            ("Craig L. Griffin", "rebuild-ca-orange"),
            ("Craig L. Griffin", "roster_match"),
        ]
        obs = [
            ("Craig L Griffin", "reingest-ca-orange"),
            ("Craig L. Griffin", "reingest-ca-orange"),
            ("Craig L Griffin", "scraper"),
            ("Craig L. Griffin", "scraper"),
        ] * 2
        finals, _, max_renames = _run_orders(
            start, seed, obs, evidence=[("Craig L Griffin", 10), ("Craig L. Griffin", 10)]
        )
        assert finals == {"Craig L. Griffin"}
        assert max_renames <= 1

    def test_roster_only_start_converges_to_one_name(self) -> None:
        """From a canonical no document printed, every order ends on the same name."""
        seed = [
            ("Shaina Clover", "roster_match"),
            ("H. Shaina Clover", "roster_match"),
            ("H. Shaina Colover", "roster_match"),
        ]
        obs = [
            ("Shaina Clover", "rebuild-ca-orange"),
            ("H. Shaina Clover", "reingest-ca-orange"),
            ("Shaina Clover", "reingest-ca-orange"),
            ("H. Shaina Clover", "rebuild-ca-orange"),
        ] * 2 + [("H. Shaina Colover", "scraper")]
        finals, _, _ = _run_orders("H. Shaina Colover", seed, obs, orders=24)
        assert finals == {"H. Shaina Clover"}


# The roster spelling: 'Craig L. Griffen' reaches the judge through the
# Step 3b roster match, 'Craig L. M. Griffin' through the near-duplicate step.
_GRIFFIN_ROSTER = {"C1": "Craig L. Griffin"}
# Or through an alias (Step 1), for tests without a roster.
_GRIFFEN_ALIAS = [("Craig L. Griffen", "ca-orange-tentatives")]


@_needs_pg
class TestDocumentEvidencePg:
    """Arbitration counts documents, not pipelines (#4928).

    A source is a scraper id, so a county has about four of them.  Counting
    sources let one document reading 'Craig L. Griffen' or 'Craig L. M.
    Griffin' rename a judge printed on hundreds of documents, and every
    rename re-indexed all of that judge's rulings (#4920).
    """

    @pytest.mark.parametrize("typo", ["Craig L. Griffen", "Craig L. M. Griffin"])
    @pytest.mark.parametrize("documents", [1, 5])
    def test_single_document_never_renames(self, typo: str, documents: int) -> None:
        """One typo document, in any position, never renames a judge with N documents."""
        obs = [("Craig L. Griffin", "ca-orange-tentatives")] * documents + [
            (typo, "ca-orange-tentatives")
        ]
        finals, _, max_renames = _run_orders(
            "Craig L. Griffin", [], obs, orders=8, roster=_GRIFFIN_ROSTER
        )
        assert finals == {"Craig L. Griffin"}
        assert max_renames == 0

    def test_single_document_never_renames_a_seeded_judge(self) -> None:
        """The issue's AC: 5 documents of evidence, then one typo document."""
        for typo in ("Craig L. Griffen", "Craig L. M. Griffin"):
            court = _PgCourt(
                "Craig L. Griffin",
                [("Craig L. Griffin", "ca-orange-tentatives")],
                roster=_GRIFFIN_ROSTER,
                evidence=[("Craig L. Griffin", 5)],
            )
            try:
                assert court.observe(typo, "ca-orange-tentatives") is False
                assert court.canonical() == "Craig L. Griffin"
            finally:
                court.close()

    def test_real_majority_renames_exactly_once(self) -> None:
        """Eight documents against two: every order renames, and only once."""
        obs = [("Craig L. Griffin", "ca-orange-tentatives")] * 2 + [
            ("Craig L. Griffen", "ca-orange-tentatives")
        ] * 8
        finals, min_renames, max_renames = _run_orders(
            "Craig L. Griffin", [], obs, orders=12, roster=_GRIFFIN_ROSTER
        )
        assert finals == {"Craig L. Griffen"}
        assert min_renames == max_renames == 1

    def test_reprocessing_one_document_counts_once(self) -> None:
        """The same document read by the live scraper, a reingest and a rebuild is one vote.

        Before, three sources reading one document outvoted a spelling one
        source read from many documents.
        """
        court = _PgCourt("Craig L. Griffin", _GRIFFEN_ALIAS, evidence=[("Craig L. Griffin", 2)])
        try:
            document_id = str(uuid.uuid4())
            for source in ("ca-orange-tentatives", "reingest-ca-orange", "rebuild-ca-orange"):
                assert court.observe("Craig L. Griffen", source, document_id=document_id) is False
            assert court.canonical() == "Craig L. Griffin"
            assert (document_id, "Craig L. Griffen") in court.evidence_rows()
            assert len(court.evidence_rows()) == 3
        finally:
            court.close()

    def test_reprocessed_document_replaces_its_vote(self) -> None:
        court = _PgCourt("Craig L. Griffin", _GRIFFEN_ALIAS)
        try:
            document_id = str(uuid.uuid4())
            court.observe("Craig L. Griffen", "ca-orange-tentatives", document_id=document_id)
            court.observe("Craig L. Griffin", "reingest-ca-orange", document_id=document_id)
            assert court.evidence_rows() == [(document_id, "Craig L. Griffin")]
        finally:
            court.close()

    def test_roster_only_name_yields_on_the_second_document(self) -> None:
        # The alias only routes 'Shaina Clover' to the judge; aliases are not
        # evidence.
        court = _PgCourt(
            "H. Shaina Colover",
            [("H. Shaina Colover", "roster_match"), ("Shaina Clover", "rebuild-ca-orange")],
        )
        try:
            assert court.observe("Shaina Clover", "ca-orange") is False
            assert court.canonical() == "H. Shaina Colover"
            assert court.observe("Shaina Clover", "ca-orange") is True
            assert court.canonical() == "Shaina Clover"
        finally:
            court.close()

    def test_superseded_documents_do_not_count(self) -> None:
        court = _PgCourt(
            "Craig L. Griffin",
            [("Craig L. Griffen", "ca-orange-tentatives")],
            evidence=[("Craig L. Griffen", 5)],
            evidence_status="superseded",
        )
        try:
            assert court.observe("Craig L. Griffen", "ca-orange-tentatives") is False
            assert court.canonical() == "Craig L. Griffin"
        finally:
            court.close()

    def test_vote_is_for_the_printed_spelling(self) -> None:
        """The worker's enrichment swaps a known alias for the canonical name.

        The document still votes for the spelling it printed, so alias
        spellings do not silently count as support for today's canonical.
        Two such documents rename the roster-only judge.
        """
        court = _PgCourt("H. Shaina Colover", [("Shaina Clover", "rebuild-ca-orange")])
        try:
            for _ in range(2):
                document_id = str(uuid.uuid4())
                judge_id = resolve_judge(
                    court.conn,
                    "H. Shaina Colover",
                    court.court_id,
                    source="ca-orange",
                    document_id=document_id,
                    printed_name="Shaina Clover",
                )
                court.add_document(document_id)
                court.conn.commit()
                assert judge_id == court.judge_id
            assert court.canonical() == "Shaina Clover"
            assert {raw for _, raw in court.evidence_rows()} == {"Shaina Clover"}
        finally:
            court.close()

    def test_unusable_printed_spelling_falls_back_to_the_raw_name(self) -> None:
        court = _PgCourt("Craig L. Griffin", [])
        try:
            document_id = str(uuid.uuid4())
            resolve_judge(
                court.conn,
                "Craig L. Griffin",
                court.court_id,
                source="ca-orange",
                document_id=document_id,
                printed_name="Griffin",
            )
            court.add_document(document_id)
            court.conn.commit()
            assert court.evidence_rows() == [(document_id, "Craig L. Griffin")]
        finally:
            court.close()

    def test_call_without_document_adds_no_evidence(self) -> None:
        court = _PgCourt("Craig L. Griffin", _GRIFFEN_ALIAS)
        try:
            judge_id = resolve_judge(
                court.conn, "Craig L. Griffen", court.court_id, source="ca-orange-tentatives"
            )
            court.conn.commit()
            assert judge_id == court.judge_id
            assert court.evidence_rows() == []
            assert court.canonical() == "Craig L. Griffin"
        finally:
            court.close()


# ---------------------------------------------------------------------------
# Real Postgres: Step 3b finds a renamed roster judge through its aliases (#4926)
# ---------------------------------------------------------------------------


@_needs_pg
class TestRosterMatchFindsRenamedJudgePg:
    """Step 3b must find the roster's judge after its canonical name moved off
    the roster spelling (#4926).

    On dev, judge fb070234 (Orange) is canonical 'H. Shaina Clover'; the
    roster spelling 'H. Shaina Colover' is only a ``roster_match`` alias.
    A document printing 'Shaina H. Colover' (never seen before) misses
    Step 1, misses the near-duplicate step, roster-matches 'H. Shaina
    Colover', and then found no judge with that *canonical* name, so Step 4
    created a second judge row.
    """

    _ROSTER = {"C23": "H. Shaina Colover"}
    _RENAMED_ALIASES = [
        ("Shaina Clover", "rebuild-ca-orange"),
        ("H. Shaina Clover", "reingest-ca-orange"),
        ("H. Shaina Colover", "roster_match"),
    ]

    def test_new_roster_spelling_joins_renamed_judge(self) -> None:
        court = _PgCourt("H. Shaina Clover", self._RENAMED_ALIASES, roster=self._ROSTER)
        try:
            renamed: set[str] = set()
            judge_id = resolve_judge(
                court.conn,
                "Shaina H. Colover",
                court.court_id,
                source="reingest-ca-orange",
                renamed_judge_ids=renamed,
            )
            court.conn.commit()
            assert judge_id == court.judge_id
            assert court.judge_count() == 1
            # The better-supported Clover spelling keeps the name.
            assert court.canonical() == "H. Shaina Clover"
            assert renamed == set()
        finally:
            court.close()

    def test_new_roster_spelling_without_source_joins_renamed_judge(self) -> None:
        court = _PgCourt("H. Shaina Clover", self._RENAMED_ALIASES, roster=self._ROSTER)
        try:
            judge_id = resolve_judge(court.conn, "Shaina H. Colover", court.court_id)
            court.conn.commit()
            assert judge_id == court.judge_id
            assert court.judge_count() == 1
            assert court.canonical() == "H. Shaina Clover"
        finally:
            court.close()

    def test_roster_spelling_held_by_a_document_alias_is_found(self) -> None:
        """The roster spelling may sit under any alias source, not only roster_match."""
        aliases = [
            ("Shaina Clover", "rebuild-ca-orange"),
            ("H. Shaina Clover", "reingest-ca-orange"),
            ("H. Shaina Colover", "scraper"),
        ]
        court = _PgCourt("H. Shaina Clover", aliases, roster=self._ROSTER)
        try:
            judge_id = resolve_judge(
                court.conn, "Shaina H. Colover", court.court_id, source="ca-orange"
            )
            court.conn.commit()
            assert judge_id == court.judge_id
            assert court.judge_count() == 1
        finally:
            court.close()

    def test_rename_through_alias_match_is_reported(self) -> None:
        """A rename reached through the alias match lands in renamed_judge_ids (#4920)."""
        aliases = [
            ("Shaina Clover", "rebuild-ca-orange"),
            ("H. Shaina Clover", "rebuild-ca-orange"),
            ("H. Shaina Clover", "reingest-ca-orange"),
            ("H. Shaina Colover", "roster_match"),
        ]
        # A real tie between the two Clover spellings, which the fuller one
        # wins on this document's arbitration.
        court = _PgCourt(
            "Shaina Clover",
            aliases,
            roster=self._ROSTER,
            evidence=[("Shaina Clover", 3), ("H. Shaina Clover", 3)],
        )
        try:
            renamed: set[str] = set()
            judge_id = resolve_judge(
                court.conn,
                "Shaina H. Colover",
                court.court_id,
                source="ca-orange",
                renamed_judge_ids=renamed,
            )
            court.conn.commit()
            assert judge_id == court.judge_id
            assert court.judge_count() == 1
            assert court.canonical() == "H. Shaina Clover"
            assert renamed == {court.judge_id}
        finally:
            court.close()

    def test_bootstrap_judge_found_by_its_roster_alias(self) -> None:
        """A judge created under a non-roster spelling (#3858) is found by its roster alias.

        The first document creates 'H. Shaina Clover' and stores the roster
        spelling as a roster_match alias; the second, with a new spelling
        that matches the roster, must join that judge.
        """
        court = _PgCourt("Zed Q. Other", [], roster=self._ROSTER)
        try:
            first = resolve_judge(court.conn, "H. Shaina Clover", court.court_id, source="a")
            court.conn.commit()
            second = resolve_judge(court.conn, "Shaina H. Colover", court.court_id, source="b")
            court.conn.commit()
            assert first is not None
            assert second == first
            assert court.judge_count() == 2
        finally:
            court.close()

    def test_canonical_match_preferred_over_alias_match(self) -> None:
        """A judge whose canonical is the roster spelling wins over another judge's alias."""
        court = _PgCourt(
            "Zed Q. Other", [("H. Shaina Colover", "roster_match")], roster=self._ROSTER
        )
        try:
            with court.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO judges (canonical_name, court_id) "
                    "VALUES ('H. Shaina Colover', %s::uuid) RETURNING id",
                    (court.court_id,),
                )
                roster_judge = str(cur.fetchone()[0])
            court.conn.commit()
            # A typo of the roster name: misses Steps 1-3, roster-matches in 3b.
            judge_id = resolve_judge(court.conn, "H. Shaina Collover", court.court_id)
            court.conn.commit()
            assert judge_id == roster_judge
            assert court.judge_count() == 2
        finally:
            court.close()
