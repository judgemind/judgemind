"""Real-Postgres test of the reconciler's orphaned-superseded query (#5093).

A content-hash dedup loser keeps its document row, superseded, while the
winner holds the ruling.  The #4982 path (``delete_stale_split_children``
on the winner's key) deletes the winner and nulls the loser's link: the
ruling then exists nowhere, yet ``scripts/reconcile_raw_keys.py`` counted
the loser's key as represented because it had a document row.  This test
builds that state with the real write path and runs the reconciler's own
SQL (``fetch_explained``) against it.

Runs only when ``TEST_DATABASE_URL`` points at a migrated database (see
``test_split_relink_pg.py``).  Each test runs in one rolled-back transaction.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest

from ingestion.db import (
    delete_stale_split_children,
    insert_document_and_ruling,
    upsert_case,
    upsert_court,
)

_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
sys.path.insert(0, _SCRIPTS_DIR)

rec = importlib.import_module("reconcile_raw_keys")

_DSN = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="TEST_DATABASE_URL not set")

_TEXT = "The motion to compel is GRANTED. Responses due within 20 days."
_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[object]:
    import psycopg

    c = psycopg.connect(_DSN)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _key() -> tuple[str, str]:
    """A content-addressed San Diego key and its document id."""
    h = uuid.uuid4().hex + uuid.uuid4().hex
    return (
        f"ca/san_diego/superior_court/raw/{h}.html",
        str(uuid.uuid5(uuid.NAMESPACE_URL, h)),
    )


def _write(conn: object, *, document_id: str, case_id: str, court_id: str, s3_key: str) -> None:
    insert_document_and_ruling(
        conn,
        document_id=document_id,
        case_id=case_id,
        court_id=court_id,
        content_format="html",
        content_hash=uuid.uuid4().hex,
        s3_key=s3_key,
        s3_bucket="test-bucket",
        source_url="",
        scraper_id="test-5093",
        captured_at=datetime(2026, 9, 1),
        hearing_date=date(2026, 9, 5),
        ruling_text=_TEXT,
    )


def _classify(conn: object, key: str) -> tuple[str, str | None]:
    explained = rec.fetch_explained(conn)
    raw = rec.RawKey(key, "ca", "san_diego", _NOW - timedelta(days=30))
    return rec.classify(raw, explained, now=_NOW, grace=timedelta(hours=6))


@pytest.fixture
def dedup(conn: object) -> dict[str, str]:
    """A dedup winner (key B) and loser (key A) for one ruling."""
    court_id = upsert_court(
        conn, "CA", f"Test County 5093 {uuid.uuid4().hex[:6]}", "Superior Court"
    )
    case_id = upsert_case(conn, f"26CU{uuid.uuid4().hex[:6]}C", court_id)
    key_a, loser = _key()
    key_b, winner = _key()
    _write(conn, document_id=winner, case_id=case_id, court_id=court_id, s3_key=key_b)
    _write(conn, document_id=loser, case_id=case_id, court_id=court_id, s3_key=key_a)
    return {
        "court": court_id,
        "case": case_id,
        "key_a": key_a,
        "loser": loser,
        "key_b": key_b,
        "winner": winner,
    }


def _status(conn: object, document_id: str) -> tuple | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status::text, previous_version_id::text FROM documents WHERE id = %s::uuid",
            (document_id,),
        )
        return cur.fetchone()


def test_dedup_loser_is_covered_while_its_winner_holds_the_ruling(
    conn: object, dedup: dict[str, str]
) -> None:
    assert _status(conn, dedup["loser"]) == ("superseded", dedup["winner"])
    orphaned = rec.fetch_orphaned_superseded(conn)
    assert dedup["key_a"] not in orphaned
    assert dedup["key_b"] not in orphaned
    assert _classify(conn, dedup["key_a"]) == ("represented", None)


def test_winner_deleted_strands_the_loser_and_a_reingest_revives_it(
    conn: object, dedup: dict[str, str]
) -> None:
    # The same text in another case: boilerplate repeats across cases, so
    # it must not count as the loser's ruling.
    other_case = upsert_case(conn, f"26HR{uuid.uuid4().hex[:6]}C", dedup["court"])
    key_c, other = _key()
    _write(conn, document_id=other, case_id=other_case, court_id=dedup["court"], s3_key=key_c)

    # The #4982 path: the winner's key is re-split without the winner.
    delete_stale_split_children(
        conn, dedup["key_b"], [], parent_document_id=dedup["winner"], owns_key=True
    )
    assert _status(conn, dedup["winner"]) is None
    assert _status(conn, dedup["loser"]) == ("superseded", None)

    assert dedup["key_a"] in rec.fetch_orphaned_superseded(conn)
    assert _classify(conn, dedup["key_a"]) == ("incomplete", "orphaned_superseded")

    # Re-ingesting the loser's key writes the ruling back and revives it.
    _write(
        conn,
        document_id=dedup["loser"],
        case_id=dedup["case"],
        court_id=dedup["court"],
        s3_key=dedup["key_a"],
    )
    assert _status(conn, dedup["loser"])[0] == "active"
    assert dedup["key_a"] not in rec.fetch_orphaned_superseded(conn)
    assert _classify(conn, dedup["key_a"]) == ("represented", None)


def test_loser_is_covered_by_its_winner_after_the_case_is_re_keyed(
    conn: object, dedup: dict[str, str]
) -> None:
    """A rebuild can give the winner's ruling a new case row: the metric's
    case_id no longer matches, but the linked winner holds the same hash."""
    new_case = upsert_case(conn, f"37-2026-{uuid.uuid4().hex[:6]}", dedup["court"])
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE rulings SET case_id = %s::uuid WHERE document_id = %s::uuid",
            (new_case, dedup["winner"]),
        )
    assert dedup["key_a"] not in rec.fetch_orphaned_superseded(conn)

    # Without the link (and the exact case gone), nothing covers it.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE documents SET previous_version_id = NULL WHERE id = %s::uuid",
            (dedup["loser"],),
        )
    assert dedup["key_a"] in rec.fetch_orphaned_superseded(conn)
