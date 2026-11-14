"""Real-Postgres tests: the reingest judge pre-pass seeds judges from a
labelled header field only, never from a name in a ruling body (#4934).

The pre-pass (``scripts/reingest_from_s3._seed_judges_from_keys``) scans the
whole raw document.  Before #4934 it took any name ``extract_judge_name``
found, so an Orange calendar that said a matter "was mediated before the
Honorable James P. Gray (Ret.)" created a judge "James P. Gray" with no
rulings.

Needs a migrated database in ``TEST_DATABASE_URL``; skips without one, like
``test_shown_row_changes_pg.py``.  Each test uses its own court.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import reingest_from_s3 as reingest  # noqa: E402

from ingestion.db import upsert_court  # noqa: E402

_DSN = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="TEST_DATABASE_URL not set")

_KEY = (
    "ca/orange/superior_court/raw/"
    "15acf7b81f4c3fef28afe74f98b15ca195ad50f0d4cf34c84c9a6d52b0141af2.pdf"
)

_MEDIATOR_TEXT = (
    "TENTATIVE RULINGS\nDEPT C34\nJUDGE H. SHAINA COLOVER\n"
    "LAW AND MOTION IS HEARD ON THURSDAY AT 1:30 P.M.\n"
    "The issue of Ron's nonpayment of Ms. Chen's retainer was mediated\n"
    "before the Honorable James P. Gray (Ret.) and Ron agreed to pay his share.\n"
    "The prior action was assigned to Judge Maria Gonzalez of another court.\n"
)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_DSN) as c:
        yield c
        c.rollback()


@pytest.fixture
def court_id(conn: psycopg.Connection) -> str:
    return upsert_court(conn, "CA", f"Prepass {uuid.uuid4().hex[:10]}", "Superior Court")


def _judges(conn: psycopg.Connection, court_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT canonical_name FROM judges WHERE court_id = %s::uuid ORDER BY 1",
            (court_id,),
        )
        return [r[0] for r in cur.fetchall()]


def _prepass(conn: psycopg.Connection, court_id: str, text: str) -> dict[str, int]:
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"%PDF"))}
    # The pre-pass commits each chunk that seeded a judge; keep the test's
    # rows inside its own transaction so the fixture's rollback removes them.
    with (
        patch.object(reingest, "_extract_text_from_content", return_value=text),
        patch.object(conn, "commit"),
    ):
        return reingest._seed_judges_from_keys(
            conn,
            s3,
            [_KEY],
            "bucket",
            {"ca-orange": court_id},
            parse_timeout=1.0,
            concurrency=1,
        )


def test_names_in_ruling_bodies_create_no_judge(conn: psycopg.Connection, court_id: str) -> None:
    stats = _prepass(conn, court_id, _MEDIATOR_TEXT)
    assert stats["judges_seeded"] == 0
    assert _judges(conn, court_id) == []


def test_presiding_judge_header_creates_the_judge(conn: psycopg.Connection, court_id: str) -> None:
    text = "DEPARTMENT 56 JUDGE STEVEN A. ELLIS\n" + _MEDIATOR_TEXT
    stats = _prepass(conn, court_id, text)
    assert stats["judges_seeded"] == 1
    assert _judges(conn, court_id) == ["Steven A. Ellis"]
