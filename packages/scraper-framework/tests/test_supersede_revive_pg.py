"""Real-Postgres regression tests for #4809: a document superseded by
content-hash dedup comes back to ``active`` once it holds the ruling again.

Found by the #4801 repair.  An LA page's only ruling sat on a stray row that
had won dedup against the key's content parent.  The prefix reingest deleted
the stray (the ``owns_key`` cleanup, #4796) and wrote the ruling back onto the
content parent, which kept ``status = 'superseded'``.  The API then answers
410 for "view original" on a live ruling.

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
    delete_stale_split_children,
    insert_document_and_ruling,
    upsert_case,
    upsert_court,
)

_DSN = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="TEST_DATABASE_URL not set")

_TEXT = "The demurrer is OVERRULED. Defendant shall answer within 10 days."


@pytest.fixture
def conn() -> Iterator[object]:
    import psycopg

    c = psycopg.connect(_DSN)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


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
        scraper_id="test-4809",
        captured_at=datetime(2026, 9, 1),
        hearing_date=date(2026, 9, 5),
        ruling_text=_TEXT,
    )


def _doc(conn: object, document_id: str) -> tuple | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.status::text, d.change_type, d.previous_version_id, "
            "(SELECT count(*) FROM rulings r WHERE r.document_id = d.id) "
            "FROM documents d WHERE id = %s::uuid",
            (document_id,),
        )
        return cur.fetchone()


def test_dedup_loser_stays_superseded(conn: object) -> None:
    court_id = upsert_court(conn, "CA", "Test County 4809", "Superior Court")
    case_id = upsert_case(conn, "24STCV04809", court_id)
    key = f"ca/los_angeles/superior_court/raw/{'a' * 64}.html"
    winner, loser = str(uuid.uuid4()), str(uuid.uuid4())

    _write(conn, document_id=winner, case_id=case_id, court_id=court_id, s3_key=key)
    _write(conn, document_id=loser, case_id=case_id, court_id=court_id, s3_key=key)

    assert _doc(conn, winner)[0] == "active"
    status, change_type, prev, rulings = _doc(conn, loser)
    assert (status, change_type, str(prev), rulings) == (
        "superseded",
        "duplicate_content",
        winner,
        0,
    )


def test_superseded_parent_revives_when_it_takes_the_ruling_back(conn: object) -> None:
    court_id = upsert_court(conn, "CA", "Test County 4809", "Superior Court")
    case_id = upsert_case(conn, "24STCV14809", court_id)
    key_hash = "b" * 64
    key = f"ca/los_angeles/superior_court/raw/{key_hash}.html"
    parent = str(uuid.uuid5(uuid.NAMESPACE_URL, key_hash))
    stray = str(uuid.uuid5(uuid.NAMESPACE_URL, "c" * 64))

    # The stray row wins dedup; the content parent loses and is superseded.
    _write(conn, document_id=stray, case_id=case_id, court_id=court_id, s3_key=key)
    _write(conn, document_id=parent, case_id=case_id, court_id=court_id, s3_key=key)
    assert _doc(conn, parent)[0] == "superseded"

    # Reingest of the key: the content parent owns every row on it.
    delete_stale_split_children(conn, key, [parent], parent_document_id=parent, owns_key=True)
    _write(conn, document_id=parent, case_id=case_id, court_id=court_id, s3_key=key)

    assert _doc(conn, stray) is None
    assert _doc(conn, parent) == ("active", None, None, 1)
