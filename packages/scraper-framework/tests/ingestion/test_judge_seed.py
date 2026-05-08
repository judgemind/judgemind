"""Tests for ``ingestion.judge_seed.seed_judges_from_directory_snapshots`` (#4370).

The helper exists to break the chicken-and-egg between #4297's
single-word surname-expansion helper and the
``_looks_like_valid_judge_name`` rejection guard:

- ``_looks_like_valid_judge_name`` rejects single-word names from
  creating new judge rows (defense against truncated/garbage entries).
- So a judge who only ever appears in tentative rulings as a bare
  surname never enters ``derived.judges``.
- So #4297's surname-suffix lookup has nothing to expand against.

This module seeds ``derived.judges`` from the canonical mapping in
``derived.court_directory_snapshots`` so #4297's helper has multi-word
candidates available.

Coverage matrix:
  - Happy path: latest snapshot per court is read, valid names are
    inserted, stats are accurate.
  - Idempotency: ON CONFLICT DO NOTHING means a second run with the
    same data is a no-op (zero inserts, same skipped_existing count).
  - Invalid-name guard: single-word values, empty strings, garbage are
    rejected and counted as skipped_invalid.
  - Snapshot court_id -> derived.courts.id translation: ``ca_los_angeles``
    resolves to the court UUID for ``court_code = ca-los-angeles``.
  - Missing court row: snapshot for a court that has no derived.courts
    row is skipped, counted as skipped_no_court.
  - Single-court mode: only_court_id restricts the query.
  - Deduplication: when multiple departments map to the same canonical
    name, only one INSERT is attempted (one candidate counted).
  - Mapping JSON parse failure: skipped without raising.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ingestion.judge_seed import seed_judges_from_directory_snapshots


def _make_mock_conn() -> tuple[MagicMock, MagicMock]:
    """Create a mock psycopg connection with cursor context manager."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


class TestHappyPath:
    """Latest snapshot is read, valid names are inserted, stats are accurate."""

    def test_inserts_unique_canonical_names_from_la_snapshot(self) -> None:
        """LA snapshot with three departments produces three INSERTs when none
        of those judges already exist."""
        mock_conn, mock_cur = _make_mock_conn()

        # fetchall: latest snapshots query -> one LA row
        # fetchone: court UUID lookup for ca_los_angeles -> one row
        # rowcount: each INSERT returns 1 (newly inserted)
        mock_cur.fetchall.return_value = [
            (
                "ca_los_angeles",
                {
                    "1": "Karine Mkrtchyan",
                    "25": "Jonathan H. Eisenman",
                    "62": "Maureen Duffy-Lewis",
                },
            )
        ]
        mock_cur.fetchone.return_value = ("court-uuid-la",)
        mock_cur.rowcount = 1

        stats = seed_judges_from_directory_snapshots(mock_conn)

        assert stats["courts"] == 1
        assert stats["candidates"] == 3
        assert stats["inserted"] == 3
        assert stats["skipped_existing"] == 0
        assert stats["skipped_invalid"] == 0
        assert stats["skipped_no_court"] == 0

    def test_inserts_use_judges_table_with_on_conflict_do_nothing(self) -> None:
        """The INSERT must use ON CONFLICT DO NOTHING (idempotent)."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [("ca_los_angeles", {"1": "Alice Smith"})]
        mock_cur.fetchone.return_value = ("court-uuid-la",)
        mock_cur.rowcount = 1

        seed_judges_from_directory_snapshots(mock_conn)

        # Find the INSERT call.
        insert_calls = [
            c for c in mock_cur.execute.call_args_list if "INSERT INTO judges" in c.args[0]
        ]
        assert len(insert_calls) == 1
        sql = insert_calls[0].args[0]
        assert "ON CONFLICT (canonical_name, court_id) DO NOTHING" in sql
        # No UPDATE clause — INSERT-only per AC #2.
        assert "DO UPDATE" not in sql
        assert "UPDATE judges" not in sql

    def test_passes_correct_canonical_name_and_court_uuid_to_insert(self) -> None:
        """Insert parameters are (canonical_name, court_uuid)."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [("ca_los_angeles", {"25": "Karine Mkrtchyan"})]
        mock_cur.fetchone.return_value = ("la-uuid",)
        mock_cur.rowcount = 1

        seed_judges_from_directory_snapshots(mock_conn)

        insert_calls = [
            c for c in mock_cur.execute.call_args_list if "INSERT INTO judges" in c.args[0]
        ]
        assert insert_calls[0].args[1] == ("Karine Mkrtchyan", "la-uuid")


class TestIdempotency:
    """Re-running on the same snapshot should not double-insert.

    AC #2: "unit test seeds a snapshot, runs the helper twice, asserts
    second run is a no-op (no duplicate inserts, no UPDATE statements
    issued)."
    """

    def test_second_run_is_no_op_when_judges_already_exist(self) -> None:
        """When ON CONFLICT fires (rowcount=0), candidates count toward
        skipped_existing instead of inserted."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [
            (
                "ca_los_angeles",
                {"1": "Alice Smith", "2": "Bob Jones"},
            )
        ]
        mock_cur.fetchone.return_value = ("court-uuid-la",)
        # ON CONFLICT DO NOTHING -> rowcount = 0 for already-present rows.
        mock_cur.rowcount = 0

        stats = seed_judges_from_directory_snapshots(mock_conn)

        assert stats["candidates"] == 2
        assert stats["inserted"] == 0
        assert stats["skipped_existing"] == 2

    def test_running_twice_produces_zero_inserts_on_second_call(self) -> None:
        """Run the helper twice on the same connection.  First call:
        rowcount=1 (newly inserted).  Second call: rowcount=0 (ON CONFLICT
        fired).  Total: 2 inserted in run 1, 0 inserted in run 2.

        This exercises the full AC #2 scenario verbatim — "seeds a
        snapshot, runs the helper twice, asserts second run is a no-op."
        """
        mock_conn, mock_cur = _make_mock_conn()
        # The two queries the seeder makes are identical across runs;
        # only rowcount differs (mock-controlled below).
        mock_cur.fetchall.return_value = [
            (
                "ca_los_angeles",
                {"1": "Alice Smith", "2": "Bob Jones"},
            )
        ]
        mock_cur.fetchone.return_value = ("court-uuid-la",)

        # Run 1 — inserts both judges.
        mock_cur.rowcount = 1
        stats_first = seed_judges_from_directory_snapshots(mock_conn)
        assert stats_first["inserted"] == 2
        assert stats_first["skipped_existing"] == 0

        # Run 2 — ON CONFLICT fires for both, no rows inserted.
        mock_cur.rowcount = 0
        stats_second = seed_judges_from_directory_snapshots(mock_conn)
        assert stats_second["inserted"] == 0
        assert stats_second["skipped_existing"] == 2

        # No UPDATE statements ever issued (neither run).
        for c in mock_cur.execute.call_args_list:
            sql_lower = c.args[0].lstrip().lower()
            assert not sql_lower.startswith("update "), f"Helper issued UPDATE: {c.args[0]!r}"
            assert "do update" not in sql_lower

    def test_no_update_statements_issued(self) -> None:
        """The helper must NEVER issue an UPDATE.  Only SELECT + INSERT."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [("ca_los_angeles", {"1": "Alice Smith"})]
        mock_cur.fetchone.return_value = ("court-uuid-la",)
        mock_cur.rowcount = 0  # already exists

        seed_judges_from_directory_snapshots(mock_conn)

        # Inspect every SQL statement issued.
        for c in mock_cur.execute.call_args_list:
            sql = c.args[0]
            # Trim leading whitespace to handle indented SQL strings.
            sql_lower = sql.lstrip().lower()
            # We allow the substring "update" inside "updated_at" but not at
            # the start of a SQL statement.
            assert not sql_lower.startswith("update "), (
                f"Helper issued an UPDATE statement: {sql!r}"
            )
            assert "do update" not in sql_lower, f"Helper used ON CONFLICT DO UPDATE: {sql!r}"


class TestInvalidNameGuard:
    """Names that fail _looks_like_valid_judge_name are skipped."""

    def test_single_word_name_skipped(self) -> None:
        """A bare surname like 'Mkrtchyan' should be rejected — that's the
        whole bug class we're trying to avoid creating."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [
            (
                "ca_los_angeles",
                {"1": "Mkrtchyan", "2": "Karine Mkrtchyan"},
            )
        ]
        mock_cur.fetchone.return_value = ("court-uuid-la",)
        mock_cur.rowcount = 1

        stats = seed_judges_from_directory_snapshots(mock_conn)

        assert stats["candidates"] == 2
        assert stats["skipped_invalid"] == 1  # single-word "Mkrtchyan"
        assert stats["inserted"] == 1  # "Karine Mkrtchyan"

    def test_empty_string_skipped(self) -> None:
        """Empty or whitespace-only mapping values are dropped before
        the validity check via the truthiness filter, and any that slip
        through are caught by the validity check."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [
            (
                "ca_los_angeles",
                {"1": "", "2": "Alice Smith"},
            )
        ]
        mock_cur.fetchone.return_value = ("court-uuid-la",)
        mock_cur.rowcount = 1

        stats = seed_judges_from_directory_snapshots(mock_conn)

        # Empty string is filtered by `if name` truthiness gate, so it
        # never becomes a candidate.
        assert stats["candidates"] == 1
        assert stats["inserted"] == 1


class TestCourtIdResolution:
    """Snapshot court_id (text) resolves to derived.courts.id (uuid)."""

    def test_snapshot_court_id_translated_to_court_code(self) -> None:
        """ca_los_angeles -> court_code = ca-los-angeles."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [("ca_los_angeles", {"1": "Alice Smith"})]
        mock_cur.fetchone.return_value = ("la-uuid",)
        mock_cur.rowcount = 1

        seed_judges_from_directory_snapshots(mock_conn)

        # The court-lookup query must pass the dashed form.
        court_lookup_calls = [
            c for c in mock_cur.execute.call_args_list if "FROM courts" in c.args[0]
        ]
        assert len(court_lookup_calls) == 1
        assert court_lookup_calls[0].args[1] == ("ca-los-angeles",)

    def test_missing_court_row_counted_as_skipped_no_court(self) -> None:
        """A snapshot whose court isn't in derived.courts should not crash."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [("ca_unknown_county", {"1": "Alice Smith"})]
        # courts table returns no row.
        mock_cur.fetchone.return_value = None

        stats = seed_judges_from_directory_snapshots(mock_conn)

        assert stats["courts"] == 1
        assert stats["skipped_no_court"] == 1
        assert stats["candidates"] == 0  # never reached
        assert stats["inserted"] == 0


class TestSingleCourtMode:
    """only_court_id restricts the query to a single court."""

    def test_only_court_id_filters_query(self) -> None:
        """The latest-snapshots query must filter by court_id when
        only_court_id is set."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [("ca_los_angeles", {"1": "Alice Smith"})]
        mock_cur.fetchone.return_value = ("la-uuid",)
        mock_cur.rowcount = 1

        seed_judges_from_directory_snapshots(mock_conn, only_court_id="ca_los_angeles")

        snapshot_calls = [
            c for c in mock_cur.execute.call_args_list if "court_directory_snapshots" in c.args[0]
        ]
        assert len(snapshot_calls) == 1
        sql = snapshot_calls[0].args[0]
        assert "WHERE court_id = %s" in sql
        assert snapshot_calls[0].args[1] == ("ca_los_angeles",)

    def test_default_uses_distinct_on_court_id(self) -> None:
        """Without only_court_id, the query reads the latest row per court."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = []

        seed_judges_from_directory_snapshots(mock_conn)

        snapshot_calls = [
            c for c in mock_cur.execute.call_args_list if "court_directory_snapshots" in c.args[0]
        ]
        assert len(snapshot_calls) == 1
        sql = snapshot_calls[0].args[0]
        assert "DISTINCT ON (court_id)" in sql


class TestMappingDedup:
    """Multiple departments mapped to the same judge produce one candidate."""

    def test_same_judge_in_multiple_departments_inserted_once(self) -> None:
        """Two departments with the same judge -> one candidate, one INSERT."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [
            (
                "ca_los_angeles",
                {"1": "Alice Smith", "2": "Alice Smith", "3": "Bob Jones"},
            )
        ]
        mock_cur.fetchone.return_value = ("la-uuid",)
        mock_cur.rowcount = 1

        stats = seed_judges_from_directory_snapshots(mock_conn)

        assert stats["candidates"] == 2  # Alice + Bob, deduped
        assert stats["inserted"] == 2

        insert_calls = [
            c for c in mock_cur.execute.call_args_list if "INSERT INTO judges" in c.args[0]
        ]
        assert len(insert_calls) == 2


class TestMultipleCourts:
    """Multiple courts in one run."""

    def test_processes_all_courts(self) -> None:
        """Two courts each contribute their judges."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [
            ("ca_los_angeles", {"1": "Alice Smith"}),
            ("ca_orange", {"1": "Bob Jones"}),
        ]
        # courts lookups: la-uuid then orange-uuid
        mock_cur.fetchone.side_effect = [
            ("la-uuid",),
            ("orange-uuid",),
        ]
        mock_cur.rowcount = 1

        stats = seed_judges_from_directory_snapshots(mock_conn)

        assert stats["courts"] == 2
        assert stats["candidates"] == 2
        assert stats["inserted"] == 2


class TestMappingJsonString:
    """Mappings stored as JSON strings (legacy / non-jsonb path) are parsed."""

    def test_string_mapping_parsed_as_json(self) -> None:
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [
            ("ca_los_angeles", json.dumps({"1": "Alice Smith"})),
        ]
        mock_cur.fetchone.return_value = ("la-uuid",)
        mock_cur.rowcount = 1

        stats = seed_judges_from_directory_snapshots(mock_conn)

        assert stats["candidates"] == 1
        assert stats["inserted"] == 1

    def test_unparseable_mapping_skipped(self) -> None:
        """Malformed JSON is logged + skipped, not raised."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = [
            ("ca_los_angeles", "not valid json {"),
        ]

        stats = seed_judges_from_directory_snapshots(mock_conn)

        # The court isn't even counted because _latest_snapshots filters it
        # out before yielding.
        assert stats["courts"] == 0
        assert stats["candidates"] == 0


class TestEmptySnapshots:
    """Helper handles the empty snapshot table without raising."""

    def test_no_snapshots_returns_zero_stats(self) -> None:
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchall.return_value = []

        stats = seed_judges_from_directory_snapshots(mock_conn)

        assert stats == {
            "courts": 0,
            "candidates": 0,
            "inserted": 0,
            "skipped_existing": 0,
            "skipped_invalid": 0,
            "skipped_no_court": 0,
        }
