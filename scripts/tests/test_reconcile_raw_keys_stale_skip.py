# ruff: noqa: F811 — the s3 / explained fixtures are imported, then requested
"""Keys explained only by an extraction skip verdict that must be re-judged (#5094).

``no_ruling_text`` and ``zero_rulings_no_text`` come from an extraction, and
an extraction can be wrong (an LLM answering with empty cells, OCR that read
nothing).  A skip recorded under an older rule version, or one that replaced
rulings the key held, must not count as explained for good: the reconciler
re-ingests the key (``stale_skip``, through the #5008 incomplete path and
its attempt cap) with ``--bust-llm-cache``.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import types
from datetime import timedelta
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reconcile_raw_keys as rec
from validation.skip_versions import EXTRACTION_SKIP_RULE_VERSION

from tests.test_reconcile_raw_keys import (  # noqa: F401 — fixtures
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
#: The fixture's ``ingest_skips`` key.
KEY = f"{SD}{_h('6')}.pdf"
CURRENT = {"rule_version": EXTRACTION_SKIP_RULE_VERSION, "held_rulings": 0}


def _skip(
    ex: rec.Explained,
    reason: str = "no_ruling_text",
    detail: dict[str, Any] | None = None,
) -> rec.Explained:
    ex.skips[KEY] = reason
    if detail is not None:
        ex.skip_details[KEY] = detail
    return ex


def test_stale_skip_reason_matches_the_worker_constant() -> None:
    # ``ingestion.ingest_incomplete`` imports the worker (psycopg), which the
    # scripts test environment lacks: read the constant from the source.
    source = pathlib.Path(rec.__file__).resolve().parent.parent / (
        "packages/scraper-framework/src/ingestion/ingest_incomplete.py"
    )
    assert f'INCOMPLETE_STALE_SKIP = "{rec.STALE_SKIP}"' in source.read_text()


@pytest.mark.parametrize("reason", ["no_ruling_text", "zero_rulings_no_text"])
def test_extraction_skip_under_an_older_version_is_reopened(
    explained: rec.Explained, reason: str
) -> None:
    """A skip row recorded before #5094 carries no rule version (1)."""
    ex = _skip(explained, reason)
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "stale_skip",
    )
    assert rec.key_stale_skip(_raw(KEY), ex) == "rule_version"


@pytest.mark.parametrize("reason", ["no_ruling_text", "zero_rulings_no_text"])
def test_current_extraction_skip_on_a_fresh_key_stays_skipped(
    explained: rec.Explained, reason: str
) -> None:
    ex = _skip(explained, reason, dict(CURRENT))
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == ("skipped", reason)


def test_skip_that_replaced_held_rulings_is_flagged(explained: rec.Explained) -> None:
    """The worker refuses that write now: such a row hid the key's rulings."""
    ex = _skip(explained, detail={**CURRENT, "held_rulings": 3})
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "stale_skip",
    )
    assert rec.key_stale_skip(_raw(KEY), ex) == "removed_rulings"


def test_skip_that_removed_rulings_with_the_override_stands(
    explained: rec.Explained,
) -> None:
    ex = _skip(
        explained, detail={**CURRENT, "held_rulings": 3, "allowed_removal": True}
    )
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "skipped",
        "no_ruling_text",
    )


@pytest.mark.parametrize(
    "reason", ["santa_clara_placeholder", "governor_bio_route", "no_extractable_text"]
)
def test_deterministic_skips_are_never_reopened(
    explained: rec.Explained, reason: str
) -> None:
    ex = _skip(explained, reason, {"held_rulings": 5})
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == ("skipped", reason)


def test_a_represented_key_is_not_reopened_by_an_old_skip_row(
    explained: rec.Explained,
) -> None:
    """Skip rows are never deleted: a key written later is represented."""
    ex = _skip(explained)
    ex.documents.add(KEY)
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == ("represented", None)


def test_a_rule_version_bump_reopens_current_skips(
    explained: rec.Explained, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structural guarantee: bumping the version re-opens every
    extraction skip recorded under the old one."""
    import validation.skip_versions as sv

    ex = _skip(explained, detail=dict(CURRENT))
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE)[0] == "skipped"
    monkeypatch.setattr(
        sv, "EXTRACTION_SKIP_RULE_VERSION", EXTRACTION_SKIP_RULE_VERSION + 1
    )
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "stale_skip",
    )


def test_unresolved_stale_skip_record_counts_attempts_up_to_the_cap(
    explained: rec.Explained,
) -> None:
    ex = _skip(explained)
    ex.incomplete[KEY] = ("stale_skip", 1)
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "stale_skip",
    )
    ex.incomplete[KEY] = ("stale_skip", 3)
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete_stuck",
        "stale_skip",
    )


def test_worker_record_keeps_its_own_reason(explained: rec.Explained) -> None:
    """The reingest found the key holds rulings and refused the verdict
    (``empty_extraction``): a plain incomplete key now."""
    ex = _skip(explained)
    ex.incomplete[KEY] = ("empty_extraction", 1)
    ex.documents.add(KEY)
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "empty_extraction",
    )


def test_run_records_and_reingests_stale_skips_with_a_cache_bust(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    ex = _skip(explained)
    db = FakeDb({"RETURNING run_id": [("run-k",)]})
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
    assert result["incomplete_by_reason"] == {"stale_skip": 1}
    assert result["stale_skip_new"] == 1
    assert result["reingest_bust_cache"] == 1
    assert result["issue_needed"] is False
    # Recorded before the reingest, without overwriting a worker record.
    (insert,) = db.sql("INSERT INTO telemetry.ingest_incomplete")
    assert "ON CONFLICT (s3_key) DO NOTHING" in insert[0]
    assert insert[1][:3] == (KEY, "stale_skip", rec.raw_key_document_id(KEY))
    assert json.loads(insert[1][3]) == {
        "stale": "rule_version",
        "scope": "ca/san_diego",
    }
    # Re-ingested on its own launch, with --bust-llm-cache; the other
    # launches read the cache as usual.
    busted = [c for c in calls if "--bust-llm-cache" in c]
    assert len(busted) == 1
    listed = s3.puts[busted[0][busted[0].index("--s3-key-list") + 1]].decode()
    assert listed.splitlines() == [KEY]
    for cmd in calls:
        if cmd is not busted[0]:
            key_list = s3.puts[cmd[cmd.index("--s3-key-list") + 1]].decode()
            assert KEY not in key_list.splitlines()
    attempts = db.sql("SET retry_attempts = retry_attempts + 1")
    assert [KEY] in [a[1][0] for a in attempts]
    assert any(
        line.startswith(
            "STALE_SKIP ca/san_diego skip=no_ruling_text stale=rule_version"
        )
        for line in lines
    )


@pytest.mark.parametrize("represented", [True, False])
def test_resolved_stale_skip_record_is_deleted(
    s3: FakeS3, explained: rec.Explained, represented: bool
) -> None:
    """At the attempt cap, but re-judged: the reingest wrote the key, or it
    recorded the verdict again under the current rules."""
    ex = _skip(explained)
    ex.incomplete[KEY] = ("stale_skip", 3)
    if represented:
        ex.documents.add(KEY)
    else:
        ex.skip_details[KEY] = dict(CURRENT)
    expected = ("represented", None) if represented else ("skipped", "no_ruling_text")
    assert rec.classify(_raw(KEY), ex, now=NOW, grace=GRACE) == expected

    db = FakeDb({"RETURNING run_id": [("run-r",)], "stuck_at IS NULL": [(KEY,)]})
    lines: list[str] = []
    rec.run(_args("--record"), s3, ex, out=lines.append, now=NOW, conn=db)
    result = _result(lines)
    assert result["stale_skip_resolved"] == 1
    assert result["incomplete_stuck"] == 0
    assert result["newly_stuck"] == 0
    (delete,) = db.sql("DELETE FROM telemetry.ingest_incomplete")
    assert delete[1] == ([KEY], "stale_skip")


def test_fetch_explained_reads_extraction_skip_details() -> None:
    db = FakeDb(
        {
            "SELECT s3_key, detail FROM telemetry.ingest_skips": [
                ("k1", {"rule_version": 2, "held_rulings": 0}),
                ("k2", json.dumps({"rule_version": 1})),
                ("k3", "not json"),
            ],
            "SELECT s3_key, reason FROM telemetry.ingest_skips": [
                ("k1", "no_ruling_text"),
                ("k2", "zero_rulings_no_text"),
                ("k3", "no_ruling_text"),
                ("k4", "governor_bio_route"),
            ],
        }
    )
    ex = rec.fetch_explained(db)
    assert ex.skip_details == {
        "k1": {"rule_version": 2, "held_rulings": 0},
        "k2": {"rule_version": 1},
    }
    (query,) = db.sql("SELECT s3_key, detail")
    assert sorted(query[1][0]) == ["no_ruling_text", "zero_rulings_no_text"]


def test_fetch_explained_skips_the_detail_query_without_extraction_skips() -> None:
    db = FakeDb(
        {
            "SELECT s3_key, reason FROM telemetry.ingest_skips": [
                ("k4", "governor_bio_route")
            ]
        }
    )
    ex = rec.fetch_explained(db)
    assert ex.skip_details == {}
    assert not db.sql("SELECT s3_key, detail")


def test_unreadable_skip_details_read_as_version_one(capsys: Any) -> None:
    db = FakeDb(
        {
            "SELECT s3_key, reason FROM telemetry.ingest_skips": [
                ("k1", "no_ruling_text")
            ]
        }
    )
    db.fail_on = "SELECT s3_key, detail"
    ex = rec.fetch_explained(db)
    assert ex.skips == {"k1": "no_ruling_text"}
    assert ex.skip_details == {}
    assert "ingest_skips detail unreadable" in capsys.readouterr().out
