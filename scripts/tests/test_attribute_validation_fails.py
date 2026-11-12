"""Tests for scripts/attribute_validation_fails.py (#4728).

Pure tests cover rule classification and aggregation.  The drift guard runs
each rule in ``validation/deterministic.py`` and checks its reason still
classifies to that rule (skipped where scraper-framework is not importable).
The Postgres tests run the real SQL when ``TEST_DATABASE_URL`` points at a
migrated database; each runs in one rolled-back transaction.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _SCRIPTS)

import attribute_validation_fails as avf

_DSN = os.environ.get("TEST_DATABASE_URL", "")


def _args(*argv: str) -> object:
    return avf.parse_args(list(argv))


# ---------------------------------------------------------------------------
# Rule classification
# ---------------------------------------------------------------------------


def test_rules_for_joined_reasons_and_fallbacks() -> None:
    joined = (
        "case_number starts with 'UNKNOWN-' — extraction couldn't find case number; "
        "case_number starts with 'UNKNOWN-' AND case_title is null/empty/role-literal/"
        "placeholder — ruling is almost certainly a bad-split fragment (see #2571, #4628)"
    )
    assert avf.rules_for(joined, "deterministic") == [
        "case_number_not_unknown",
        "unknown_and_null_title_fail",
    ]
    assert avf.rules_for("looks wrong", "claude-haiku-4-5") == ["llm"]
    assert avf.rules_for("something new", "deterministic") == ["other"]
    assert avf.rules_for(None, None) == ["other"]


def test_drift_guard_every_deterministic_rule_classifies() -> None:
    """Each rule's real reason string must map back to that rule."""
    src = os.path.join(
        os.path.dirname(__file__), "..", "..", "packages", "scraper-framework", "src"
    )
    sys.path.insert(0, os.path.abspath(src))
    before = set(sys.modules)
    try:
        try:
            # ``ingestion`` first: importing validation.deterministic cold hits
            # the framework -> ingestion -> worker -> validation import cycle.
            import ingestion  # noqa: F401
            from validation import deterministic as det
        except ImportError as exc:  # scripts-tests CI has no scraper-framework deps
            pytest.skip(f"scraper-framework not importable: {exc}")
        _check_rules(det)
    finally:
        sys.path.pop(0)
        # Drop the scraper-framework modules so later scripts tests that mock
        # ``ingestion`` / ``framework`` see a clean sys.modules (#4426).
        for name in set(sys.modules) - before:
            del sys.modules[name]


def _check_rules(det: object) -> None:
    cases = [
        det.check_no_html_in_ruling_text("<html><body>x</body></html>"),
        det.check_hearing_date_in_range(date(2020, 1, 1), date(2026, 1, 1)),
        det.check_hearing_date_in_range(
            date(1990, 1, 1), date(2026, 1, 1), "structured_scraper"
        ),
        det.check_no_multiple_adversarial_patterns("A v. B and C v. D"),
        det.check_ruling_text_not_empty(""),
        det.check_case_number_not_unknown("UNKNOWN-abc"),
        det.check_unknown_and_null_title_fail("UNKNOWN-abc", None),
        det.check_no_duplicate_ruling_text([10, 10]),
        det.check_no_cross_case_ruling_text(
            "see 23STCV12345", "24STCV00001", ["23STCV12345"]
        ),
        det.check_ruling_text_case_number_marker(
            "Superior Court Case No. 25CECG03271", "24CECG00001"
        ),
        det.check_ruling_text_reasonable_length("x" * det._TRUNCATION_SENTINEL_LENGTH),
    ]
    seen = set()
    for res in cases:
        assert res.result != "pass", res
        assert res.rule in avf.rules_for(res.reason, "deterministic"), (
            res.rule,
            res.reason,
        )
        seen.add(res.rule)
    assert seen == {rule for rule, _ in avf.RULE_MARKERS}


# ---------------------------------------------------------------------------
# Aggregation / rendering
# ---------------------------------------------------------------------------

_UNK = "case_number starts with 'UNKNOWN-' — extraction couldn't find case number"
_EMPTY = "ruling_text is null or empty — extraction produced no content"


def _rows() -> list[avf.Row]:
    return [
        avf.Row("Santa Clara", "ca-sc", "raw/a.pdf", "deterministic", _UNK, "row", 3),
        avf.Row("Santa Clara", "ca-sc", "raw/b.pdf", "deterministic", _EMPTY, "row", 1),
        avf.Row("Orange", "ca-oc", "raw/c.pdf", "claude", "meh", "document", 2),
        avf.Row(None, None, None, "deterministic", _UNK, "none", 5),
    ]


def test_aggregate_groups_and_filters() -> None:
    rep = avf.aggregate(_rows(), top=2)
    assert rep["total"] == 11
    assert rep["by_attribution"] == {"none": 5, "row": 4, "document": 2}
    assert rep["by_county"]["Santa Clara / ca-sc"] == 4
    assert rep["by_county"]["(none) / (none)"] == 5
    assert rep["by_rule"] == {
        "case_number_not_unknown": 8,
        "llm": 2,
        "ruling_text_not_empty": 1,
    }
    assert rep["distinct_s3_keys"] == 3
    assert rep["top_s3_keys"][0] == {
        "s3_key": "raw/a.pdf",
        "n": 3,
        "rules": ["case_number_not_unknown"],
    }
    assert len(rep["top_s3_keys"]) == 2

    sc = avf.aggregate(_rows(), county="Santa Clara")
    assert sc["total"] == 4
    assert avf.aggregate(_rows(), scraper_id="ca-oc")["total"] == 2
    assert avf.aggregate(_rows(), rule="ruling_text_not_empty")["total"] == 1


def test_render_mentions_every_section() -> None:
    args = _args("--legacy")
    out = avf.render(avf.aggregate(_rows()), args)
    assert out.startswith(
        "validation_results result=fail window=2026-09-26T00:00:00Z .. now"
    )
    assert "(legacy attribution on)" in out
    for heading in (
        "by attribution:",
        "by county / scraper_id:",
        "by rule",
        "top 3 of 3 s3 keys:",
    ):
        assert heading in out
    assert "raw/a.pdf  [case_number_not_unknown]" in out


def test_build_query_legacy_adds_document_join() -> None:
    assert "derived.documents" not in avf.build_query(False)
    q = avf.build_query(True)
    assert "LEFT JOIN derived.documents d" in q
    assert "COALESCE(v.s3_key, d.s3_key)" in q


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_without_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert avf.main([]) == 2


def _fake_psycopg(rows: list[tuple]) -> tuple[MagicMock, MagicMock]:
    fake = MagicMock()
    conn = fake.connect.return_value.__enter__.return_value
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = rows
    return fake, conn


def test_main_is_read_only_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    fake, conn = _fake_psycopg(
        [("Santa Clara", "ca-sc", "raw/a.pdf", "deterministic", _UNK, "row", 3)]
    )
    with patch.dict(sys.modules, {"psycopg": fake}):
        assert avf.main(["--county", "Santa Clara"]) == 0
        assert conn.read_only is True
        assert avf.main(["--json", "--until", "2026-11-01T00:00:00Z"]) == 0
    text, js = capsys.readouterr().out.split("\n{", 1)
    assert "total rows: 3" in text
    payload = json.loads("{" + js)
    assert payload["total"] == 3
    assert payload["args"]["until"] == "2026-11-01T00:00:00Z"
    params = conn.cursor.return_value.__enter__.return_value.execute.call_args[0][1]
    assert params == {
        "result": "fail",
        "since": avf.DEFAULT_SINCE,
        "until": "2026-11-01T00:00:00Z",
    }


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


def _seed(conn: object) -> str:
    """Seed rows in 2001 (outside any real window) under a unique county."""
    county = f"Attr 4728 {uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO derived.courts (state, county, court_name, court_code) "
            "VALUES ('CA', %s, 'Superior Court', %s) RETURNING id::text",
            (county, f"ca-4728-{uuid.uuid4().hex[:8]}"),
        )
        court = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO derived.cases (court_id, case_number) VALUES (%s::uuid, %s) "
            "RETURNING id::text",
            (court, f"4728-{uuid.uuid4().hex[:8]}"),
        )
        case = cur.fetchone()[0]
        legacy_doc = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO derived.documents (id, court_id, case_id, document_type, format, "
            "s3_key, s3_bucket, content_hash, scraper_id, captured_at, status) VALUES "
            "(%s::uuid, %s::uuid, %s::uuid, 'ruling', 'pdf', 'raw/legacy.pdf', 'b', %s, "
            "'ca-4728', now(), 'active')",
            (legacy_doc, court, case, uuid.uuid4().hex),
        )
        rows = [
            # attributed at write time (post-#4706 shape)
            (str(uuid.uuid4()), "fail", _UNK, county, "ca-4728", "raw/new.pdf"),
            (str(uuid.uuid4()), "fail", _UNK, county, "ca-4728", "raw/new.pdf"),
            (str(uuid.uuid4()), "flag", _UNK, county, "ca-4728", "raw/new.pdf"),
            # pre-#4706: NULL attribution, document still exists
            (legacy_doc, "fail", _EMPTY, None, None, None),
            # pre-#4706: NULL attribution, no document (failed split child)
            (str(uuid.uuid4()), "fail", _EMPTY, None, None, None),
        ]
        for doc_id, result, reason, cty, scraper, key in rows:
            cur.execute(
                "INSERT INTO telemetry.validation_results (document_id, result, reason, "
                "model, county, scraper_id, s3_key, created_at) VALUES "
                "(%s::uuid, %s, %s, 'deterministic', %s, %s, %s, '2001-06-01T00:00:00Z')",
                (doc_id, result, reason, cty, scraper, key),
            )
    return county


def test_pg_plain_and_legacy_attribution(pg: object) -> None:
    county = _seed(pg)
    window = ("--since", "2001-01-01T00:00:00Z", "--until", "2002-01-01T00:00:00Z")

    plain = avf.fetch(pg, _args(*window))
    rep = avf.aggregate(plain, county=county)
    assert rep["total"] == 2
    assert rep["top_s3_keys"] == [
        {"s3_key": "raw/new.pdf", "n": 2, "rules": ["case_number_not_unknown"]}
    ]

    legacy = avf.fetch(pg, _args(*window, "--legacy"))
    rep = avf.aggregate(legacy, county=county)
    assert rep["total"] == 3
    assert rep["by_attribution"] == {"row": 2, "document": 1}
    assert {e["s3_key"] for e in rep["top_s3_keys"]} == {
        "raw/new.pdf",
        "raw/legacy.pdf",
    }
    # The failed split child stays unattributed.
    none_rows = [r for r in legacy if r.attribution == "none" and r.reason == _EMPTY]
    assert none_rows and all(r.s3_key is None for r in none_rows)

    flags = avf.aggregate(
        avf.fetch(pg, _args(*window, "--result", "flag")), county=county
    )
    assert flags["total"] == 1


def test_pg_read_only_session_rejects_writes(pg: object) -> None:
    pg.rollback()
    pg.read_only = True
    try:
        with pg.cursor() as cur, pytest.raises(Exception, match="read-only"):
            cur.execute("DELETE FROM telemetry.validation_results WHERE false")
    finally:
        pg.rollback()
        pg.read_only = False
