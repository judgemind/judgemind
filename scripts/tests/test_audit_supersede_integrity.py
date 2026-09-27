"""Tests for scripts/audit_supersede_integrity.py (#4813).

The mock tests cover the control flow.  The Postgres tests run the real SQL
when ``TEST_DATABASE_URL`` points at a migrated database; each runs in one
rolled-back transaction.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audit_supersede_integrity as audit_mod

_DSN = os.environ.get("TEST_DATABASE_URL", "")


def _conn_with(results: list[list[tuple]]) -> MagicMock:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.side_effect = results
    return conn


def test_audit_counts_and_samples() -> None:
    conn = _conn_with([[("a",), ("b",)], [("c",), ("d",)]])
    result = audit_mod.audit(conn)
    assert result["superseded_with_ruling"] == 2
    assert result["cycle_members"] == 2
    assert result["cycle_members_sample"] == ["c", "d"]
    line = audit_mod.report(result)
    assert line.startswith("SUPERSEDE_INTEGRITY ")
    assert "_cycle_members" not in json.loads(line.split(" ", 1)[1])


def test_cycles_of_groups_members() -> None:
    chain = {"a": "b", "b": "a", "c": "d", "d": "e", "e": "c"}
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    last: dict[str, str] = {}

    def _execute(sql: str, params: tuple) -> None:
        last["id"] = params[0]

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = lambda: (chain[last["id"]],)

    assert audit_mod.cycles_of(conn, ["e", "a", "b", "c", "d"]) == [
        ["a", "b"],
        ["c", "d", "e"],
    ]


def test_main_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert audit_mod.main([]) == 2


def test_main_reports_and_exits_on_findings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    psycopg = pytest.importorskip("psycopg")
    dirty = {"superseded_with_ruling": 1, "cycle_members": 0, "_cycle_members": []}
    clean = {"superseded_with_ruling": 0, "cycle_members": 0, "_cycle_members": []}
    with (
        patch.object(psycopg, "connect"),
        patch.object(audit_mod, "audit", side_effect=[dirty]),
    ):
        assert audit_mod.main([]) == 1
    with (
        patch.object(psycopg, "connect"),
        patch.object(audit_mod, "audit", side_effect=[dirty, clean]),
        patch.object(
            audit_mod, "repair", return_value={"revived": 1, "cycles_broken": 0}
        ),
    ):
        assert audit_mod.main(["--repair"]) == 0
    out = capsys.readouterr().out
    assert "SUPERSEDE_REPAIR" in out


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------


@pytest.fixture
def pg() -> object:
    if not _DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    psycopg = pytest.importorskip("psycopg")
    c = psycopg.connect(_DSN)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _seed(conn: object) -> tuple[str, str, str]:
    """Two documents in a previous_version_id cycle; the superseded one
    holds the ruling (the Orange 5e2650f8 / 8a8f866c shape), plus a
    three-row cycle with no ruling."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO courts (state, county, court_name, court_code) "
            "VALUES ('CA', %s, 'Superior Court', %s) "
            "RETURNING id::text",
            (f"Audit 4813 {uuid.uuid4().hex[:8]}", f"ca-4813-{uuid.uuid4().hex[:8]}"),
        )
        court = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO cases (court_id, case_number) VALUES (%s::uuid, %s) RETURNING id::text",
            (court, f"4813-{uuid.uuid4().hex[:8]}"),
        )
        case = cur.fetchone()[0]
        ids = [str(uuid.uuid4()) for _ in range(5)]
        for doc_id in ids:
            cur.execute(
                "INSERT INTO documents (id, court_id, case_id, document_type, format, s3_key, "
                "s3_bucket, content_hash, scraper_id, captured_at, status) VALUES "
                "(%s::uuid, %s::uuid, %s::uuid, 'ruling', 'pdf', 'k', 'b', %s, 'test-4813', "
                "now(), 'superseded')",
                (doc_id, court, case, uuid.uuid4().hex),
            )
        a, b, c, d, e = ids
        for src, dst in ((a, b), (b, a), (c, d), (d, e), (e, c)):
            cur.execute(
                "UPDATE documents SET previous_version_id = %s::uuid WHERE id = %s::uuid",
                (dst, src),
            )
        cur.execute(
            "INSERT INTO rulings (document_id, case_id, court_id, ruling_text) "
            "VALUES (%s::uuid, %s::uuid, %s::uuid, 'The motion is GRANTED.')",
            (a, case, court),
        )
    return a, b, c


def test_pg_audit_finds_and_repair_fixes(pg: object) -> None:
    a, b, c = _seed(pg)
    before = audit_mod.audit(pg)
    assert (
        a in before["superseded_with_ruling_sample"]
        or before["superseded_with_ruling"] > 20
    )
    assert {a, b, c} <= set(before["_cycle_members"])

    fixed = audit_mod.repair(pg)
    assert fixed["revived"] >= 1
    assert fixed["cycles_broken"] >= 1

    after = audit_mod.audit(pg)
    assert after["superseded_with_ruling"] == 0
    assert after["cycle_members"] == 0
    with pg.cursor() as cur:
        cur.execute(
            "SELECT status::text, previous_version_id FROM documents WHERE id = %s::uuid",
            (a,),
        )
        assert cur.fetchone() == ("active", None)
