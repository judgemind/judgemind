"""Real-Postgres tests: the case and court upserts report a change to a
field other rulings' search docs show (#4935).

Needs a migrated database in ``TEST_DATABASE_URL``; skips without one unless
``REQUIRE_PG_TESTS=1``, like ``test_split_invariants_pg.py``.  Each test
uses its own court, so runs never see each other's rows.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ingestion.db import (  # noqa: E402
    lookup_court_id,
    upsert_case,
    upsert_case_returning_title,
    upsert_court,
)

_DSN = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_DSN) as c:
        yield c
        c.rollback()


def _county() -> str:
    return f"Shown {uuid.uuid4().hex[:10]}"


def test_court_insert_is_not_a_change(conn: psycopg.Connection) -> None:
    changed: set[str] = set()
    upsert_court(conn, "CA", _county(), "Superior Court", changed_court_ids=changed)
    assert changed == set()


def test_court_rename_is_a_change(conn: psycopg.Connection) -> None:
    county = _county()
    court_id = upsert_court(conn, "CA", county, "Superior Court")
    changed: set[str] = set()

    upsert_court(conn, "CA", county, "Superior Court", changed_court_ids=changed)
    assert changed == set()

    assert upsert_court(conn, "CA", county, "Renamed Court", changed_court_ids=changed) == (
        court_id
    )
    assert changed == {court_id}


def test_lookup_court_id(conn: psycopg.Connection) -> None:
    county = _county()
    assert lookup_court_id(conn, "CA", county) is None
    court_id = upsert_court(conn, "CA", county, "Superior Court")
    assert lookup_court_id(conn, "CA", county) == court_id


@pytest.mark.parametrize("script", ["reingest_from_s3", "rebuild_db"])
def test_script_court_seeding_reports_a_rename(conn: psycopg.Connection, script: str) -> None:
    """The batch scripts seed courts with their own upsert, which also
    rewrites the court name (#4935)."""
    import importlib

    mod = importlib.import_module(script)
    seed = mod._seed_courts if script == "reingest_from_s3" else mod.seed_courts
    county = _county()
    court = {
        "state": "CA",
        "county": county,
        "court_name": f"Superior Court, County of {county}",
        "court_code": f"ca-{county.lower().replace(' ', '-')}",
        "timezone": "America/Los_Angeles",
    }
    changed: set[str] = set()
    ids = seed(conn, [court], changed_court_ids=changed)
    assert changed == set()
    seed(conn, [court], changed_court_ids=changed)
    assert changed == set()
    seed(conn, [{**court, "court_name": "Renamed"}], changed_court_ids=changed)
    assert changed == {ids[court["court_code"]]}


def test_case_title_fill_is_a_change(conn: psycopg.Connection) -> None:
    court_id = upsert_court(conn, "CA", _county(), "Superior Court")
    changed: set[str] = set()

    case_id = upsert_case(conn, "30-2026-1", court_id, changed_case_ids=changed)
    assert changed == set(), "an insert changes no existing search doc"

    upsert_case(conn, "30-2026-1", court_id, changed_case_ids=changed)
    assert changed == set(), "an upsert that changes nothing is not a change"

    case_id2, title = upsert_case_returning_title(
        conn, "30-2026-1", court_id, case_title="Doe v. Roe", changed_case_ids=changed
    )
    assert (case_id2, title) == (case_id, "Doe v. Roe")
    assert changed == {case_id}


def test_case_type_fill_is_a_change(conn: psycopg.Connection) -> None:
    court_id = upsert_court(conn, "CA", _county(), "Superior Court")
    case_id = upsert_case(conn, "30-2026-2", court_id, case_title="Doe v. Roe")
    changed: set[str] = set()

    upsert_case(conn, "30-2026-2", court_id, case_type="civil", changed_case_ids=changed)
    assert changed == {case_id}


def test_preserved_title_is_not_a_change(conn: psycopg.Connection) -> None:
    """Live ingestion keeps the first title (#2468): a different incoming
    title changes nothing, so nothing is re-indexed."""
    court_id = upsert_court(conn, "CA", _county(), "Superior Court")
    upsert_case(conn, "30-2026-3", court_id, case_title="Doe v. Roe")
    changed: set[str] = set()

    _id, title = upsert_case_returning_title(
        conn, "30-2026-3", court_id, case_title="Other v. Party", changed_case_ids=changed
    )
    assert title == "Doe v. Roe"
    assert changed == set()


def test_forced_retitle_is_a_change(conn: psycopg.Connection) -> None:
    court_id = upsert_court(conn, "CA", _county(), "Superior Court")
    case_id = upsert_case(conn, "30-2026-4", court_id, case_title="Doe v. Roe")
    changed: set[str] = set()

    upsert_case(
        conn,
        "30-2026-4",
        court_id,
        case_title="Other v. Party",
        force_update=True,
        changed_case_ids=changed,
    )
    assert changed == {case_id}
