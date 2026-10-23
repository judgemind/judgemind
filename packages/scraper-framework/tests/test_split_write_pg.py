"""Real-Postgres tests for ``ingestion.db.write_split_rulings`` (#4845).

The worker writes a split set in one transaction: the key's existing ruling
rows are matched to the new children by content, so a ruling keeps its row
(and its ``public.alert_events``) however the split shifts.  These pin the
matching rules; ``test_split_invariants_pg.py`` checks the invariants over
random sequences.

Runs only when ``TEST_DATABASE_URL`` points at a migrated database.  Each
test runs in one rolled-back transaction.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date, datetime

import pytest

from ingestion.db import (
    SplitChildRuling,
    delete_stale_split_children,
    insert_document,
    upsert_case,
    upsert_court,
    write_split_rulings,
)
from ingestion.split_ids import derive_parent_document_id, make_split_document_id

_DSN = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def conn() -> Iterator[object]:
    import psycopg

    c = psycopg.connect(_DSN)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


class Key:
    def __init__(self, conn: object) -> None:
        self.conn = conn
        self.hash = uuid.uuid4().hex * 2
        self.s3_key = f"ca/test/superior_court/raw/{self.hash}.pdf"
        self.parent = derive_parent_document_id(self.hash)
        self.court_id = upsert_court(conn, "CA", f"SplitWrite {self.hash[:8]}", "Superior Court")

    def slot(self, i: int) -> str:
        return make_split_document_id(self.parent, i)

    def case(self, number: str | None, doc: str) -> str:
        return upsert_case(self.conn, number or f"UNKNOWN-{doc}", self.court_id)

    def write(
        self,
        rows: list[tuple[str | None, str] | None],
        *,
        relink: bool = True,
        protected: frozenset[str] = frozenset(),
    ) -> dict[str, str | None]:
        """Write the split ``[(case number, text), ...]`` like the worker.
        A ``None`` row is a textless slot the worker skips."""
        children = []
        for i, row in enumerate(rows):
            if row is None:
                continue
            number, text = row
            doc = self.slot(i) if len(rows) > 1 else self.parent
            case_id = self.case(number, doc)
            insert_document(
                self.conn,
                document_id=doc,
                case_id=case_id,
                court_id=self.court_id,
                content_format="pdf",
                content_hash=f"{self.hash}:{i}",
                s3_key=self.s3_key,
                s3_bucket="b",
                source_url="",
                scraper_id="t",
                captured_at=datetime(2026, 9, 1),
                hearing_date=date(2026, 9, 4),
            )
            children.append(
                SplitChildRuling(
                    document_id=doc,
                    case_id=case_id,
                    court_id=self.court_id,
                    hearing_date=date(2026, 9, 4),
                    ruling_text=text,
                    hearing_date_source="llm",
                )
            )
        result = write_split_rulings(
            self.conn,
            s3_key=self.s3_key,
            parent_document_id=self.parent,
            owns_key=True,
            children=children,
            relink=relink,
            protected_ids=protected,
        )
        delete_stale_split_children(
            self.conn,
            self.s3_key,
            [c.document_id for c in children] + sorted(protected),
            parent_document_id=self.parent,
            owns_key=True,
        )
        return result

    def rulings(self) -> dict[str, tuple[str, str, str, str]]:
        """``{document id: (ruling id, text, case number, doc status)}``."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT d.id::text, r.id::text, r.ruling_text, c.case_number, d.status::text "
                "FROM rulings r JOIN documents d ON d.id = r.document_id "
                "JOIN cases c ON c.id = r.case_id WHERE d.s3_key = %s",
                (self.s3_key,),
            )
            return {row[0]: tuple(row[1:]) for row in cur.fetchall()}

    def statuses(self) -> dict[str, str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, status::text FROM documents WHERE s3_key = %s", (self.s3_key,)
            )
            return dict(cur.fetchall())


def _text(i: int) -> str:
    return f"Ruling {i}. The motion is GRANTED as to count {i}."


def _alert(conn: object, ruling_id: str, document_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email) VALUES (%s) RETURNING id",
            (f"{uuid.uuid4().hex}@example.test",),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO alert_subscriptions (user_id, alert_type) "
            "VALUES (%s, (SELECT enum_range(NULL::alert_type))[1]) RETURNING id",
            (user_id,),
        )
        sub_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO alert_events (subscription_id, document_id, ruling_id) "
            "VALUES (%s, %s::uuid, %s::uuid) RETURNING id",
            (sub_id, document_id, ruling_id),
        )
        return str(cur.fetchone()[0])


def _alert_target(conn: object, alert_id: str) -> tuple[str | None, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ruling_id::text, document_id::text FROM alert_events WHERE id = %s::uuid",
            (alert_id,),
        )
        return cur.fetchone()


def test_shift_right_past_a_skipped_row_keeps_every_ruling_row(conn: object) -> None:
    """#4836: a textless row at slot 0 shifts every ruling one slot right.
    Each ruling keeps its row id and its alert, on its new slot."""
    key = Key(conn)
    key.write([("30-A-1", _text(1)), ("30-A-2", _text(2)), ("30-A-3", _text(3))])
    before = key.rulings()
    alerts = {doc: _alert(conn, rid, doc) for doc, (rid, *_rest) in before.items()}

    # The textless row at slot 0 is not written; rulings sit at slots 1..3.
    key.write([None, ("30-A-1", _text(1)), ("30-A-2", _text(2)), ("30-A-3", _text(3))])
    after = key.rulings()
    assert set(after) == {key.slot(1), key.slot(2), key.slot(3)}
    for i, slot in enumerate([key.slot(1), key.slot(2), key.slot(3)]):
        ruling_id, text, number, status = after[slot]
        old_doc = key.slot(i)
        assert text == _text(i + 1)
        assert number == f"30-A-{i + 1}"
        assert status == "active"
        assert ruling_id == before[old_doc][0]
        assert _alert_target(conn, alerts[old_doc]) == (ruling_id, slot)


def test_swap_moves_rows_through_a_cycle(conn: object) -> None:
    """Two rulings swap slots: both rows keep their ids (the move needs a
    scratch document to break the cycle) and no scratch row is left."""
    key = Key(conn)
    key.write([("30-B-1", _text(1)), ("30-B-2", _text(2))])
    before = key.rulings()
    key.write([("30-B-2", _text(2)), ("30-B-1", _text(1))])
    after = key.rulings()
    assert after[key.slot(0)][0] == before[key.slot(1)][0]
    assert after[key.slot(1)][0] == before[key.slot(0)][0]
    assert set(key.statuses()) == {key.slot(0), key.slot(1)}


def test_true_duplicate_within_the_split_supersedes_the_later_copy(conn: object) -> None:
    key = Key(conn)
    result = key.write([("30-C-1", _text(1)), ("30-C-1", _text(1))])
    assert result[key.slot(1)] is None
    assert key.statuses()[key.slot(1)] == "superseded"
    assert set(key.rulings()) == {key.slot(0)}
    # Re-running keeps the same winner.
    key.write([("30-C-1", _text(1)), ("30-C-1", _text(1))])
    assert set(key.rulings()) == {key.slot(0)}


def test_missed_case_number_keeps_the_real_case(conn: object) -> None:
    """#4788: a re-extraction that finds no case number keeps the ruling on
    the real case an earlier extraction of the same bytes found."""
    key = Key(conn)
    key.write([("30-D-1", _text(1)), ("30-D-2", _text(2))])
    key.write([(None, _text(1)), ("30-D-2", _text(2))])
    assert key.rulings()[key.slot(0)][2] == "30-D-1"


def test_new_real_case_relinks_only_on_replacement(conn: object) -> None:
    key = Key(conn)
    key.write([("30-E-1", _text(1)), ("30-E-2", _text(2))])
    key.write([("30-E-9", _text(1)), ("30-E-2", _text(2))], relink=False)
    assert key.rulings()[key.slot(0)][2] == "30-E-1"  # live: preserve-first
    key.write([("30-E-9", _text(1)), ("30-E-2", _text(2))], relink=True)
    assert key.rulings()[key.slot(0)][2] == "30-E-9"  # replacement: relink


def test_dropped_ruling_is_removed_and_its_alert_detached(conn: object) -> None:
    key = Key(conn)
    key.write([("30-F-1", _text(1)), ("30-F-2", _text(2))])
    ruling_id = key.rulings()[key.slot(1)][0]
    alert_id = _alert(conn, ruling_id, key.slot(1))
    key.write([("30-F-1", _text(1))])
    assert set(key.rulings()) == {key.parent}
    assert _alert_target(conn, alert_id)[0] is None  # detached, not deleted


def test_protected_slot_is_left_alone(conn: object) -> None:
    """A child whose extraction failed this pass keeps what it held."""
    key = Key(conn)
    key.write([("30-G-1", _text(1)), ("30-G-2", _text(2)), ("30-G-3", _text(3))])
    before = key.rulings()
    # Slot 1 failed: only slots 0 and 2 are written.
    children = [(0, "30-G-1", _text(1)), (2, "30-G-3", _text(3))]
    written = []
    for i, number, text in children:
        doc = key.slot(i)
        case_id = key.case(number, doc)
        written.append(
            SplitChildRuling(
                document_id=doc,
                case_id=case_id,
                court_id=key.court_id,
                hearing_date=date(2026, 9, 4),
                ruling_text=text,
                hearing_date_source="llm",
            )
        )
    write_split_rulings(
        conn,
        s3_key=key.s3_key,
        parent_document_id=key.parent,
        owns_key=True,
        children=written,
        relink=True,
        protected_ids=frozenset({key.slot(1)}),
    )
    delete_stale_split_children(
        conn,
        key.s3_key,
        [key.slot(0), key.slot(2), key.slot(1)],
        parent_document_id=key.parent,
        owns_key=True,
    )
    assert key.rulings()[key.slot(1)] == before[key.slot(1)]
