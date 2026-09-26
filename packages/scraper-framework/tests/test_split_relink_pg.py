"""Real-Postgres regression tests for split-child relink and stale-child
cleanup (#4788, regression from #4743 / #4700).

The mocked tests in ``test_ingestion_db.py`` pin the SQL shape.  These run
the same functions against a real schema and check the rows that matter:
the ruling survives and ``public.alert_events.ruling_id`` still points at it.

They need a migrated database and run only when ``TEST_DATABASE_URL`` is set
(for example the local Docker Compose Postgres,
``postgresql://judgemind:localdev@localhost:5432/judgemind``).  Every test
runs inside one transaction that is rolled back, so no rows are left behind.
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
from ingestion.split_ids import make_split_document_id

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


def _court(conn: object) -> str:
    return upsert_court(conn, "CA", "Test County 4788", "Superior Court")


def _write(
    conn: object,
    *,
    document_id: str,
    case_id: str,
    court_id: str,
    s3_key: str,
    relink_case: bool = False,
) -> None:
    insert_document_and_ruling(
        conn,
        document_id=document_id,
        case_id=case_id,
        court_id=court_id,
        content_format="pdf",
        content_hash=uuid.uuid4().hex,
        s3_key=s3_key,
        s3_bucket="test-bucket",
        source_url="",
        scraper_id="test-4788",
        captured_at=datetime(2026, 9, 1),
        hearing_date=date(2026, 9, 5),
        ruling_text=f"Ruling text for {document_id}. The motion is GRANTED.",
        relink_case=relink_case,
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


def _row(conn: object, sql: str, *params: object) -> tuple | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def test_placeholder_case_never_replaces_real_case(conn: object) -> None:
    """Re-processing a correctly linked split child whose case number comes
    back missing (UNKNOWN-) keeps the ruling, its alert, and the document's
    case link, even when the caller asked for a relink."""
    court_id = _court(conn)
    parent = str(uuid.uuid4())
    child = make_split_document_id(parent, 2)
    real_case = upsert_case(conn, "24CV004788", court_id)
    _write(conn, document_id=child, case_id=real_case, court_id=court_id, s3_key="k/4788a.pdf")
    alert_id, ruling_id = _alert_on(conn, child)

    unknown_case = upsert_case(conn, f"UNKNOWN-{child}", court_id)
    _write(
        conn,
        document_id=child,
        case_id=unknown_case,
        court_id=court_id,
        s3_key="k/4788a.pdf",
        relink_case=True,
    )

    assert _row(conn, "SELECT id, case_id FROM rulings WHERE document_id = %s::uuid", child) == (
        uuid.UUID(ruling_id),
        uuid.UUID(real_case),
    )
    assert _row(conn, "SELECT case_id FROM documents WHERE id = %s::uuid", child) == (
        uuid.UUID(real_case),
    )
    assert _row(conn, "SELECT ruling_id FROM alert_events WHERE id = %s::uuid", alert_id) == (
        uuid.UUID(ruling_id),
    )


def test_real_case_relink_still_works(conn: object) -> None:
    """The #4700 relink (real case -> different real case) still applies."""
    court_id = _court(conn)
    child = make_split_document_id(str(uuid.uuid4()), 0)
    old_case = upsert_case(conn, "24CV000001", court_id)
    new_case = upsert_case(conn, "24CV000002", court_id)
    _write(conn, document_id=child, case_id=old_case, court_id=court_id, s3_key="k/4788b.pdf")
    _write(
        conn,
        document_id=child,
        case_id=new_case,
        court_id=court_id,
        s3_key="k/4788b.pdf",
        relink_case=True,
    )
    assert _row(conn, "SELECT case_id FROM rulings WHERE document_id = %s::uuid", child) == (
        uuid.UUID(new_case),
    )
    assert _row(conn, "SELECT case_id FROM documents WHERE id = %s::uuid", child) == (
        uuid.UUID(new_case),
    )


def test_pre_split_sibling_survives_cleanup(conn: object) -> None:
    """Two scraper pre-split children of one PDF share an s3_key and both
    have v5 ids.  Processing one of them must not delete the other."""
    court_id = _court(conn)
    content_parent = str(uuid.uuid5(uuid.NAMESPACE_URL, "f" * 64))
    sib0 = make_split_document_id(content_parent, 1)
    sib1 = make_split_document_id(content_parent, 2)
    s3_key = "ca/fresno/superior_court/4788.pdf"
    case_a = upsert_case(conn, "24CECG00001", court_id)
    case_b = upsert_case(conn, "24CECG00002", court_id)
    _write(conn, document_id=sib0, case_id=case_a, court_id=court_id, s3_key=s3_key)
    _write(conn, document_id=sib1, case_id=case_b, court_id=court_id, s3_key=s3_key)
    alert_id, ruling_id = _alert_on(conn, sib1)

    # A live pre-split event for sib0 that the LLM path handles as a
    # single ruling: its parent id is sib0, and the only child it writes
    # is sib0 itself.
    deleted = delete_stale_split_children(conn, s3_key, [sib0], parent_document_id=sib0)

    assert deleted == 0
    assert _row(conn, "SELECT id FROM rulings WHERE document_id = %s::uuid", sib1) == (
        uuid.UUID(ruling_id),
    )
    assert _row(conn, "SELECT ruling_id FROM alert_events WHERE id = %s::uuid", alert_id) == (
        uuid.UUID(ruling_id),
    )


def test_stale_children_of_this_parent_are_removed(conn: object) -> None:
    """Surplus children of *this* parent are still removed (#4700); a v5
    row on the same key that is not a child of this parent is kept."""
    court_id = _court(conn)
    parent = str(uuid.uuid5(uuid.NAMESPACE_URL, "e" * 64))
    kids = [make_split_document_id(parent, i) for i in range(3)]
    foreign = make_split_document_id(str(uuid.uuid4()), 0)
    s3_key = "ca/santa_clara/superior_court/4788.pdf"
    for i, kid in enumerate([*kids, foreign]):
        case = upsert_case(conn, f"24CV10000{i}", court_id)
        _write(conn, document_id=kid, case_id=case, court_id=court_id, s3_key=s3_key)
    alert_id, _ = _alert_on(conn, kids[2])

    removed: list[str] = []
    deleted = delete_stale_split_children(
        conn, s3_key, kids[:2], parent_document_id=parent, deleted_ids=removed
    )

    assert deleted == 1
    assert removed == [kids[2]]
    assert _row(conn, "SELECT 1 FROM documents WHERE id = %s::uuid", foreign) == (1,)
    # The alert is detached, never deleted.
    assert _row(conn, "SELECT ruling_id FROM alert_events WHERE id = %s::uuid", alert_id) == (None,)
