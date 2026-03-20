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
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

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
SplitAction = backfill_mod.SplitAction
build_candidate_query = backfill_mod.build_candidate_query
try_split = backfill_mod.try_split
apply_split = backfill_mod.apply_split
process_batch = backfill_mod.process_batch
fetch_candidates = backfill_mod.fetch_candidates
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
        query, has_single = build_candidate_query(county="Orange")
        assert has_single is True
        assert "ct.county = %s" in query

    def test_without_county(self) -> None:
        """When no county is specified, query includes IN clause for all splittable counties."""
        query, has_single = build_candidate_query(county=None)
        assert has_single is False
        assert "ct.county IN" in query

    def test_query_orders_by_cursor(self) -> None:
        """Query uses cursor-based pagination with (created_at, id) ordering."""
        query, _ = build_candidate_query()
        assert "ORDER BY r.created_at, r.id" in query

    def test_query_has_length_filter(self) -> None:
        """Query filters by minimum ruling_text length."""
        query, _ = build_candidate_query()
        assert "LENGTH(r.ruling_text)" in query


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

    def test_uses_original_case_when_no_split_case_number(self) -> None:
        """When split has no case_number, uses the original case_id."""
        candidate = _make_candidate(
            case_id="original-case-id",
            case_number="ORIG-001",
        )
        splits = [
            SplitResult(ruling_text="Text A", case_title="A v. B"),
            SplitResult(ruling_text="Text B", case_title="C v. D"),
        ]

        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        action = apply_split(conn, candidate, splits)

        # Case numbers should fall back to original.
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
