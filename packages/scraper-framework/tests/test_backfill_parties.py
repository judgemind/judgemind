"""Tests for the backfill_parties script.

All database access is mocked — these tests verify the extraction and
update logic without requiring a live database.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
backfill_parties = importlib.import_module("backfill_parties")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COURT_ID = str(uuid.uuid4())
_CREATED_AT = datetime(2026, 3, 1, 10, 0, 0)
_DEFAULT_CURSOR = (backfill_parties._CURSOR_MIN_TIMESTAMP, backfill_parties._CURSOR_MIN_UUID)


def _make_case_row(
    ruling_text: str,
    created_at: datetime = _CREATED_AT,
) -> tuple:
    """Return a tuple matching the FETCH_QUERY columns."""
    return (
        str(uuid.uuid4()),  # c.id
        ruling_text,
        _COURT_ID,
        created_at,  # c.created_at
    )


# ---------------------------------------------------------------------------
# extract_parties tests (text-based, no HTML)
# ---------------------------------------------------------------------------


class TestExtractParties:
    """Tests for text-based party extraction."""

    def test_caption_block_plaintiffs_defendants(self) -> None:
        """Extract parties from a formal caption block in ruling text."""
        text = (
            "SUMAYYA AASI, et al.,\n"
            "  Plaintiff(s),\n"
            "  vs.\n"
            "AMERICAN HONDA MOTOR CO., INC., et al.,\n"
            "  Defendant(s)."
        )
        parties = backfill_parties.extract_parties(text)
        assert len(parties) == 2

        names = {p["name"].lower() for p in parties}
        assert any("aasi" in n for n in names)
        assert any("honda" in n for n in names)

        roles = {p["role"] for p in parties}
        assert "plaintiff" in roles
        assert "defendant" in roles

    def test_caption_block_petitioner_respondent(self) -> None:
        """Extract parties with petitioner/respondent roles."""
        text = (
            "MIC-BRY8, LLC,\n"
            "  Petitioner(s),\n"
            "  vs.\n"
            "CERTAIN STATUTORY INTERESTED PARTIES,\n"
            "  Respondent(s)."
        )
        parties = backfill_parties.extract_parties(text)
        assert len(parties) == 2

        roles = {p["role"] for p in parties}
        assert "petitioner" in roles
        assert "respondent" in roles

    def test_moving_responding_fallback(self) -> None:
        """Extract from MOVING PARTY / RESPONDING PARTY fields."""
        text = (
            "MOVING PARTY: Defendant Rayne Dealership Corporation.\n"
            "RESPONDING PARTY: Plaintiffs Alpha Beta and Gamma Delta."
        )
        parties = backfill_parties.extract_parties(text)
        assert len(parties) >= 2

        roles = {p["role"] for p in parties}
        assert "moving_party" in roles
        assert "responding_party" in roles

    def test_no_parties_returns_empty(self) -> None:
        """Text with no party patterns returns empty list."""
        text = "The court sets a case management conference."
        parties = backfill_parties.extract_parties(text)
        assert parties == []

    def test_no_opposition_returns_empty(self) -> None:
        """'No opposition filed' returns empty list."""
        text = "MOVING PARTY: Defendant Big Corp.\nRESPONDING PARTY: No opposition filed."
        parties = backfill_parties.extract_parties(text)
        assert parties == []

    def test_corporate_suffix_not_split(self) -> None:
        """Corporate suffixes like 'Inc.' should stay with their entity name."""
        text = (
            "CALDERA, et al.,\n"
            "  Plaintiff(s),\n"
            "  vs.\n"
            "TECHNO-ADVANCED, INC., et al.,\n"
            "  Defendant(s)."
        )
        parties = backfill_parties.extract_parties(text)
        assert len(parties) == 2
        names = [p["name"].lower() for p in parties]
        # "Inc" should NOT appear as a standalone entry
        assert not any(n.strip().rstrip(".") == "inc" for n in names)
        # The defendant should contain both parts
        assert any("techno" in n and "inc" in n for n in names)

    def test_fragment_names_filtered(self) -> None:
        """Standalone fragments like 'Inc' or 'AB' should be filtered out."""
        assert backfill_parties._is_name_fragment("Inc") is True
        assert backfill_parties._is_name_fragment("LLC") is True
        assert backfill_parties._is_name_fragment("AB") is True
        assert backfill_parties._is_name_fragment("John Doe") is False


# ---------------------------------------------------------------------------
# backfill_batch tests
# ---------------------------------------------------------------------------


class TestBackfillBatch:
    """Tests for backfill_batch()."""

    def test_no_rows_returns_zero(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []

        processed, updated, next_cursor = backfill_parties.backfill_batch(
            conn, batch_size=10, cursor=_DEFAULT_CURSOR
        )
        assert processed == 0
        assert updated == 0
        assert next_cursor == _DEFAULT_CURSOR

    def test_extracts_and_creates_parties(self) -> None:
        """Ruling text with a caption block produces party records."""
        ruling_text = "JOHN DOE,\n  Plaintiff(s),\n  vs.\nJANE SMITH,\n  Defendant(s)."
        row = _make_case_row(ruling_text)

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]

        contexts = [cur_fetch]
        # We need cursors for each upsert_party (2 parties x 2 calls each)
        # and upsert_case_party (2 calls)
        for _ in range(6):
            ctx_mock = MagicMock()
            # upsert_party: no existing alias, then INSERT returns new id
            ctx_mock.fetchone.side_effect = [None, (str(uuid.uuid4()),)]
            contexts.append(ctx_mock)

        context_iter = iter(contexts)

        def cursor_ctx() -> MagicMock:
            ctx = MagicMock()
            cur = next(context_iter)
            ctx.__enter__ = MagicMock(return_value=cur)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        conn.cursor.side_effect = cursor_ctx

        processed, updated, _cursor = backfill_parties.backfill_batch(
            conn, batch_size=10, cursor=_DEFAULT_CURSOR
        )

        assert processed == 1
        assert updated == 1

    def test_skips_no_parties(self) -> None:
        """Ruling text with no extractable parties is skipped."""
        ruling_text = "The court sets a case management conference."
        row = _make_case_row(ruling_text)

        conn = MagicMock()
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]

        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur_fetch)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        processed, updated, _cursor = backfill_parties.backfill_batch(
            conn, batch_size=10, cursor=_DEFAULT_CURSOR
        )

        assert processed == 1
        assert updated == 0


# ---------------------------------------------------------------------------
# run_backfill tests
# ---------------------------------------------------------------------------


class TestRunBackfill:
    """Tests for run_backfill() end-to-end flow."""

    @patch("backfill_parties.psycopg")
    @patch("backfill_parties.backfill_batch")
    def test_dry_run_rolls_back(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = (datetime(2026, 3, 1), str(uuid.uuid4()))
        cursor_2 = (datetime(2026, 3, 2), str(uuid.uuid4()))
        mock_batch.side_effect = [(100, 80, cursor_1), (5, 3, cursor_2)]

        stats = backfill_parties.run_backfill("postgresql://test", batch_size=100, dry_run=True)

        assert mock_conn.rollback.call_count == 2
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 105
        assert stats["total_updated"] == 83

    @patch("backfill_parties.psycopg")
    @patch("backfill_parties.backfill_batch")
    def test_commits_per_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = (datetime(2026, 3, 1), str(uuid.uuid4()))
        cursor_2 = (datetime(2026, 3, 2), str(uuid.uuid4()))
        mock_batch.side_effect = [(100, 80, cursor_1), (30, 20, cursor_2)]

        stats = backfill_parties.run_backfill("postgresql://test", batch_size=100)

        assert mock_conn.commit.call_count == 2
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 130
        assert stats["total_updated"] == 100

    @patch("backfill_parties.psycopg")
    @patch("backfill_parties.backfill_batch")
    def test_limit_respected(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = (datetime(2026, 3, 1), str(uuid.uuid4()))
        mock_batch.side_effect = [(50, 40, cursor_1)]

        stats = backfill_parties.run_backfill("postgresql://test", batch_size=100, limit=50)

        call_args = mock_batch.call_args_list[0]
        assert call_args[0][1] == 50
        assert stats["total_processed"] == 50

    def test_query_uses_keyset_not_offset(self) -> None:
        """The query must use keyset pagination, not OFFSET."""
        assert "OFFSET" not in backfill_parties.FETCH_QUERY
        assert "(c.created_at, c.id) > (%s, %s)" in backfill_parties.FETCH_QUERY
