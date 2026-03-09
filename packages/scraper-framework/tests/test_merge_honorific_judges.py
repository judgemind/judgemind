"""Tests for the merge_honorific_judges script.

All database access is mocked — these tests verify the merge and rename
logic without requiring a live database.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
merge = importlib.import_module("merge_honorific_judges")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COURT_ID = str(uuid.uuid4())
_CURSOR_MIN = merge._CURSOR_MIN_UUID


def _make_cursor_ctx(cur: MagicMock) -> MagicMock:
    """Wrap a mock cursor in a context manager mock."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# merge_batch tests
# ---------------------------------------------------------------------------


class TestMergeBatch:
    """Tests for merge_batch()."""

    def test_no_rows_returns_zero(self) -> None:
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = _make_cursor_ctx(cur)
        cur.fetchall.return_value = []

        processed, merged, renamed, _cursor = merge.merge_batch(
            conn, batch_size=10, cursor=_CURSOR_MIN
        )
        assert processed == 0
        assert merged == 0
        assert renamed == 0

    def test_renames_when_no_merge_target(self) -> None:
        """Judge with prefix but no clean counterpart gets renamed in place."""
        judge_id = str(uuid.uuid4())
        row = (judge_id, "Hon. Joseph B. Widman", _COURT_ID)

        # Cursors: fetch -> find_target -> rename
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]

        cur_find = MagicMock()
        cur_find.fetchone.return_value = None  # no merge target

        cur_rename = MagicMock()

        cursors = iter([cur_fetch, cur_find, cur_rename])

        def cursor_factory() -> MagicMock:
            return _make_cursor_ctx(next(cursors))

        conn = MagicMock()
        conn.cursor.side_effect = cursor_factory

        processed, merged, renamed, _cursor = merge.merge_batch(
            conn, batch_size=10, cursor=_CURSOR_MIN
        )

        assert processed == 1
        assert merged == 0
        assert renamed == 1

        # Verify RENAME was called with normalized name
        rename_args = cur_rename.execute.call_args[0][1]
        assert rename_args[0] == "Joseph B. Widman"
        assert rename_args[1] == judge_id

    def test_merges_when_target_exists(self) -> None:
        """Judge with prefix merges into existing clean counterpart."""
        source_id = str(uuid.uuid4())
        target_id = str(uuid.uuid4())
        row = (source_id, "Judge Bobby P. Luna", _COURT_ID)

        # Cursors: fetch -> find_target -> count_rulings -> count_case_judges
        #          -> update_rulings -> delete_conflict_cj -> update_cj
        #          -> delete_dup_aliases -> update_aliases -> delete_judge
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]

        cur_find = MagicMock()
        cur_find.fetchone.return_value = (target_id,)

        cur_count_r = MagicMock()
        cur_count_r.fetchone.return_value = (5,)

        cur_count_cj = MagicMock()
        cur_count_cj.fetchone.return_value = (3,)

        cur_update_r = MagicMock()
        cur_del_cj = MagicMock()
        cur_update_cj = MagicMock()
        cur_del_alias = MagicMock()
        cur_update_alias = MagicMock()
        cur_del_judge = MagicMock()

        cursors = iter(
            [
                cur_fetch,
                cur_find,
                cur_count_r,
                cur_count_cj,
                cur_update_r,
                cur_del_cj,
                cur_update_cj,
                cur_del_alias,
                cur_update_alias,
                cur_del_judge,
            ]
        )

        def cursor_factory() -> MagicMock:
            return _make_cursor_ctx(next(cursors))

        conn = MagicMock()
        conn.cursor.side_effect = cursor_factory

        processed, merged, renamed, _cursor = merge.merge_batch(
            conn, batch_size=10, cursor=_CURSOR_MIN
        )

        assert processed == 1
        assert merged == 1
        assert renamed == 0

        # Verify rulings were reassigned
        update_r_args = cur_update_r.execute.call_args[0][1]
        assert update_r_args == (target_id, source_id)

        # Verify case_judges conflicts were deleted
        del_cj_args = cur_del_cj.execute.call_args[0][1]
        assert del_cj_args == (source_id, target_id)

        # Verify remaining case_judges were moved
        update_cj_args = cur_update_cj.execute.call_args[0][1]
        assert update_cj_args == (target_id, source_id)

        # Verify duplicate aliases were deleted then remaining moved
        del_alias_args = cur_del_alias.execute.call_args[0][1]
        assert del_alias_args == (source_id, target_id)
        update_alias_args = cur_update_alias.execute.call_args[0][1]
        assert update_alias_args == (target_id, source_id)

        # Verify source judge was deleted
        del_judge_args = cur_del_judge.execute.call_args[0][1]
        assert del_judge_args == (source_id,)

    def test_skips_when_normalization_unchanged(self) -> None:
        """Judge whose name doesn't change after normalization is skipped.

        This shouldn't normally happen because of the ILIKE filter, but
        the code handles it defensively.
        """
        # Construct a name that looks like it starts with "Hon." but
        # normalize_judge_name returns the same thing. In practice this
        # can't happen with the current regex, so we mock.
        judge_id = str(uuid.uuid4())
        row = (judge_id, "Hondorf Smith", _COURT_ID)

        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = [row]

        conn = MagicMock()
        conn.cursor.return_value = _make_cursor_ctx(cur_fetch)

        # "Hondorf Smith" won't match the honorific prefix regex, so
        # normalize_judge_name returns "Hondorf Smith" (title-cased).
        # But canonical_name is already "Hondorf Smith" — this won't
        # actually match ILIKE 'Hon.%' but we test the defensive check.
        with patch.object(merge, "normalize_judge_name", return_value="Hondorf Smith"):
            processed, merged, renamed, _cursor = merge.merge_batch(
                conn, batch_size=10, cursor=_CURSOR_MIN
            )

        assert processed == 1
        assert merged == 0
        assert renamed == 0

    def test_processes_multiple_judges_in_batch(self) -> None:
        """Multiple judges in a batch are each processed correctly."""
        judge1_id = str(uuid.uuid4())
        judge2_id = str(uuid.uuid4())
        target2_id = str(uuid.uuid4())

        rows = [
            (judge1_id, "Honorable Jane Doe", _COURT_ID),
            (judge2_id, "The Honorable John Smith", _COURT_ID),
        ]

        # Judge 1: rename (no target)
        # Judge 2: merge (target exists)
        cur_fetch = MagicMock()
        cur_fetch.fetchall.return_value = rows

        # Judge 1: find_target (None) -> rename
        cur_find1 = MagicMock()
        cur_find1.fetchone.return_value = None
        cur_rename1 = MagicMock()

        # Judge 2: find_target -> count_r -> count_cj -> update_r
        #          -> del_cj -> update_cj -> del_alias -> update_alias -> del_judge
        cur_find2 = MagicMock()
        cur_find2.fetchone.return_value = (target2_id,)
        cur_count_r = MagicMock()
        cur_count_r.fetchone.return_value = (2,)
        cur_count_cj = MagicMock()
        cur_count_cj.fetchone.return_value = (1,)
        cur_update_r = MagicMock()
        cur_del_cj = MagicMock()
        cur_update_cj = MagicMock()
        cur_del_alias = MagicMock()
        cur_update_alias = MagicMock()
        cur_del_judge = MagicMock()

        cursors = iter(
            [
                cur_fetch,
                cur_find1,
                cur_rename1,
                cur_find2,
                cur_count_r,
                cur_count_cj,
                cur_update_r,
                cur_del_cj,
                cur_update_cj,
                cur_del_alias,
                cur_update_alias,
                cur_del_judge,
            ]
        )

        def cursor_factory() -> MagicMock:
            return _make_cursor_ctx(next(cursors))

        conn = MagicMock()
        conn.cursor.side_effect = cursor_factory

        processed, merged, renamed, cursor = merge.merge_batch(
            conn, batch_size=10, cursor=_CURSOR_MIN
        )

        assert processed == 2
        assert merged == 1
        assert renamed == 1
        assert cursor == judge2_id  # last processed judge


# ---------------------------------------------------------------------------
# run_merge tests
# ---------------------------------------------------------------------------


class TestRunMerge:
    """Tests for run_merge() end-to-end flow."""

    @patch("merge_honorific_judges.psycopg")
    @patch("merge_honorific_judges.merge_batch")
    def test_dry_run_rolls_back(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = str(uuid.uuid4())
        cursor_2 = str(uuid.uuid4())
        mock_batch.side_effect = [
            (10, 5, 3, cursor_1),
            (2, 1, 0, cursor_2),
        ]

        stats = merge.run_merge("postgresql://test", batch_size=10, dry_run=True)

        assert mock_conn.rollback.call_count == 2
        mock_conn.commit.assert_not_called()
        assert stats["total_processed"] == 12
        assert stats["total_merged"] == 6
        assert stats["total_renamed"] == 3

    @patch("merge_honorific_judges.psycopg")
    @patch("merge_honorific_judges.merge_batch")
    def test_commits_per_batch(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = str(uuid.uuid4())
        mock_batch.side_effect = [(5, 3, 2, cursor_1)]

        stats = merge.run_merge("postgresql://test", batch_size=100)

        assert mock_conn.commit.call_count == 1
        mock_conn.rollback.assert_not_called()
        assert stats["total_processed"] == 5
        assert stats["total_merged"] == 3
        assert stats["total_renamed"] == 2

    @patch("merge_honorific_judges.psycopg")
    @patch("merge_honorific_judges.merge_batch")
    def test_limit_respected(
        self,
        mock_batch: MagicMock,
        mock_psycopg: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value = mock_conn

        cursor_1 = str(uuid.uuid4())
        mock_batch.side_effect = [(20, 10, 5, cursor_1)]

        stats = merge.run_merge("postgresql://test", batch_size=100, limit=20)

        # effective_batch should have been capped to 20
        batch_call = mock_batch.call_args_list[0]
        assert batch_call[0][1] == 20
        assert stats["total_processed"] == 20

    def test_query_uses_keyset_not_offset(self) -> None:
        """The query must use keyset pagination, not OFFSET."""
        assert "OFFSET" not in merge.FETCH_HONORIFIC_JUDGES_QUERY
        assert "j.id > %s::uuid" in merge.FETCH_HONORIFIC_JUDGES_QUERY


class TestNormalizationIntegration:
    """Verify normalize_judge_name strips the prefixes the script targets."""

    def test_hon_dot_stripped(self) -> None:
        from ingestion.db import normalize_judge_name

        assert normalize_judge_name("Hon. Joseph B. Widman") == "Joseph B. Widman"

    def test_judge_stripped(self) -> None:
        from ingestion.db import normalize_judge_name

        assert normalize_judge_name("Judge Bobby P. Luna") == "Bobby P. Luna"

    def test_honorable_stripped(self) -> None:
        from ingestion.db import normalize_judge_name

        assert normalize_judge_name("Honorable Jane Doe") == "Jane Doe"

    def test_the_honorable_stripped(self) -> None:
        from ingestion.db import normalize_judge_name

        assert normalize_judge_name("The Honorable John Smith") == "John Smith"

    def test_hon_without_dot_stripped(self) -> None:
        from ingestion.db import normalize_judge_name

        assert normalize_judge_name("Hon Joseph B. Widman") == "Joseph B. Widman"

    def test_already_clean_unchanged(self) -> None:
        from ingestion.db import normalize_judge_name

        assert normalize_judge_name("Joseph B. Widman") == "Joseph B. Widman"
