"""Tests for the backfill_split_rulings script.

Tests cover:
  - Candidate query building with and without county filter
  - try_split() returning None for non-splittable rulings
  - try_split() returning splits for multi-case rulings
  - process_batch() dry-run mode producing correct reports
  - process_batch() apply mode calling DB functions
  - run_backfill() end-to-end with mocked DB
  - SplitAction dataclass correctness
  - Cursor-based pagination
  - Missing ruling document detection (--check-missing)
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import psycopg.errors

from ingestion.splitter import SplitResult, _splitter_registry, register_splitter

# The script lives in scripts/ — add it to the path for import.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# Import the script module via importlib (it's not a package module).
_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "backfill_split_rulings.py"
)
_spec = importlib.util.spec_from_file_location("backfill_split_rulings", _SCRIPT_PATH)
assert _spec is not None
assert _spec.loader is not None
backfill_mod = importlib.util.module_from_spec(_spec)
sys.modules["backfill_split_rulings"] = backfill_mod
_spec.loader.exec_module(backfill_mod)

# Pull in the names we need.
CandidateRuling = backfill_mod.CandidateRuling
MissingRulingDocument = backfill_mod.MissingRulingDocument
SplitAction = backfill_mod.SplitAction
build_candidate_query = backfill_mod.build_candidate_query
try_split = backfill_mod.try_split
apply_split = backfill_mod.apply_split
process_batch = backfill_mod.process_batch
fetch_candidates = backfill_mod.fetch_candidates
find_missing_ruling_documents = backfill_mod.find_missing_ruling_documents
report_missing_rulings = backfill_mod.report_missing_rulings
_insert_split_ruling = backfill_mod._insert_split_ruling
_CURSOR_MIN_TIMESTAMP = backfill_mod._CURSOR_MIN_TIMESTAMP
_CURSOR_MIN_UUID = backfill_mod._CURSOR_MIN_UUID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidate(**overrides: Any) -> CandidateRuling:
    """Create a minimal CandidateRuling for testing."""
    defaults: dict[str, Any] = {
        "ruling_id": "rrrrrrrr-0000-0000-0000-000000000001",
        "document_id": "dddddddd-0000-0000-0000-000000000001",
        "case_id": "cccccccc-0000-0000-0000-000000000001",
        "court_id": "tttttttt-0000-0000-0000-000000000001",
        "judge_id": "jjjjjjjj-0000-0000-0000-000000000001",
        "hearing_date": "2026-03-15",
        "ruling_text": (
            "101 Smith vs Jones Motion to Compel\nGRANTED\n102 Doe vs Roe Demurrer\nSUSTAINED"
        ),
        "ruling_text_html": None,
        "department": "N15",
        "outcome": None,
        "motion_type": None,
        "state": "CA",
        "county": "Orange",
        "case_number": "30-2024-01393434",
        "case_title": None,
        "source_url": "https://example.com/ruling.pdf",
        "scraper_id": "oc-tentatives",
        "content_format": "pdf",
        "content_hash": "abc123",
        "s3_key": "rulings/2026/03/15/doc.pdf",
        "s3_bucket": "judgemind-archive",
        "captured_at": "2026-03-15T10:00:00",
        "created_at": datetime(2026, 3, 15, 10, 0, 0),
    }
    defaults.update(overrides)
    return CandidateRuling(**defaults)


def _multi_case_splitter(event_data: dict[str, Any]) -> list[SplitResult]:
    """A mock splitter that splits on '---' delimiter."""
    text = event_data.get("ruling_text", "")
    parts = text.split("---")
    if len(parts) < 2:
        return [SplitResult(ruling_text=text)]
    return [
        SplitResult(
            ruling_text=part.strip(),
            case_title=f"Case {i + 1} v. Defendant {i + 1}",
            case_number=f"30-2024-0000000{i + 1}",
        )
        for i, part in enumerate(parts)
        if part.strip()
    ]


# ---------------------------------------------------------------------------
# build_candidate_query tests
# ---------------------------------------------------------------------------


class TestBuildCandidateQuery:
    def test_with_specific_county(self) -> None:
        """When county is specified, query includes single county filter."""
        query, has_single, _has_dept = build_candidate_query(county="Orange")
        assert has_single is True
        assert "ct.county = %s" in query

    def test_without_county(self) -> None:
        """When no county is specified, query includes IN clause for all splittable counties."""
        query, has_single, _has_dept = build_candidate_query(county=None)
        assert has_single is False
        assert "ct.county IN" in query

    def test_query_orders_by_cursor(self) -> None:
        """Query uses cursor-based pagination with (created_at, id) ordering."""
        query, _, _ = build_candidate_query()
        assert "ORDER BY r.created_at, r.id" in query

    def test_query_has_length_filter(self) -> None:
        """Query filters by minimum ruling_text length."""
        query, _, _ = build_candidate_query()
        assert "LENGTH(r.ruling_text)" in query

    def test_with_department(self) -> None:
        """When department is specified, query includes department filter."""
        query, _has_county, has_dept = build_candidate_query(department="N18")
        assert has_dept is True
        assert "r.department = %s" in query

    def test_without_department(self) -> None:
        """When no department is specified, query has no department filter."""
        query, _has_county, has_dept = build_candidate_query(department=None)
        assert has_dept is False
        assert "r.department = %s" not in query

    def test_with_county_and_department(self) -> None:
        """Both county and department filters can be applied together."""
        query, has_county, has_dept = build_candidate_query(
            county="Orange",
            department="N18",
        )
        assert has_county is True
        assert has_dept is True
        assert "ct.county = %s" in query
        assert "r.department = %s" in query


# ---------------------------------------------------------------------------
# try_split tests
# ---------------------------------------------------------------------------


class TestTrySplit:
    def setup_method(self) -> None:
        """Save and clear the splitter registry."""
        self._saved_registry = dict(_splitter_registry)
        _splitter_registry.clear()

    def teardown_method(self) -> None:
        """Restore the splitter registry."""
        _splitter_registry.clear()
        _splitter_registry.update(self._saved_registry)

    def test_returns_none_when_no_splitter(self) -> None:
        """When no splitter is registered for the county, returns None."""
        candidate = _make_candidate(state="CA", county="Nonexistent")
        result = try_split(candidate)
        assert result is None

    def test_returns_none_for_single_case(self) -> None:
        """When the splitter returns a single result, returns None."""
        register_splitter("CA", "Orange", _multi_case_splitter)
        candidate = _make_candidate(ruling_text="Single case text only")
        result = try_split(candidate)
        assert result is None

    def test_returns_splits_for_multi_case(self) -> None:
        """When the splitter finds multiple cases, returns the splits."""
        register_splitter("CA", "Orange", _multi_case_splitter)
        text = "Case one text --- Case two text --- Case three text"
        candidate = _make_candidate(ruling_text=text)
        result = try_split(candidate)
        assert result is not None
        assert len(result) == 3
        assert result[0].ruling_text == "Case one text"
        assert result[0].case_title == "Case 1 v. Defendant 1"
        assert result[1].ruling_text == "Case two text"
        assert result[2].ruling_text == "Case three text"

    def test_builds_correct_event_data(self) -> None:
        """try_split passes the correct fields to split_document."""
        captured_events: list[dict[str, Any]] = []

        def capturing_splitter(event_data: dict[str, Any]) -> list[SplitResult]:
            captured_events.append(event_data)
            return []

        register_splitter("CA", "Orange", capturing_splitter)
        candidate = _make_candidate(
            state="CA",
            county="Orange",
            ruling_text="Some text",
            case_number="30-2024-01234567",
            department="C12",
            hearing_date="2026-03-20",
        )
        try_split(candidate)
        assert len(captured_events) == 1
        event = captured_events[0]
        assert event["state"] == "CA"
        assert event["county"] == "Orange"
        assert event["ruling_text"] == "Some text"
        assert event["case_number"] == "30-2024-01234567"
        assert event["department"] == "C12"
        assert event["hearing_date"] == "2026-03-20"


# ---------------------------------------------------------------------------
# process_batch tests (dry-run)
# ---------------------------------------------------------------------------


class TestProcessBatchDryRun:
    def setup_method(self) -> None:
        self._saved_registry = dict(_splitter_registry)
        _splitter_registry.clear()

    def teardown_method(self) -> None:
        _splitter_registry.clear()
        _splitter_registry.update(self._saved_registry)

    @patch("backfill_split_rulings.fetch_candidates")
    def test_dry_run_no_candidates(self, mock_fetch: MagicMock) -> None:
        """When no candidates are found, returns zeros."""
        mock_fetch.return_value = []
        conn = MagicMock()
        cursor = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

        checked, split_count, new_rulings, actions, next_cursor = process_batch(
            conn, 50, 5000, None, cursor, dry_run=True
        )
        assert checked == 0
        assert split_count == 0
        assert new_rulings == 0
        assert actions == []
        assert next_cursor == cursor

    @patch("backfill_split_rulings.fetch_candidates")
    def test_dry_run_no_splittable(self, mock_fetch: MagicMock) -> None:
        """When candidates don't split, returns checked but no splits."""
        # No splitter registered — try_split returns None for everything.
        candidate = _make_candidate()
        mock_fetch.return_value = [candidate]
        conn = MagicMock()
        cursor = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

        checked, split_count, new_rulings, actions, next_cursor = process_batch(
            conn, 50, 5000, None, cursor, dry_run=True
        )
        assert checked == 1
        assert split_count == 0
        assert new_rulings == 0
        assert actions == []

    @patch("backfill_split_rulings.fetch_candidates")
    def test_dry_run_produces_actions(self, mock_fetch: MagicMock) -> None:
        """Dry-run mode produces SplitActions without calling DB write functions."""
        register_splitter("CA", "Orange", _multi_case_splitter)
        candidate = _make_candidate(
            ruling_text="Case A text --- Case B text",
            document_id="dddddddd-0000-0000-0000-000000000001",
        )
        mock_fetch.return_value = [candidate]
        conn = MagicMock()
        cursor = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

        checked, split_count, new_rulings, actions, next_cursor = process_batch(
            conn, 50, 5000, None, cursor, dry_run=True
        )
        assert checked == 1
        assert split_count == 1
        assert new_rulings == 2
        assert len(actions) == 1

        action = actions[0]
        assert action.split_count == 2
        assert len(action.split_document_ids) == 2
        assert action.original_document_id == "dddddddd-0000-0000-0000-000000000001"
        assert action.split_case_titles == ["Case 1 v. Defendant 1", "Case 2 v. Defendant 2"]

    @patch("backfill_split_rulings.fetch_candidates")
    def test_cursor_advances(self, mock_fetch: MagicMock) -> None:
        """Cursor advances to the last candidate's (created_at, id)."""
        register_splitter("CA", "Orange", _multi_case_splitter)
        c1 = _make_candidate(
            ruling_id="rrrrrrrr-0000-0000-0000-000000000001",
            ruling_text="Single case only",
            created_at=datetime(2026, 3, 15, 10, 0, 0),
        )
        c2 = _make_candidate(
            ruling_id="rrrrrrrr-0000-0000-0000-000000000002",
            ruling_text="Case A --- Case B",
            created_at=datetime(2026, 3, 16, 12, 0, 0),
        )
        mock_fetch.return_value = [c1, c2]
        conn = MagicMock()
        cursor = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

        _, _, _, _, next_cursor = process_batch(conn, 50, 5000, None, cursor, dry_run=True)
        expected_cursor = (
            datetime(2026, 3, 16, 12, 0, 0),
            "rrrrrrrr-0000-0000-0000-000000000002",
        )
        assert next_cursor == expected_cursor


# ---------------------------------------------------------------------------
# process_batch tests (apply mode)
# ---------------------------------------------------------------------------


class TestProcessBatchApply:
    def setup_method(self) -> None:
        self._saved_registry = dict(_splitter_registry)
        _splitter_registry.clear()

    def teardown_method(self) -> None:
        _splitter_registry.clear()
        _splitter_registry.update(self._saved_registry)

    @patch("backfill_split_rulings.apply_split")
    @patch("backfill_split_rulings.fetch_candidates")
    def test_apply_calls_apply_split(self, mock_fetch: MagicMock, mock_apply: MagicMock) -> None:
        """Apply mode calls apply_split for splittable candidates."""
        register_splitter("CA", "Orange", _multi_case_splitter)
        candidate = _make_candidate(ruling_text="Case A --- Case B")
        mock_fetch.return_value = [candidate]
        mock_apply.return_value = SplitAction(
            original_ruling_id=candidate.ruling_id,
            original_document_id=candidate.document_id,
            original_case_number=candidate.case_number,
            split_count=2,
            split_document_ids=["id1", "id2"],
            split_case_numbers=["num1", "num2"],
            split_case_titles=["title1", "title2"],
        )
        conn = MagicMock()
        cursor = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

        checked, split_count, new_rulings, actions, _ = process_batch(
            conn, 50, 5000, None, cursor, dry_run=False
        )
        assert checked == 1
        assert split_count == 1
        assert new_rulings == 2
        mock_apply.assert_called_once()


# ---------------------------------------------------------------------------
# apply_split tests
# ---------------------------------------------------------------------------


class TestApplySplit:
    def setup_method(self) -> None:
        self._saved_registry = dict(_splitter_registry)
        _splitter_registry.clear()

    def teardown_method(self) -> None:
        _splitter_registry.clear()
        _splitter_registry.update(self._saved_registry)

    def test_creates_split_records(self) -> None:
        """apply_split creates document and ruling rows for each split."""
        candidate = _make_candidate(
            document_id="dddddddd-0000-0000-0000-000000000001",
        )
        splits = [
            SplitResult(ruling_text="Text A", case_title="A v. B", case_number="30-2024-00000001"),
            SplitResult(ruling_text="Text B", case_title="C v. D", case_number="30-2024-00000002"),
        ]

        conn = MagicMock()
        mock_cursor = MagicMock()
        # For _upsert_case: return a case_id UUID.
        mock_cursor.fetchone.return_value = ("new-case-id-uuid",)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        action = apply_split(conn, candidate, splits)

        assert action.split_count == 2
        assert len(action.split_document_ids) == 2
        assert action.split_case_numbers == ["30-2024-00000001", "30-2024-00000002"]
        assert action.split_case_titles == ["A v. B", "C v. D"]
        # Original ruling should be deleted.
        assert action.original_ruling_id == candidate.ruling_id

    def test_uses_original_case_when_no_split_case_number_same_title(self) -> None:
        """When split has no case_number and same case_title, keeps original case_id."""
        candidate = _make_candidate(
            case_id="original-case-id",
            case_number="ORIG-001",
            case_title="A v. B",
        )
        splits = [
            SplitResult(ruling_text="Text A", case_title="A v. B"),
            SplitResult(ruling_text="Text B", case_title="A v. B"),
        ]

        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        action = apply_split(conn, candidate, splits)

        # Both splits have the same title as the original -> same case_number.
        assert action.split_case_numbers == ["ORIG-001", "ORIG-001"]

    def test_north_jc_splits_create_separate_cases(self) -> None:
        """North JC: splits with different case_titles but no case_numbers create separate cases.

        This is the core fix for #1186: when the splitter provides no
        case_number (North JC PDFs have no case numbers) but each split has
        a unique case_title, each split should get its own case record.
        """
        candidate = _make_candidate(
            case_id="original-case-id",
            case_number="Zavala v. Becker",
            case_title="Zavala v. Becker",
            department="N17",
        )
        splits = [
            SplitResult(ruling_text="Text for Zavala", case_title="Zavala v. Becker"),
            SplitResult(ruling_text="Text for Post", case_title="Post v. Chung"),
            SplitResult(ruling_text="Text for Alpha", case_title="Alpha v. Beta"),
        ]

        conn = MagicMock()
        mock_cursor = MagicMock()
        # _upsert_case returns a unique case_id for each call.
        mock_cursor.fetchone.return_value = ("new-case-id-uuid",)
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        action = apply_split(conn, candidate, splits)

        assert action.split_count == 3
        # First split has same title as original -> keeps original case_number.
        assert action.split_case_numbers[0] == "Zavala v. Becker"
        # Other splits have different titles -> use title as synthetic case_number.
        assert action.split_case_numbers[1] == "Post v. Chung"
        assert action.split_case_numbers[2] == "Alpha v. Beta"

    def test_north_jc_split_no_title_keeps_original(self) -> None:
        """When split has no case_number AND no case_title, keeps original case."""
        candidate = _make_candidate(
            case_id="original-case-id",
            case_number="ORIG-001",
            case_title="Original Title",
        )
        splits = [
            SplitResult(ruling_text="Text A"),
            SplitResult(ruling_text="Text B"),
        ]

        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        action = apply_split(conn, candidate, splits)

        # No case_number or case_title on splits -> fall back to original.
        assert action.split_case_numbers == ["ORIG-001", "ORIG-001"]


# ---------------------------------------------------------------------------
# SplitAction tests
# ---------------------------------------------------------------------------


class TestSplitAction:
    def test_dataclass_construction(self) -> None:
        action = SplitAction(
            original_ruling_id="r1",
            original_document_id="d1",
            original_case_number="case-001",
            split_count=3,
            split_document_ids=["sd1", "sd2", "sd3"],
            split_case_numbers=["cn1", "cn2", "cn3"],
            split_case_titles=["t1", "t2", "t3"],
        )
        assert action.split_count == 3
        assert len(action.split_document_ids) == 3


# ---------------------------------------------------------------------------
# run_backfill integration test (mocked DB)
# ---------------------------------------------------------------------------


class TestRunBackfill:
    def setup_method(self) -> None:
        self._saved_registry = dict(_splitter_registry)
        _splitter_registry.clear()

    def teardown_method(self) -> None:
        _splitter_registry.clear()
        _splitter_registry.update(self._saved_registry)

    @patch("backfill_split_rulings.fetch_candidates")
    @patch("psycopg.connect")
    def test_dry_run_does_not_commit(self, mock_connect: MagicMock, mock_fetch: MagicMock) -> None:
        """Dry-run mode calls rollback, not commit."""
        register_splitter("CA", "Orange", _multi_case_splitter)

        candidate = _make_candidate(ruling_text="Case A --- Case B")
        # First call returns candidates, second call returns empty (end of data).
        mock_fetch.side_effect = [[candidate], []]

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        stats = backfill_mod.run_backfill("postgresql://test", dry_run=True)

        assert stats["total_checked"] == 1
        assert stats["total_split"] == 1
        assert stats["total_new_rulings"] == 2
        mock_conn.rollback.assert_called()
        mock_conn.commit.assert_not_called()

    @patch("backfill_split_rulings.fetch_candidates")
    @patch("backfill_split_rulings.apply_split")
    @patch("psycopg.connect")
    def test_apply_mode_commits(
        self,
        mock_connect: MagicMock,
        mock_apply: MagicMock,
        mock_fetch: MagicMock,
    ) -> None:
        """Apply mode calls commit after each batch."""
        register_splitter("CA", "Orange", _multi_case_splitter)

        candidate = _make_candidate(ruling_text="Case A --- Case B")
        mock_fetch.side_effect = [[candidate], []]
        mock_apply.return_value = SplitAction(
            original_ruling_id="r1",
            original_document_id="d1",
            original_case_number="c1",
            split_count=2,
            split_document_ids=["sd1", "sd2"],
            split_case_numbers=["n1", "n2"],
            split_case_titles=["t1", "t2"],
        )

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        stats = backfill_mod.run_backfill("postgresql://test", dry_run=False)

        assert stats["total_split"] == 1
        mock_conn.commit.assert_called()

    @patch("backfill_split_rulings.fetch_candidates")
    @patch("psycopg.connect")
    def test_limit_stops_processing(self, mock_connect: MagicMock, mock_fetch: MagicMock) -> None:
        """When limit is reached, processing stops."""
        register_splitter("CA", "Orange", _multi_case_splitter)

        candidates = [
            _make_candidate(
                ruling_id=f"rrrrrrrr-0000-0000-0000-{i:012d}",
                ruling_text=f"Case {i}A --- Case {i}B",
                created_at=datetime(2026, 3, 15, 10, i, 0),
            )
            for i in range(5)
        ]
        mock_fetch.side_effect = [candidates[:2], []]

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        stats = backfill_mod.run_backfill("postgresql://test", dry_run=True, limit=2)

        assert stats["total_checked"] == 2


# ---------------------------------------------------------------------------
# Idempotency tests (#1233)
# ---------------------------------------------------------------------------


class TestInsertSplitRulingIdempotency:
    """Tests for _insert_split_ruling handling duplicate content gracefully."""

    def test_returns_true_on_successful_insert(self) -> None:
        """Normal insert returns True."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = _insert_split_ruling(
            conn,
            document_id="dddddddd-0000-0000-0000-000000000001",
            case_id="cccccccc-0000-0000-0000-000000000001",
            court_id="tttttttt-0000-0000-0000-000000000001",
            judge_id=None,
            hearing_date="2026-03-15",
            ruling_text="Some ruling text",
            department="N15",
            outcome=None,
            motion_type=None,
        )
        assert result is True

    def test_returns_false_on_unique_violation(self) -> None:
        """When UniqueViolation is raised (duplicate text hash), returns False."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        # First call is SAVEPOINT, second call is the INSERT which raises.
        call_count = 0

        def side_effect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise psycopg.errors.UniqueViolation("duplicate key")

        mock_cursor.execute.side_effect = side_effect

        result = _insert_split_ruling(
            conn,
            document_id="dddddddd-0000-0000-0000-000000000002",
            case_id="cccccccc-0000-0000-0000-000000000001",
            court_id="tttttttt-0000-0000-0000-000000000001",
            judge_id=None,
            hearing_date="2026-03-15",
            ruling_text="Some ruling text",
            department="N15",
            outcome=None,
            motion_type=None,
        )
        assert result is False

    def test_savepoint_rollback_on_unique_violation(self) -> None:
        """On UniqueViolation, the savepoint is rolled back (not the transaction)."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        call_count = 0

        def side_effect(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise psycopg.errors.UniqueViolation("duplicate key")

        mock_cursor.execute.side_effect = side_effect

        _insert_split_ruling(
            conn,
            document_id="dddddddd-0000-0000-0000-000000000002",
            case_id="cccccccc-0000-0000-0000-000000000001",
            court_id="tttttttt-0000-0000-0000-000000000001",
            judge_id=None,
            hearing_date="2026-03-15",
            ruling_text="Some ruling text",
            department="N15",
            outcome=None,
            motion_type=None,
        )

        # Verify: SAVEPOINT, INSERT (raises), ROLLBACK TO SAVEPOINT
        execute_calls = mock_cursor.execute.call_args_list
        assert execute_calls[0] == call("SAVEPOINT insert_split_ruling")
        assert execute_calls[2] == call("ROLLBACK TO SAVEPOINT insert_split_ruling")

    def test_includes_ruling_text_hash_in_insert(self) -> None:
        """The INSERT includes ruling_text_hash for dedup."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _insert_split_ruling(
            conn,
            document_id="dddddddd-0000-0000-0000-000000000001",
            case_id="cccccccc-0000-0000-0000-000000000001",
            court_id="tttttttt-0000-0000-0000-000000000001",
            judge_id=None,
            hearing_date="2026-03-15",
            ruling_text="Some ruling text",
            department="N15",
            outcome=None,
            motion_type=None,
        )

        # The INSERT SQL should mention ruling_text_hash.
        insert_call = mock_cursor.execute.call_args_list[1]
        sql = insert_call[0][0]
        assert "ruling_text_hash" in sql


class TestApplySplitIdempotency:
    """Tests for apply_split skipping duplicates gracefully (#1233)."""

    def setup_method(self) -> None:
        self._saved_registry = dict(_splitter_registry)
        _splitter_registry.clear()

    def teardown_method(self) -> None:
        _splitter_registry.clear()
        _splitter_registry.update(self._saved_registry)

    @patch("backfill_split_rulings._insert_split_ruling")
    @patch("backfill_split_rulings._upsert_split_document")
    @patch("backfill_split_rulings._delete_original_ruling")
    def test_skips_duplicate_splits_and_still_deletes_original(
        self,
        mock_delete: MagicMock,
        mock_upsert_doc: MagicMock,
        mock_insert: MagicMock,
    ) -> None:
        """When all splits are duplicates, apply_split still deletes the original."""
        # _insert_split_ruling returns False for all splits (all are duplicates).
        mock_insert.return_value = False

        candidate = _make_candidate(
            ruling_id="rrrrrrrr-0000-0000-0000-000000000099",
            case_id="cccccccc-0000-0000-0000-000000000001",
            case_number="ORIG-001",
            case_title="A v. B",
        )
        splits = [
            SplitResult(ruling_text="Text A", case_title="A v. B"),
            SplitResult(ruling_text="Text B", case_title="A v. B"),
        ]

        conn = MagicMock()
        action = apply_split(conn, candidate, splits)

        # Original should still be deleted even though all splits were skipped.
        mock_delete.assert_called_once()
        delete_args = mock_delete.call_args[0]
        assert delete_args[1] == "rrrrrrrr-0000-0000-0000-000000000099"
        # Action should still report 2 splits.
        assert action.split_count == 2

    @patch("backfill_split_rulings._insert_split_ruling")
    @patch("backfill_split_rulings._upsert_split_document")
    @patch("backfill_split_rulings._delete_original_ruling")
    def test_partial_duplicates_inserts_new_ones(
        self,
        mock_delete: MagicMock,
        mock_upsert_doc: MagicMock,
        mock_insert: MagicMock,
    ) -> None:
        """When some splits are duplicates and some are new, new ones are inserted."""
        # First split is a duplicate, second is new.
        mock_insert.side_effect = [False, True]

        candidate = _make_candidate(
            case_number="ORIG-001",
            case_title="A v. B",
        )
        splits = [
            SplitResult(ruling_text="Text A (duplicate)", case_title="A v. B"),
            SplitResult(ruling_text="Text B (new)", case_title="A v. B"),
        ]

        action = apply_split(MagicMock(), candidate, splits)

        assert mock_insert.call_count == 2
        assert action.split_count == 2


# ---------------------------------------------------------------------------
# Missing ruling document detection tests (--check-missing)
# ---------------------------------------------------------------------------


class TestFindMissingRulingDocuments:
    """Tests for find_missing_ruling_documents()."""

    def test_returns_empty_when_no_missing(self) -> None:
        """When query returns no rows, returns empty list."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = find_missing_ruling_documents(conn)
        assert result == []

    def test_returns_documents_when_missing(self) -> None:
        """When query returns rows, returns MissingRulingDocument instances."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (
                "dddddddd-0000-0000-0000-000000000001",  # document_id
                "rulings/2026/03/15/doc.pdf",  # s3_key
                "judgemind-archive",  # s3_bucket
                "https://example.com/ruling.pdf",  # source_url
                "oc-tentatives",  # scraper_id
                "2026-03-15T10:00:00",  # captured_at
                "2026-03-15",  # hearing_date
                "CA",  # state
                "Orange",  # county
                "30-2024-01393434",  # case_number
                "Smith v. Jones",  # case_title
            ),
        ]
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = find_missing_ruling_documents(conn)
        assert len(result) == 1
        doc = result[0]
        assert doc.document_id == "dddddddd-0000-0000-0000-000000000001"
        assert doc.county == "Orange"
        assert doc.case_number == "30-2024-01393434"
        assert doc.s3_key == "rulings/2026/03/15/doc.pdf"

    def test_county_filter_passes_single_county(self) -> None:
        """When county is specified, query includes single county param."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        find_missing_ruling_documents(conn, county="Orange", limit=50)

        # Check the SQL was called with county param
        execute_call = mock_cursor.execute.call_args
        sql = execute_call[0][0]
        params = execute_call[0][1]
        assert "ct.county = %s" in sql
        assert params == ["Orange", 50]

    def test_no_county_uses_splittable_counties(self) -> None:
        """When no county specified, query uses IN clause for splittable counties."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        find_missing_ruling_documents(conn, county=None, limit=100)

        execute_call = mock_cursor.execute.call_args
        sql = execute_call[0][0]
        assert "ct.county IN" in sql

    def test_handles_null_fields_gracefully(self) -> None:
        """Null fields in the query result are handled gracefully."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (
                "dddddddd-0000-0000-0000-000000000002",
                "rulings/2026/03/15/doc.pdf",
                None,  # s3_bucket is None
                None,  # source_url is None
                None,  # scraper_id is None
                None,  # captured_at is None
                None,  # hearing_date is None
                "CA",
                "Orange",
                None,  # case_number is None
                None,  # case_title is None
            ),
        ]
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = find_missing_ruling_documents(conn)
        assert len(result) == 1
        doc = result[0]
        assert doc.s3_bucket is None
        assert doc.source_url == ""
        assert doc.hearing_date is None
        assert doc.case_number is None


class TestReportMissingRulings:
    """Tests for report_missing_rulings()."""

    @patch("backfill_split_rulings.find_missing_ruling_documents")
    @patch("psycopg.connect")
    def test_returns_zero_when_no_missing(
        self, mock_connect: MagicMock, mock_find: MagicMock
    ) -> None:
        """Returns 0 when no documents are missing rulings."""
        mock_find.return_value = []
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        count = report_missing_rulings("postgresql://test")
        assert count == 0

    @patch("backfill_split_rulings.find_missing_ruling_documents")
    @patch("psycopg.connect")
    def test_returns_count_when_missing(
        self, mock_connect: MagicMock, mock_find: MagicMock
    ) -> None:
        """Returns the count of missing-ruling documents."""
        mock_find.return_value = [
            MissingRulingDocument(
                document_id="d1",
                s3_key="key1",
                s3_bucket="bucket",
                source_url="https://example.com",
                scraper_id="oc-tentatives",
                captured_at="2026-03-15T10:00:00",
                hearing_date="2026-03-15",
                state="CA",
                county="Orange",
                case_number="30-2024-001",
                case_title="Smith v. Jones",
            ),
            MissingRulingDocument(
                document_id="d2",
                s3_key="key2",
                s3_bucket="bucket",
                source_url="https://example.com",
                scraper_id="oc-tentatives",
                captured_at="2026-03-16T10:00:00",
                hearing_date="2026-03-16",
                state="CA",
                county="Orange",
                case_number="30-2024-002",
                case_title="Doe v. Roe",
            ),
        ]
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        count = report_missing_rulings("postgresql://test")
        assert count == 2

    @patch("backfill_split_rulings.find_missing_ruling_documents")
    @patch("psycopg.connect")
    def test_passes_county_and_limit(self, mock_connect: MagicMock, mock_find: MagicMock) -> None:
        """County and limit are passed through to find_missing_ruling_documents."""
        mock_find.return_value = []
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        report_missing_rulings("postgresql://test", county="Orange", limit=50)

        mock_find.assert_called_once_with(mock_conn, county="Orange", limit=50)
