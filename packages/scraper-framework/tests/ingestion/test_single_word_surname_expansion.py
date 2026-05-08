"""Tests for ``_expand_single_word_judge_surname`` (#4297).

The helper exists because LA tentative-ruling HTML uses a
``JUDGE/DEPT: <Surname>/<dept>`` form-layout header that genuinely carries
only the surname.  ``resolve_judge`` rejects single-word names via
``_looks_like_valid_judge_name`` (defensive guard), so without expansion
the ruling stores ``judge_id = NULL``.

Coverage matrix:
  - Directory-snapshot match (happy path) — surname matches the directory
    canonical name's last word -> return canonical name.
  - Directory mismatch + exactly one judges-table match -> expand.
  - Directory mismatch + zero judges-table matches -> return None
    (caller falls back to current single-word-rejected NULL).
  - Directory mismatch + multiple judges-table matches -> return None
    (ambiguous).
  - No directory snapshot at all + exactly one judges-table match ->
    expand (the surname-suffix lookup still runs).
  - Defensive guards: empty / whitespace / multi-word input -> None.
  - DB error -> swallowed, returns None.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from ingestion.db import _expand_single_word_judge_surname


def _make_mock_conn() -> tuple[MagicMock, MagicMock]:
    """Create a mock psycopg connection with cursor context manager."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


class TestSingleWordGuard:
    """Defensive guards against non-single-word inputs."""

    def test_empty_string_returns_none(self) -> None:
        mock_conn, _ = _make_mock_conn()
        result = _expand_single_word_judge_surname(
            mock_conn, "court-uuid", "", "25", hearing_date=date(2026, 5, 1)
        )
        assert result is None

    def test_whitespace_only_returns_none(self) -> None:
        mock_conn, _ = _make_mock_conn()
        result = _expand_single_word_judge_surname(
            mock_conn, "court-uuid", "   ", "25", hearing_date=date(2026, 5, 1)
        )
        assert result is None

    def test_multi_word_input_returns_none(self) -> None:
        """A two-word input is not a single-word surname — the helper
        is scoped to the JUDGE/DEPT bug class only."""
        mock_conn, _ = _make_mock_conn()
        result = _expand_single_word_judge_surname(
            mock_conn,
            "court-uuid",
            "Karine Mkrtchyan",
            "25",
            hearing_date=date(2026, 5, 1),
        )
        assert result is None


class TestDirectoryMatch:
    """Happy path: directory snapshot canonical surname matches input."""

    def test_directory_surname_matches_input(self) -> None:
        """Directory says ``Karine Mkrtchyan`` for dept 25; input is
        ``Mkrtchyan``.  Last-word match → return ``Karine Mkrtchyan``."""
        mock_conn, mock_cur = _make_mock_conn()

        # Step 2 inside resolve_judge_from_department:
        #   - SELECT court_code FROM courts -> ('ca-los-angeles',)
        #   - SELECT mapping FROM court_directory_snapshots -> ({"25": "Karine Mkrtchyan"},)
        # Step 4 (judges fallback) is NOT reached because directory matches.
        mock_cur.fetchone.side_effect = [
            ("ca-los-angeles",),
            ({"25": "Karine Mkrtchyan"},),
        ]

        result = _expand_single_word_judge_surname(
            mock_conn,
            "court-uuid-la",
            "Mkrtchyan",
            "25",
            hearing_date=date(2026, 5, 1),
        )

        assert result == "Karine Mkrtchyan"

    def test_directory_match_case_insensitive(self) -> None:
        """Directory canonical may be in any case; surname compare is lower."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [
            ("ca-los-angeles",),
            ({"25": "KARINE MKRTCHYAN"},),
        ]

        result = _expand_single_word_judge_surname(mock_conn, "court-uuid-la", "mkrtchyan", "25")

        assert result == "KARINE MKRTCHYAN"


class TestDirectoryMismatchSingleJudgesMatch:
    """Mismatch path with exactly one judges-table match."""

    def test_exactly_one_judge_match_expands(self) -> None:
        """Directory says Byrdsong for dept 25 (primary assignment).  The
        document says Mkrtchyan.  judges has exactly one ``... Mkrtchyan``
        at this court → expand to that canonical name."""
        mock_conn, mock_cur = _make_mock_conn()

        # Step 2:
        #   - court_code lookup -> ('ca-los-angeles',)
        #   - snapshot mapping -> ({"25": "Latrice A. G. Byrdsong"},)
        # Step 4:
        #   - judges suffix lookup -> [("Karine Mkrtchyan",)]
        mock_cur.fetchone.side_effect = [
            ("ca-los-angeles",),
            ({"25": "Latrice A. G. Byrdsong"},),
        ]
        mock_cur.fetchall.side_effect = [
            [("Karine Mkrtchyan",)],
        ]

        result = _expand_single_word_judge_surname(
            mock_conn,
            "court-uuid-la",
            "Mkrtchyan",
            "25",
            hearing_date=date(2026, 5, 1),
        )

        assert result == "Karine Mkrtchyan"


class TestDirectoryMismatchZeroOrMultipleMatches:
    """Mismatch path with zero or multiple judges-table matches."""

    def test_zero_judge_matches_returns_none(self) -> None:
        """Directory says different surname; no judge with this surname at
        this court → None (caller falls back to current NULL behavior)."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [
            ("ca-los-angeles",),
            ({"25": "Latrice A. G. Byrdsong"},),
        ]
        mock_cur.fetchall.side_effect = [[]]

        result = _expand_single_word_judge_surname(mock_conn, "court-uuid-la", "Mkrtchyan", "25")

        assert result is None

    def test_multiple_judge_matches_returns_none(self) -> None:
        """Two judges with the same surname at the same court → ambiguous,
        return None."""
        mock_conn, mock_cur = _make_mock_conn()
        mock_cur.fetchone.side_effect = [
            ("ca-los-angeles",),
            ({"25": "Latrice A. G. Byrdsong"},),
        ]
        mock_cur.fetchall.side_effect = [
            [("Karine Mkrtchyan",), ("Other Mkrtchyan",)],
        ]

        result = _expand_single_word_judge_surname(mock_conn, "court-uuid-la", "Mkrtchyan", "25")

        assert result is None


class TestNoDirectorySnapshot:
    """When the directory snapshot is missing entirely, the surname-suffix
    lookup still runs and can expand if exactly one match exists."""

    def test_no_snapshot_falls_through_to_judges_lookup(self) -> None:
        """No court_directory_snapshots row at all — but exactly one judge
        in the judges table matches the surname suffix → expand."""
        mock_conn, mock_cur = _make_mock_conn()

        # resolve_judge_from_department fetchone sequence:
        #   - court_code lookup -> ('ca-los-angeles',)
        #   - hearing_date snapshot lookup -> None (no snapshot)
        #   - earliest snapshot fallback -> None
        # then helper Step 4 judges suffix lookup
        mock_cur.fetchone.side_effect = [
            ("ca-los-angeles",),
            None,
            None,
        ]
        mock_cur.fetchall.side_effect = [
            [("Karine Mkrtchyan",)],
        ]

        result = _expand_single_word_judge_surname(
            mock_conn,
            "court-uuid-la",
            "Mkrtchyan",
            "25",
            hearing_date=date(2026, 5, 1),
        )

        assert result == "Karine Mkrtchyan"


class TestDbError:
    """DB errors are swallowed — the helper is best-effort."""

    def test_db_error_returns_none(self) -> None:
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = RuntimeError("connection lost")

        result = _expand_single_word_judge_surname(mock_conn, "court-uuid", "Mkrtchyan", "25")

        assert result is None
