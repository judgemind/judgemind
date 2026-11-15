# ruff: noqa: F811 — the s3 / explained fixtures are imported, then requested
"""Keys explained by a validation failure under a since-relaxed rule (#5027).

Before #4793, ``hearing_date_in_range`` failed a ruling heard more than 180
days before capture; now it flags a structured-source date.  A key whose
only explanation is such a failure would be written today, so the
reconciler must re-ingest it (through the #5008 incomplete path and its
attempt cap) instead of counting it as explained for good.
"""

from __future__ import annotations

import json
import os
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reconcile_raw_keys as rec
from ingestion.ingest_incomplete import INCOMPLETE_STALE_VALIDATION
from validation.rule_versions import RULE_VERSIONS, stale_rules

from tests.test_reconcile_raw_keys import (  # noqa: F401 — fixtures
    GOV,
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
BEFORE_4793 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
AFTER_4793 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
HEARING_REASON = (
    "hearing_date (2025-11-03) is 302 days from captured_at (2026-09-01) "
    "— exceeds 180-day threshold"
)
TITLE_REASON = (
    "case_number starts with 'UNKNOWN-' AND case_title is null/empty/role-literal/"
    "placeholder — ruling is almost certainly a bad-split fragment"
)
SD_KEY = f"{SD}{_h('1')}.pdf"


def _stale(ex: rec.Explained, key: str = SD_KEY) -> rec.Explained:
    """*key* failed ``hearing_date_in_range`` before the #4793 relaxation."""
    ex.validation_fail.add(key)
    ex.validation_rows[key] = [rec.FailRow(BEFORE_4793, HEARING_REASON)]
    return ex


def test_stale_validation_reason_matches_the_worker_constant() -> None:
    assert rec.STALE_VALIDATION == INCOMPLETE_STALE_VALIDATION


def test_failure_under_a_relaxed_rule_is_pending_revalidation(
    explained: rec.Explained,
) -> None:
    ex = _stale(explained)
    assert rec.classify(_raw(SD_KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "stale_validation",
    )


def test_pre_4773_failure_matched_by_document_id_is_pending_revalidation() -> None:
    key = f"{LA}{_h('9')}.html"
    doc_id = rec.raw_key_document_id(key)
    assert doc_id
    ex = rec.Explained(
        validation_fail_documents={doc_id},
        validation_rows_by_document={
            doc_id: [rec.FailRow(BEFORE_4793, HEARING_REASON)]
        },
    )
    assert rec.classify(_raw(key), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "stale_validation",
    )


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(
            [rec.FailRow(BEFORE_4793, TITLE_REASON)], id="unchanged-rule-failed-it"
        ),
        pytest.param(
            [
                rec.FailRow(BEFORE_4793, HEARING_REASON),
                rec.FailRow(AFTER_4793, TITLE_REASON),
            ],
            id="re-judged-after-the-relaxation",
        ),
        pytest.param(
            [
                rec.FailRow(
                    AFTER_4793,
                    HEARING_REASON,
                    ("hearing_date_in_range",),
                    dict(RULE_VERSIONS),
                )
            ],
            id="failed-under-the-current-version",
        ),
    ],
)
def test_failures_that_stand_under_the_current_rules_stay_explained(
    explained: rec.Explained, rows: list[rec.FailRow]
) -> None:
    explained.validation_rows[SD_KEY] = rows
    assert rec.classify(_raw(SD_KEY), explained, now=NOW, grace=GRACE) == (
        "skipped",
        "validation_fail",
    )


def test_intentional_skip_prefix_is_never_pending_revalidation() -> None:
    key = f"{GOV}{_h('5')}.html"
    ex = _stale(rec.Explained(), key)
    assert rec.classify(_raw(key), ex, now=NOW, grace=GRACE) == (
        "skipped",
        "validation_fail",
    )


def test_a_later_rule_relaxation_makes_versioned_failures_stale_again(
    explained: rec.Explained, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structural guarantee: bumping a rule's version re-opens every key
    it failed, including failures recorded with versions (migration 73)."""
    explained.validation_rows[SD_KEY] = [
        rec.FailRow(
            AFTER_4793,
            TITLE_REASON,
            ("unknown_and_null_title_fail",),
            dict(RULE_VERSIONS),
        )
    ]
    assert rec.classify(_raw(SD_KEY), explained, now=NOW, grace=GRACE)[0] == "skipped"
    # The rule is relaxed: its version goes to 2.
    bumped = {**RULE_VERSIONS, "unknown_and_null_title_fail": 2}
    monkeypatch.setattr(rec, "stale_rules", lambda rows: stale_rules(rows, bumped))
    assert rec.classify(_raw(SD_KEY), explained, now=NOW, grace=GRACE) == (
        "incomplete",
        "stale_validation",
    )


def test_run_records_and_reingests_stale_keys_through_the_incomplete_path(
    s3: FakeS3, explained: rec.Explained, tmp_path: Any
) -> None:
    ex = _stale(explained)
    db = FakeDb({"RETURNING run_id": [("run-s",)]})
    calls: list[list[str]] = []
    lines: list[str] = []
    rec.run(
        _args("--record", "--reingest", "--reingest-script", _stub_script(tmp_path)),
        s3,
        ex,
        out=lines.append,
        now=NOW,
        conn=db,
        runner=lambda cmd, check: (
            calls.append(cmd) or types.SimpleNamespace(returncode=0)
        ),
    )
    result = _result(lines)
    assert result["incomplete"] == 1
    assert result["incomplete_by_reason"] == {"stale_validation": 1}
    assert result["stale_validation_new"] == 1
    assert result["issue_needed"] is False
    # Recorded before the reingest, without overwriting a worker record.
    (insert,) = db.sql("INSERT INTO telemetry.ingest_incomplete")
    assert "ON CONFLICT (s3_key) DO NOTHING" in insert[0]
    assert insert[1][:3] == (
        SD_KEY,
        "stale_validation",
        rec.raw_key_document_id(SD_KEY),
    )
    assert json.loads(insert[1][3]) == {
        "stale_rules": ["hearing_date_in_range"],
        "scope": "ca/san_diego",
    }
    # Re-ingested with the unrepresented keys and counted against the cap.
    ca = next(c for c in calls if c[c.index("--prefix") + 1] == "ca/")
    listed = s3.puts[ca[ca.index("--s3-key-list") + 1]].decode().splitlines()
    assert SD_KEY in listed
    attempts = db.sql("SET retry_attempts = retry_attempts + 1")
    assert attempts[0][1] == ([SD_KEY],)
    assert any(line.startswith("STALE_VALIDATION ca/san_diego") for line in lines)


def test_report_only_run_writes_nothing_but_counts_stale_keys(
    s3: FakeS3, explained: rec.Explained
) -> None:
    lines: list[str] = []
    rec.run(_args(), s3, _stale(explained), out=lines.append, now=NOW)
    result = _result(lines)
    assert result["incomplete_by_reason"] == {"stale_validation": 1}
    assert any(
        line.startswith("INCOMPLETE ca/san_diego reason=stale_validation attempts=0")
        for line in lines
    )


@pytest.mark.parametrize("represented", [True, False])
def test_resolved_stale_record_is_deleted_and_never_reported_stuck(
    s3: FakeS3, explained: rec.Explained, represented: bool
) -> None:
    """At the attempt cap, but re-judged: the reingest wrote the key, or the
    current rules failed it again (a newer, versioned failure)."""
    ex = _stale(explained)
    ex.incomplete[SD_KEY] = ("stale_validation", 3)
    if represented:
        ex.documents.add(SD_KEY)
    else:
        ex.validation_rows[SD_KEY].append(
            rec.FailRow(
                NOW, TITLE_REASON, ("unknown_and_null_title_fail",), dict(RULE_VERSIONS)
            )
        )
    expected = ("represented", None) if represented else ("skipped", "validation_fail")
    assert rec.classify(_raw(SD_KEY), ex, now=NOW, grace=GRACE) == expected

    db = FakeDb({"RETURNING run_id": [("run-r",)], "stuck_at IS NULL": [(SD_KEY,)]})
    lines: list[str] = []
    rec.run(_args("--record"), s3, ex, out=lines.append, now=NOW, conn=db)
    result = _result(lines)
    assert result["stale_validation_resolved"] == 1
    assert result["incomplete_stuck"] == 0
    assert result["newly_stuck"] == 0
    assert not db.sql("SET stuck_at = now()")
    (delete,) = db.sql("DELETE FROM telemetry.ingest_incomplete")
    assert "reason = %s" in delete[0]
    assert delete[1] == ([SD_KEY], "stale_validation")


def test_unresolved_stale_record_counts_attempts_up_to_the_cap(
    explained: rec.Explained,
) -> None:
    ex = _stale(explained)
    ex.incomplete[SD_KEY] = ("stale_validation", 1)
    assert rec.classify(_raw(SD_KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "stale_validation",
    )
    ex.incomplete[SD_KEY] = ("stale_validation", 3)
    assert rec.classify(_raw(SD_KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete_stuck",
        "stale_validation",
    )


def test_worker_record_for_a_stale_key_keeps_its_own_reason(
    explained: rec.Explained,
) -> None:
    """A later reingest that refused an incomplete extraction overwrote the
    reason: the key is a plain incomplete key now."""
    ex = _stale(explained)
    ex.incomplete[SD_KEY] = ("reingest_failed", 1)
    ex.documents.add(SD_KEY)
    assert rec.classify(_raw(SD_KEY), ex, now=NOW, grace=GRACE) == (
        "incomplete",
        "reingest_failed",
    )
