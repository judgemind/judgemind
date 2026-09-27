"""Unit tests for the #4820 split-sibling ruling move.

The row-level behaviour (no ruling ever missing, alerts stay attached) is
pinned against a real schema in ``test_split_shift_move_pg.py``.  These cover
the decision logic and the worker plumbing without a database.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from ingestion.db import (
    SplitSet,
    _content_parent_of_key,
    _is_unwritten_split_slot,
    _move_split_sibling_ruling,
    insert_document_and_ruling,
)
from ingestion.split_ids import make_split_document_id, split_child_document_id
from ingestion.worker import split_set_for_event

_HASH = "ab" * 32
_KEY = f"ca/orange/superior_court/raw/{_HASH}.pdf"
_PARENT = str(uuid.uuid5(uuid.NAMESPACE_URL, _HASH))
_KIDS = tuple(split_child_document_id(_PARENT, i, 3) for i in range(3))
_SPLIT = SplitSet(parent_document_id=_PARENT, child_ids=_KIDS)


def _conn(fetchone: object = None) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = fetchone
    cur.rowcount = 1
    return conn, cur


class TestContentParentOfKey:
    def test_content_addressed_key(self) -> None:
        assert _content_parent_of_key(_KEY) == _PARENT

    def test_other_keys(self) -> None:
        assert _content_parent_of_key("ca/orange/raw/abc123.pdf") is None
        assert _content_parent_of_key(f"ca/x/{'z' * 64}.pdf") is None


class TestIsUnwrittenSplitSlot:
    def test_later_slot_is_unwritten(self) -> None:
        assert _is_unwritten_split_slot(_KIDS[2], _KIDS[0], _KEY, _SPLIT)

    def test_earlier_slot_was_written(self) -> None:
        assert not _is_unwritten_split_slot(_KIDS[0], _KIDS[2], _KEY, _SPLIT)

    def test_stale_slot_of_this_parent(self) -> None:
        stale = make_split_document_id(_PARENT, 3)
        assert _is_unwritten_split_slot(stale, _KIDS[1], "k/other.pdf", _SPLIT)

    def test_any_v5_row_when_parent_owns_the_key(self) -> None:
        grandchild = make_split_document_id(_KIDS[0], 1)
        assert _is_unwritten_split_slot(grandchild, _KIDS[1], _KEY, _SPLIT)

    def test_foreign_row_is_not_moved(self) -> None:
        grandchild = make_split_document_id(_KIDS[0], 1)
        assert not _is_unwritten_split_slot(grandchild, _KIDS[1], "k/other.pdf", _SPLIT)
        assert not _is_unwritten_split_slot(str(uuid.uuid4()), _KIDS[1], _KEY, _SPLIT)
        assert not _is_unwritten_split_slot("not-a-uuid", _KIDS[1], _KEY, _SPLIT)


class TestMoveSplitSiblingRuling:
    def _move(self, conn: MagicMock, **overrides: object) -> bool:
        kwargs: dict = {
            "document_id": _KIDS[0],
            "case_id": "case-1",
            "text_hash": "h" * 64,
            "s3_key": _KEY,
            "split_set": _SPLIT,
        }
        kwargs.update(overrides)
        return _move_split_sibling_ruling(conn, **kwargs)

    def test_nothing_to_look_up(self) -> None:
        conn, cur = _conn()
        assert not self._move(conn, text_hash=None)
        assert not self._move(conn, s3_key=None)
        assert not self._move(conn, document_id=str(uuid.uuid4()))
        cur.execute.assert_not_called()

    def test_no_holder(self) -> None:
        conn, cur = _conn(None)
        assert not self._move(conn)
        assert cur.execute.call_count == 1

    def test_holder_already_written_keeps_dedup(self) -> None:
        conn, cur = _conn(("ruling-1", _KIDS[0]))
        assert not self._move(conn, document_id=_KIDS[2])
        assert cur.execute.call_count == 1

    def test_moves_from_a_later_slot(self) -> None:
        conn, cur = _conn(("ruling-1", _KIDS[1]))
        assert self._move(conn)
        sql = [c.args[0] for c in cur.execute.call_args_list[1:]]
        assert sql[0].startswith("UPDATE alert_events SET ruling_id = NULL")
        assert sql[1].startswith("DELETE FROM rulings")
        assert sql[2].startswith("UPDATE rulings SET document_id")
        assert cur.execute.call_args_list[3].args[1] == (_KIDS[0], "ruling-1")
        assert sql[3].startswith("UPDATE alert_events SET document_id")
        assert "case_id" in sql[4]
        assert "status = 'superseded'" in sql[5]
        assert cur.execute.call_args_list[6].args[1] == (_KIDS[0], _KIDS[1])


def _write(conn: MagicMock, split_set: SplitSet | None) -> None:
    insert_document_and_ruling(
        conn,
        document_id=_KIDS[0],
        case_id="case-1",
        court_id="court-1",
        content_format="pdf",
        content_hash=_HASH,
        s3_key=_KEY,
        s3_bucket="b",
        source_url="",
        scraper_id="t",
        captured_at=datetime(2026, 9, 1),
        hearing_date=date(2026, 9, 5),
        ruling_text="The motion is GRANTED.",
        split_set=split_set,
    )


class TestInsertDocumentAndRulingSplitSet:
    def test_split_child_tries_the_move(self) -> None:
        conn, _ = _conn((True,))
        with patch("ingestion.db._move_split_sibling_ruling", return_value=False) as move:
            _write(conn, _SPLIT)
        assert move.call_count == 1
        assert move.call_args.kwargs["split_set"] is _SPLIT
        assert move.call_args.kwargs["text_hash"]

    def test_no_split_set_no_move(self) -> None:
        conn, _ = _conn((True,))
        with patch("ingestion.db._move_split_sibling_ruling") as move:
            _write(conn, None)
        move.assert_not_called()


class TestSplitSetForEvent:
    def test_split_child(self) -> None:
        event = {
            "document_id": _KIDS[1],
            "_split_processed": True,
            "_original_document_id": _PARENT,
            "_split_index": 1,
            "_split_count": 3,
        }
        assert split_set_for_event(event) == _SPLIT

    def test_not_a_split_child(self) -> None:
        assert split_set_for_event({"document_id": _PARENT}) is None
        assert split_set_for_event({"document_id": _PARENT, "_split_processed": True}) is None
