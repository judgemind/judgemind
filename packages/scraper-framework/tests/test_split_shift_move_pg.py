"""Real-Postgres regression tests for #4820: a split set that shifts by one
position moves each ruling to its new slot instead of cascading content-hash
dedup supersedes.

Found verifying #4815 on dev.  An Orange key's stored split put ruling k at
slot k+1 (a stale 13-way split with a calendar-preamble row at slot 0).  The
first re-split into the correct 12 rulings wrote slot k's text while slot
k+1 still held it, so every child lost dedup to its right-hand neighbour,
was superseded, and had its ruling row deleted: 10 of 12 rulings were gone
until the next run.

Each write below stands for one committed child event, so the checks after
every write are the states a reader can see between commits.

Runs only when ``TEST_DATABASE_URL`` points at a migrated database (see
``test_split_relink_pg.py``).  Each test runs in one rolled-back transaction.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date, datetime

import pytest

from ingestion.db import (
    SplitSet,
    delete_stale_split_children,
    insert_document_and_ruling,
    upsert_case,
    upsert_court,
)
from ingestion.split_ids import split_child_document_id

_DSN = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="TEST_DATABASE_URL not set")

_N = 5


@pytest.fixture
def conn() -> Iterator[object]:
    import psycopg

    c = psycopg.connect(_DSN)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _text(i: int) -> str:
    return f"Ruling {i}. The motion is GRANTED as to count {i} only."


def _write(
    conn: object,
    *,
    document_id: str,
    case_id: str,
    court_id: str,
    s3_key: str,
    text: str,
    relink_case: bool = False,
    split_set: SplitSet | None = None,
) -> None:
    insert_document_and_ruling(
        conn,
        document_id=document_id,
        case_id=case_id,
        court_id=court_id,
        content_format="pdf",
        content_hash="a" * 64,
        s3_key=s3_key,
        s3_bucket="test-bucket",
        source_url="",
        scraper_id="test-4820",
        captured_at=datetime(2026, 9, 1),
        hearing_date=date(2026, 9, 5),
        ruling_text=text,
        relink_case=relink_case,
        split_set=split_set,
    )


def _alert_on(conn: object, document_id: str) -> tuple[str, str]:
    """Create a user alert on *document_id*'s ruling; return (alert id, ruling id)."""
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
        cur.execute("SELECT id FROM rulings WHERE document_id = %s::uuid", (document_id,))
        ruling_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO alert_events (subscription_id, document_id, ruling_id) "
            "VALUES (%s, %s::uuid, %s) RETURNING id",
            (sub_id, document_id, ruling_id),
        )
        return str(cur.fetchone()[0]), str(ruling_id)


def _rulings_by_text(conn: object, s3_key: str) -> dict[str, list[tuple]]:
    """Map ruling text -> [(document id, case id, doc status)] on *s3_key*."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.ruling_text, d.id::text, r.case_id::text, d.status::text "
            "FROM rulings r JOIN documents d ON d.id = r.document_id "
            "WHERE d.s3_key = %s",
            (s3_key,),
        )
        out: dict[str, list[tuple]] = {}
        for text, doc_id, case_id, status in cur.fetchall():
            out.setdefault(text, []).append((doc_id, case_id, status))
        return out


def _shifted_key(conn: object, tag: str) -> tuple[str, str, str, list[str], dict[str, str]]:
    """Store a shifted split: slot 0 holds a preamble, slot i+1 holds ruling i.

    Returns (court id, s3_key, parent id, case ids, {ruling id: alert id}).
    """
    court_id = upsert_court(conn, "CA", "Test County 4820", "Superior Court")
    key_hash = uuid.uuid4().hex * 2
    s3_key = f"ca/orange/superior_court/raw/{key_hash}.pdf"
    parent = str(uuid.uuid5(uuid.NAMESPACE_URL, key_hash))
    cases = [upsert_case(conn, f"2026-{tag}-{i:05d}", court_id) for i in range(_N)]
    preamble_case = upsert_case(conn, f"2026-{tag}-PREAMBLE", court_id)

    old = [split_child_document_id(parent, i, _N + 1) for i in range(_N + 1)]
    _write(
        conn,
        document_id=old[0],
        case_id=preamble_case,
        court_id=court_id,
        s3_key=s3_key,
        text="CIVIL CALENDAR. Tentative rulings follow.",
    )
    alerts: dict[str, str] = {}
    for i in range(_N):
        _write(
            conn,
            document_id=old[i + 1],
            case_id=cases[i],
            court_id=court_id,
            s3_key=s3_key,
            text=_text(i),
        )
        alert_id, ruling_id = _alert_on(conn, old[i + 1])
        alerts[ruling_id] = alert_id
    return court_id, s3_key, parent, cases, alerts


def _assert_every_ruling_present(conn: object, s3_key: str) -> None:
    by_text = _rulings_by_text(conn, s3_key)
    for i in range(_N):
        assert len(by_text.get(_text(i), [])) == 1, f"ruling {i} missing: {by_text}"


def _resplit(conn: object, *, relink_case: bool, tag: str) -> None:
    court_id, s3_key, parent, cases, alerts = _shifted_key(conn, tag)
    new = [split_child_document_id(parent, i, _N) for i in range(_N)]
    split_set = SplitSet(parent_document_id=parent, child_ids=tuple(new))

    for i in range(_N):
        _write(
            conn,
            document_id=new[i],
            case_id=cases[i],
            court_id=court_id,
            s3_key=s3_key,
            text=_text(i),
            relink_case=relink_case,
            split_set=split_set,
        )
        _assert_every_ruling_present(conn, s3_key)

    # The worker removes the rows the new split no longer writes after the
    # children are written (#4820), so the stale slot's ruling can move.
    delete_stale_split_children(conn, s3_key, new, parent_document_id=parent, owns_key=True)
    _assert_every_ruling_present(conn, s3_key)

    by_text = _rulings_by_text(conn, s3_key)
    for i in range(_N):
        assert by_text[_text(i)] == [(new[i], cases[i], "active")]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM documents WHERE s3_key = %s AND status = 'active'", (s3_key,)
        )
        assert cur.fetchone()[0] == _N
        # Each alert still points at its ruling, and at the slot that now
        # holds it.
        for ruling_id, alert_id in alerts.items():
            cur.execute(
                "SELECT a.ruling_id::text, a.document_id::text, r.document_id::text "
                "FROM alert_events a LEFT JOIN rulings r ON r.id = a.ruling_id "
                "WHERE a.id = %s::uuid",
                (alert_id,),
            )
            got_ruling, got_doc, ruling_doc = cur.fetchone()
            assert got_ruling == ruling_id
            assert got_doc == ruling_doc
        cur.execute(
            "SELECT count(*) FROM documents WHERE s3_key = %s AND status = 'superseded' "
            "AND EXISTS (SELECT 1 FROM rulings r WHERE r.document_id = documents.id)",
            (s3_key,),
        )
        assert cur.fetchone()[0] == 0


def test_split_shift_no_ruling_loss_reingest(conn: object) -> None:
    """Prefix reingest / rebuild: children relink to the re-derived case."""
    _resplit(conn, relink_case=True, tag="R")


def test_split_shift_no_ruling_loss_live(conn: object) -> None:
    """Live capture: no relink; the moved ruling keeps its (correct) case."""
    _resplit(conn, relink_case=False, tag="L")


def test_split_shift_second_run_is_a_no_op(conn: object) -> None:
    court_id, s3_key, parent, cases, _ = _shifted_key(conn, "S")
    new = [split_child_document_id(parent, i, _N) for i in range(_N)]
    split_set = SplitSet(parent_document_id=parent, child_ids=tuple(new))
    for _run in range(2):
        for i in range(_N):
            _write(
                conn,
                document_id=new[i],
                case_id=cases[i],
                court_id=court_id,
                s3_key=s3_key,
                text=_text(i),
                relink_case=True,
                split_set=split_set,
            )
        delete_stale_split_children(conn, s3_key, new, parent_document_id=parent, owns_key=True)
    by_text = _rulings_by_text(conn, s3_key)
    for i in range(_N):
        assert by_text[_text(i)] == [(new[i], cases[i], "active")]


def test_true_duplicate_within_one_split_still_supersedes(conn: object) -> None:
    """Two slots of one split with the same case and text: the later slot
    loses dedup to the earlier one, which was already written in this pass
    (#2458 semantics).  A second pass does not flip the winner."""
    court_id = upsert_court(conn, "CA", "Test County 4820", "Superior Court")
    key_hash = uuid.uuid4().hex * 2
    s3_key = f"ca/orange/superior_court/raw/{key_hash}.pdf"
    parent = str(uuid.uuid5(uuid.NAMESPACE_URL, key_hash))
    case_id = upsert_case(conn, "2026-D-00001", court_id)
    new = [split_child_document_id(parent, i, 2) for i in range(2)]
    split_set = SplitSet(parent_document_id=parent, child_ids=tuple(new))
    for _run in range(2):
        for doc in new:
            _write(
                conn,
                document_id=doc,
                case_id=case_id,
                court_id=court_id,
                s3_key=s3_key,
                text=_text(0),
                split_set=split_set,
            )
        assert _rulings_by_text(conn, s3_key)[_text(0)] == [(new[0], case_id, "active")]
    with conn.cursor() as cur:
        cur.execute("SELECT status::text FROM documents WHERE id = %s::uuid", (new[1],))
        assert cur.fetchone()[0] == "superseded"


def test_dedup_loser_with_an_alert_detaches_it(conn: object) -> None:
    """A dedup loser whose own ruling row carries an alert: the row is
    deleted, so the alert is detached (never deleted) instead of the delete
    failing on the ``alert_events`` foreign key."""
    court_id = upsert_court(conn, "CA", "Test County 4820", "Superior Court")
    s3_key = f"ca/orange/superior_court/raw/{uuid.uuid4().hex * 2}.pdf"
    case_id = upsert_case(conn, "2026-A-00001", court_id)
    winner, loser = str(uuid.uuid4()), str(uuid.uuid4())
    _write(
        conn, document_id=winner, case_id=case_id, court_id=court_id, s3_key=s3_key, text="x one"
    )
    _write(conn, document_id=loser, case_id=case_id, court_id=court_id, s3_key=s3_key, text="x two")
    alert_id, _ = _alert_on(conn, loser)

    _write(conn, document_id=loser, case_id=case_id, court_id=court_id, s3_key=s3_key, text="x one")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT ruling_id, document_id::text FROM alert_events WHERE id = %s::uuid",
            (alert_id,),
        )
        assert cur.fetchone() == (None, loser)
