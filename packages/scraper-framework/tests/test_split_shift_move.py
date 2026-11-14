"""Unit tests for a scraper pre-split child's split set (#4820, #4967).

A pre-split child arrives as its own event and is written by
``write_split_rulings`` as a split set of one.  The row-level behaviour (no
ruling lost to a shifted split, alerts follow their own ruling) is pinned
against a real schema in ``test_split_shift_move_pg.py``.  These cover the
decision logic and the worker plumbing without a database.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from ingestion.db import SplitSet
from ingestion.split_ids import make_split_document_id, split_child_document_id
from ingestion.worker import IngestionWorker, split_set_for_event

_HASH = "ab" * 32
_KEY = f"ca/orange/superior_court/raw/{_HASH}.pdf"
_PARENT = str(uuid.uuid5(uuid.NAMESPACE_URL, _HASH))
_KIDS = tuple(split_child_document_id(_PARENT, i, 3) for i in range(3))
_SPLIT = SplitSet(parent_document_id=_PARENT, child_ids=_KIDS)


class TestWrittenBefore:
    def test_first_child_has_written_none(self) -> None:
        assert _SPLIT.written_before(_KIDS[0]) == frozenset()

    def test_later_child_has_written_the_earlier_slots(self) -> None:
        assert _SPLIT.written_before(_KIDS[2]) == frozenset(_KIDS[:2])

    def test_document_outside_the_split_takes_from_no_sibling(self) -> None:
        stray = make_split_document_id(_PARENT, 7)
        assert _SPLIT.written_before(stray) == frozenset(_KIDS)


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


def _worker() -> IngestionWorker:
    with patch("ingestion.worker.create_llm_client", return_value=None):
        worker = IngestionWorker(
            redis_client=MagicMock(),
            pg_dsn="postgresql://unused",
            opensearch_client=MagicMock(),
            s3_client=MagicMock(),
            archive_bucket="b",
        )
    worker._indexer = MagicMock()
    return worker


def _write_presplit_batch(worker: IngestionWorker, document_id: str) -> dict[str, Any]:
    """Run ``_write_split_batch`` for one pre-split child with the database
    calls mocked; return ``write_split_rulings``' keyword arguments."""
    from ingestion.worker import _SplitBatch

    batch = _SplitBatch(
        parent_document_id=_PARENT,
        children=[MagicMock(document_id=document_id, judge_vote=None, event_data={})],
        keep_ids=set(_SPLIT.written_before(document_id)),
        replace=True,
        pre_split=_SPLIT,
    )
    event = {"s3_key": _KEY, "content_hash": _HASH}

    def fake_write(_conn: object, **kwargs: Any) -> dict[str, str | None]:
        kwargs["vacated_ids"].append(_KIDS[2])
        return {document_id: "case-1"}

    with (
        patch("ingestion.worker.write_split_rulings", side_effect=fake_write) as write,
        patch("ingestion.worker.delete_stale_split_children") as cleanup,
        patch("ingestion.worker.insert_document"),
        patch("ingestion.worker.record_judge_votes"),
        patch.object(worker, "_get_connection", return_value=MagicMock()),
        patch.object(worker, "_prepare_ruling_write", return_value=("court", "case-1", None)),
        patch.object(worker, "_link_ruling_rows"),
        patch.object(worker, "_index_committed_ruling") as index,
        patch.object(worker, "_reindex_dependent_rulings"),
    ):
        worker._write_split_batch(event, _PARENT, batch)
    cleanup.assert_not_called()
    index.assert_called_once()
    return write.call_args.kwargs


class TestPreSplitBatchWrite:
    def test_replaces_only_its_own_slot(self) -> None:
        worker = _worker()
        kwargs = _write_presplit_batch(worker, _KIDS[1])
        assert kwargs["retire_ids"] == frozenset({_KIDS[1]})
        assert kwargs["protected_ids"] == frozenset({_KIDS[0]})
        assert kwargs["owns_key"] is True
        assert kwargs["relink"] is False

    def test_vacated_sibling_leaves_search(self) -> None:
        worker = _worker()
        _write_presplit_batch(worker, _KIDS[0])
        worker._indexer.delete_documents.assert_called_once_with([_KIDS[2]])
