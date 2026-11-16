# ruff: noqa: F811 — the s3 / explained fixtures are imported, then requested
"""Keys the reconciler used to count as represented by mistake (#5093).

1. A key whose document rows are all superseded, hold no ruling, and whose
   dedup ruling is gone (the #4982 path deletes the dedup winner and nulls
   the loser's link) is ``incomplete`` with reason ``orphaned_superseded``,
   recorded and re-ingested under the #5008 attempt cap.
2. A ``telemetry.ingest_incomplete`` row whose raw object is gone from the
   archive is reported as ``orphan_incomplete`` and closed out as a
   ``raw_missing`` skip, never retried.

The SQL that finds orphaned keys runs against real Postgres in
``packages/scraper-framework/tests/test_reconcile_orphaned_superseded_pg.py``.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import types
from datetime import timedelta
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reconcile_raw_keys as rec
from validation.deferred_backlogs import DeferredBacklog

from tests.test_reconcile_raw_keys import (  # noqa: F401 — fixtures
    FED,
    LA,
    NOW,
    SD,
    FakeDb,
    FakeS3,
    _args,
    _h,
    _raw,
    _result,
    _stub_script,
    explained,
    s3,
)

GRACE = timedelta(hours=6)
#: The fixture's represented LA key; orphaned in these tests.
KEY = f"{LA}{_h('a')}.html"
#: An incomplete key whose raw object is not in the archive.
GONE = f"{LA}{_h('9')}.html"


def _orphaned(ex: rec.Explained, key: str = KEY) -> rec.Explained:
    ex.documents.add(key)
    ex.orphaned_superseded.add(key)
    return ex


class _NotFound(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class HeadS3(FakeS3):
    """FakeS3 with ``head_object``: listed keys exist, *codes* maps other
    keys to the error code their HEAD raises (default 404)."""

    def __init__(self, base: FakeS3, codes: dict[str, str] | None = None) -> None:
        super().__init__(base.objects)
        self.codes = codes or {}
        self.heads: list[str] = []

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        self.heads.append(Key)
        if Key in self.objects:
            return {}
        raise _NotFound(self.codes.get(Key, "404"))


def _package_source(module: str) -> str:
    # ``ingestion`` imports the worker (psycopg), which the scripts test
    # environment lacks: read the constants from the source.
    return (
        pathlib.Path(rec.__file__).resolve().parent.parent
        / f"packages/scraper-framework/src/ingestion/{module}.py"
    ).read_text()


def test_reasons_match_the_package_constants() -> None:
    assert (
        f'INCOMPLETE_ORPHANED_SUPERSEDED = "{rec.ORPHANED_SUPERSEDED}"'
        in _package_source("ingest_incomplete")
    )
    assert f'SKIP_RAW_MISSING = "{rec.RAW_MISSING}"' in _package_source("ingest_skips")


# -- classification -----------------------------------------------------------


def test_superseded_only_key_with_no_reachable_ruling_is_incomplete(
    explained: rec.Explained,
) -> None:
    raw = _raw(KEY)
    assert rec.classify(raw, explained, now=NOW, grace=GRACE) == ("represented", None)
    ex = _orphaned(explained)
    assert not rec.is_represented(KEY, ex)
    assert rec.classify(raw, ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "orphaned_superseded",
    )


def test_recorded_skip_or_validation_failure_still_explains_an_orphaned_key(
    explained: rec.Explained,
) -> None:
    ex = _orphaned(explained)
    ex.skips[KEY] = "no_ruling_text"
    # An extraction skip recorded before #5094 is re-judged (stale_skip)...
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "stale_skip",
    )
    # ...one recorded under the current rules explains the key.
    ex.skip_details[KEY] = {"rule_version": 2, "held_rulings": 0}
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "skipped",
        "no_ruling_text",
    )
    del ex.skips[KEY]
    ex.validation_fail.add(KEY)
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "skipped",
        "validation_fail",
    )


def test_orphaned_record_counts_attempts_up_to_the_cap(
    explained: rec.Explained,
) -> None:
    ex = _orphaned(explained)
    ex.incomplete[KEY] = ("orphaned_superseded", 2)
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "orphaned_superseded",
    )
    ex.incomplete[KEY] = ("orphaned_superseded", 3)
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete_stuck",
        "orphaned_superseded",
    )


def test_reconcile_tracks_new_and_resolved_orphaned_records(
    explained: rec.Explained,
) -> None:
    ex = _orphaned(explained)
    reports = rec.reconcile([_raw(KEY)], ex, now=NOW, grace=GRACE)
    assert reports["ca/los_angeles"].new_orphaned == [_raw(KEY)]
    assert reports["ca/los_angeles"].incomplete_by_reason == {"orphaned_superseded": 1}

    # Revived since: the record resolves and the key is represented.
    ex.orphaned_superseded.clear()
    ex.incomplete[KEY] = ("orphaned_superseded", 1)
    reports = rec.reconcile([_raw(KEY)], ex, now=NOW, grace=GRACE)
    rep = reports["ca/los_angeles"]
    assert rep.resolved_orphaned == [_raw(KEY)]
    assert rep.new_orphaned == []
    assert rep.counts == {"represented": 1}


def test_stale_record_on_an_orphaned_key_hands_over_to_the_orphaned_reason(
    explained: rec.Explained,
) -> None:
    """A stale_validation record whose re-validation is done (no stale rule
    left) on a key that is now orphaned: the stale record resolves and an
    orphaned one replaces it."""
    ex = _orphaned(explained)
    ex.incomplete[KEY] = ("stale_validation", 1)
    rep = rec.reconcile([_raw(KEY)], ex, now=NOW, grace=GRACE)["ca/los_angeles"]
    assert rep.resolved_stale == [_raw(KEY)]
    assert rep.new_orphaned == [_raw(KEY)]


# -- run ----------------------------------------------------------------------


def test_run_records_and_reingests_orphaned_keys(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    ex = _orphaned(explained)
    db = FakeDb({"RETURNING run_id": [("run-o",)]})
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args("--record", "--reingest", "--reingest-script", _stub_script(tmp_path)),
        s3,
        ex,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check, **kw: (
            calls.append(cmd) or types.SimpleNamespace(returncode=0)
        ),
    )
    result = _result(lines)
    assert result["represented"] == 0
    assert result["incomplete"] == 1
    assert result["incomplete_by_reason"] == {"orphaned_superseded": 1}
    assert result["orphaned_superseded"] == 1
    assert result["orphaned_superseded_new"] == 1
    # Recorded before the reingest, without overwriting a worker record.
    (insert,) = db.sql("INSERT INTO telemetry.ingest_incomplete")
    assert "ON CONFLICT (s3_key) DO NOTHING" in insert[0]
    assert insert[1][:4] == (
        KEY,
        "orphaned_superseded",
        "los_angeles",
        rec.raw_key_document_id(KEY),
    )
    assert json.loads(insert[1][4]) == {"scope": "ca/los_angeles"}
    # Re-ingested with the unrepresented keys and counted against the cap.
    ca = next(c for c in calls if c[c.index("--prefix") + 1] == "ca/")
    listed = s3.puts[ca[ca.index("--s3-key-list") + 1]].decode().splitlines()
    assert KEY in listed
    attempts = db.sql("SET retry_attempts = retry_attempts + 1")
    assert attempts[0][1] == ([KEY],)
    assert any(line.startswith("ORPHANED_SUPERSEDED ca/los_angeles") for line in lines)


def test_run_deletes_a_resolved_orphaned_record(
    s3: FakeS3, explained: rec.Explained
) -> None:
    explained.incomplete[KEY] = ("orphaned_superseded", 3)
    db = FakeDb({"RETURNING run_id": [("run-r",)], "stuck_at IS NULL": [(KEY,)]})
    lines: list[str] = []
    rec.run(_args("--record"), s3, explained, out=lines.append, now=NOW, conn=db)
    result = _result(lines)
    assert result["orphaned_superseded_resolved"] == 1
    assert result["incomplete_stuck"] == 0
    assert result["newly_stuck"] == 0
    (delete,) = db.sql("DELETE FROM telemetry.ingest_incomplete")
    assert delete[1] == ([KEY], "orphaned_superseded")
    assert not db.sql("INSERT INTO telemetry.ingest_incomplete")


def test_stuck_orphaned_key_is_reported_once(
    s3: FakeS3, explained: rec.Explained
) -> None:
    """The #4982 dedup-loser case a reingest cannot revive: at the cap it is
    reported (issue once), not forced."""
    ex = _orphaned(explained)
    ex.incomplete[KEY] = ("orphaned_superseded", 3)
    db = FakeDb({"RETURNING run_id": [("run-s",)], "stuck_at IS NULL": [(KEY,)]})
    lines: list[str] = []
    rec.run(_args("--record"), s3, ex, out=lines.append, now=NOW, conn=db)
    result = _result(lines)
    assert result["incomplete_stuck"] == 1
    assert result["newly_stuck"] == 1
    assert result["issue_needed"] is True
    assert result["orphaned_superseded_new"] == 0


def test_deferred_backlog_orphan_is_never_recorded_or_reingested(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    fed = f"{FED}{_h('7')}.txt"
    ex = _orphaned(explained, fed)
    backlog = DeferredBacklog(name="cl", issue=3954, scopes=("federal/*",))
    db = FakeDb({"RETURNING run_id": [("run-d",)]})
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args("--record", "--reingest", "--reingest-script", _stub_script(tmp_path)),
        s3,
        ex,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check, **kw: (
            calls.append(cmd) or types.SimpleNamespace(returncode=0)
        ),
        deferrals=(backlog,),
    )
    assert not db.sql("INSERT INTO telemetry.ingest_incomplete")
    for cmd in calls:
        listed = s3.puts[cmd[cmd.index("--s3-key-list") + 1]].decode().splitlines()
        assert fed not in listed
    assert not any(q[1] and fed in q[1][0] for q in db.sql("retry_attempts + 1"))


# -- incomplete records whose raw object is gone ------------------------------


def test_missing_raw_incomplete_is_reported_and_closed_out(
    s3: FakeS3, explained: rec.Explained
) -> None:
    explained.incomplete[GONE] = ("evidence_unavailable", 1)
    heads = HeadS3(s3)
    db = FakeDb({"RETURNING run_id": [("run-m",)]})
    lines: list[str] = []
    rec.run(_args("--record"), heads, explained, out=lines.append, now=NOW, conn=db)
    result = _result(lines)
    assert result["orphan_incomplete"] == 1
    assert result["orphan_incomplete_by_reason"] == {"evidence_unavailable": 1}
    assert result["orphan_incomplete_sample"] == [GONE]
    assert result["orphan_incomplete_closed"] == 1
    assert result["issue_needed"] is True
    assert heads.heads == [GONE]
    (skip,) = db.sql("INSERT INTO telemetry.ingest_skips")
    assert skip[1] == ("raw_missing", [GONE])
    assert "'incomplete_reason', reason" in skip[0]
    (delete,) = db.sql("DELETE FROM telemetry.ingest_incomplete")
    assert delete[1] == ([GONE],)
    assert any(
        line == f"ORPHAN_INCOMPLETE reason=evidence_unavailable key={GONE}"
        for line in lines
    )
    assert "Raw object missing (#5093)" in rec.issue_summary(result)


def test_report_only_run_reports_missing_raw_without_writing(
    s3: FakeS3, explained: rec.Explained
) -> None:
    explained.incomplete[GONE] = ("dead_letter", 0)
    lines: list[str] = []
    rec.run(_args(), HeadS3(s3), explained, out=lines.append, now=NOW)
    result = _result(lines)
    assert result["orphan_incomplete"] == 1
    assert result["orphan_incomplete_closed"] == 0


def test_listed_out_of_scope_and_unproven_keys_are_not_missing(
    s3: FakeS3, explained: rec.Explained
) -> None:
    listed = f"{LA}{_h('b')}.html"
    throttled = f"{LA}{_h('8')}.html"
    elsewhere = f"{FED}{_h('6')}.txt"
    excluded = "tmp/keys/x.txt"
    for key in (listed, throttled, elsewhere, excluded):
        explained.incomplete[key] = ("dead_letter", 0)
    heads = HeadS3(s3, {throttled: "SlowDown"})
    lines: list[str] = []
    rec.run(_args("--prefix", "ca/"), heads, explained, out=lines.append, now=NOW)
    result = _result(lines)
    assert result["orphan_incomplete"] == 0
    # Only the in-scope, unlisted key is HEAD-checked.
    assert heads.heads == [throttled]


def test_in_listing_scope() -> None:
    scope = rec.in_listing_scope
    excl = rec.DEFAULT_EXCLUDE_PREFIXES
    assert scope(f"{SD}{_h('1')}.pdf", prefix="", exclude=excl, counties=None)
    assert not scope("tmp/keys/x.txt", prefix="", exclude=excl, counties=None)
    assert not scope(f"{FED}{_h('1')}.txt", prefix="ca/", exclude=excl, counties=None)
    assert scope(
        f"{SD}{_h('1')}.pdf", prefix="ca/", exclude=excl, counties={"san_diego"}
    )
    assert not scope(
        f"{LA}{_h('1')}.pdf", prefix="", exclude=excl, counties={"san_diego"}
    )
    assert not scope("ca/notes.txt", prefix="", exclude=excl, counties={"san_diego"})


def test_raw_missing_needs_a_not_found_code() -> None:
    class S3:
        def __init__(self, exc: Exception | None) -> None:
            self.exc = exc

        def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
            if self.exc:
                raise self.exc
            return {}

    assert rec.raw_missing(S3(_NotFound("NoSuchKey")), "b", "k")
    assert rec.raw_missing(S3(_NotFound("404")), "b", "k")
    assert not rec.raw_missing(S3(_NotFound("AccessDenied")), "b", "k")
    assert not rec.raw_missing(S3(RuntimeError("boom")), "b", "k")
    assert not rec.raw_missing(S3(None), "b", "k")


# -- database -----------------------------------------------------------------


def test_fetch_orphaned_superseded_reads_the_query_and_degrades(capsys: Any) -> None:
    db = FakeDb({"content_hash_dedup_supersede": [("k1",), (None,)]})
    assert rec.fetch_orphaned_superseded(db) == {"k1"}
    (sql,) = db.sql("content_hash_dedup_supersede")
    # Covered by the exact (case, hash) or by the hash on the linked winner;
    # never by the hash alone.
    assert "r.case_id = m.case_id AND r.ruling_text_hash = m.text_hash" in sql[0]
    assert "r.document_id = d.previous_version_id" in sql[0]

    db = FakeDb()
    db.fail_on = "content_hash_dedup_supersede"
    assert rec.fetch_orphaned_superseded(db) == set()
    assert db.rollbacks == 1
    assert "orphaned superseded keys unreadable" in capsys.readouterr().out


def test_db_helpers_write_nothing_for_no_keys() -> None:
    db = FakeDb()
    rec.record_orphaned_superseded(db, [])
    rec.close_out_raw_missing(db, [])
    rec.clear_stale_validation(db, [], rec.ORPHANED_SUPERSEDED)
    assert db.queries == []
    assert db.commits == 0


def test_issue_summary_reports_orphaned_superseded_keys() -> None:
    text = rec.issue_summary(
        {
            "action": "reingest",
            "orphaned_superseded": 92,
            "orphaned_superseded_new": 92,
            "orphaned_superseded_resolved": 0,
            "max_auto_reingest": 500,
        }
    )
    assert "Superseded with no reachable ruling (#5093):** 92 keys (92 new" in text
    assert "Raw object missing" not in text
