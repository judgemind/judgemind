"""Real-Postgres invariant harness for split / dedup / supersede / relink (#4845).

Random sequences of split results for one S3 key run through the live,
prefix-reingest and DB-row reingest write paths; ``split_harness`` checks the
invariants after every commit (see its docstring for the list).

- ``test_invariants_hold`` sweeps ``SPLIT_HARNESS_SEEDS`` seeds (default
  200).  ``SPLIT_HARNESS_SEED=<n>`` runs one seed.
- ``test_known_bug`` pins the seeds (and hand-built scenarios) that
  reproduce the bugs filed against this area, so each stays reproduced.

Needs a migrated database in ``TEST_DATABASE_URL`` (``scripts/apply_migrations.sh``).
Without one every test here skips, unless ``REQUIRE_PG_TESTS=1`` (set in CI),
which turns the skip into a failure so the harness cannot silently stop
running.  Each run uses fresh keys and courts, so runs never see each
other's rows.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import split_harness as harness  # noqa: E402

_DSN = os.environ.get("TEST_DATABASE_URL", "")
_REQUIRED = os.environ.get("REQUIRE_PG_TESTS") == "1"

_needs_pg = pytest.mark.skipif(not _DSN, reason="TEST_DATABASE_URL not set")

#: One token per test session, so a rerun never collides with earlier rows.
_RUN = uuid.uuid4().hex[:10]


def _seeds() -> list[int]:
    one = os.environ.get("SPLIT_HARNESS_SEED")
    if one:
        return [int(one)]
    return list(range(int(os.environ.get("SPLIT_HARNESS_SEEDS", "200"))))


def test_postgres_is_configured_when_required() -> None:
    """CI sets ``REQUIRE_PG_TESTS=1``: the harness must run there, not skip."""
    if _REQUIRED and not _DSN:
        pytest.fail("REQUIRE_PG_TESTS=1 but TEST_DATABASE_URL is not set")


@_needs_pg
@pytest.mark.parametrize("seed", _seeds())
def test_invariants_hold(seed: int) -> None:
    harness.run_seed(_DSN, seed, _RUN)


# ---------------------------------------------------------------------------
# Reproductions of filed bugs
# ---------------------------------------------------------------------------

#: (bug, seed, invariant the seed broke).  Found by the sweep; each run
#: enforces only that invariant, so an unrelated bug cannot mask or fake the
#: reproduction.  All of them failed before #4845 (PR #4846's CI run records
#: the failures as strict xfails).
_BUG_SEEDS = [
    # A textless row at slot 0 shifted every ruling one slot right; the
    # write of slot j+1 found its ruling on the earlier, never-dispatched
    # slot j, dedup superseded j+1 and deleted j+1's ruling.  Fixed by
    # writing the split set in one transaction, matching rows by content.
    pytest.param("4836", 9, "gap"),
    pytest.param("4836", 11, "gap"),
    # A ruling moved off its holder slot, whose own write was then rejected
    # by validation: the holder kept a search doc with no ruling behind it.
    # Fixed: a rejected child's row is removed with the split set's stale
    # rows, and dropped from search.
    pytest.param("4837", 69, "search"),
    # DB-row reingest wrote hearing_date with no hearing_date_source.  Fixed
    # by the one write path: DB-row mode now runs through the worker.
    pytest.param("4839", 13, "date_source"),
]


@_needs_pg
@pytest.mark.parametrize(("bug", "seed", "invariant"), _BUG_SEEDS)
def test_known_bug(bug: str, seed: int, invariant: str) -> None:
    harness.run_seed(_DSN, seed, _RUN, name=f"bug{bug}", invariants=frozenset({invariant}))


def _single_after_split_setup(h: harness.KeyHarness, key: harness.Key) -> None:
    """#4838 starting state: the key's parent row is active and holds the
    old unsplit ruling while the split children of an earlier re-split are
    still on the key."""
    step = _BUG_4838_STEPS[0]
    h.run_step(key, step, "setup split")
    first = step.specs[0]
    unsplit = harness.Spec(99, first.case_number, first.title, "Unsplit calendar text. " * 4)
    h.write_legacy_row(key, key.parent, unsplit)


def _bug_4838_steps() -> list[harness.Step]:
    specs = tuple(
        harness.Spec(i, harness._case_number(i), harness._title(i), harness._text("b4838", i))
        for i in range(3)
    )
    rows = tuple(harness.Row("ruling", i) for i in range(3))
    return [
        harness.Step("reingest", rows, specs),
        # DB-row reingest now parses the document as one ruling.
        harness.Step("db", (harness.Row("ruling", 0),), specs, ("single",)),
    ]


_BUG_4838_STEPS = _bug_4838_steps()


def _enrichment_seeds() -> list[int]:
    one = os.environ.get("SPLIT_HARNESS_SEED")
    if one:
        return [int(one)]
    return list(range(int(os.environ.get("SPLIT_HARNESS_ENRICHMENT_SEEDS", "8"))))


@_needs_pg
@pytest.mark.parametrize("seed", _enrichment_seeds())
@pytest.mark.parametrize(
    "splitter",
    harness.DETERMINISTIC_SPLITTERS,
    ids=[s.name for s in harness.DETERMINISTIC_SPLITTERS],
)
def test_exhausted_enrichment_keeps_child_ruling(
    splitter: harness.DeterministicSplitter, seed: int
) -> None:
    """A deterministic-split child whose per-child enrichment is exhausted
    keeps its existing document and ruling (#4878).  Before the fix only
    the LLM split path protected such a child; the six deterministic
    splitters let the split-set write delete it as stale."""
    harness.run_enrichment_failure(_DSN, splitter, seed, _RUN)


def _bug_4879_steps() -> list[harness.Step]:
    specs = tuple(
        harness.Spec(i, harness._case_number(i), harness._title(i), harness._text("b4879", i))
        for i in range(3)
    )
    # On the reingest, ruling 1 has lost its case number and caption, so it
    # fails deterministic validation (``unknown_and_null_title_fail``).
    rejected = harness.Spec(1, None, None, specs[1].text)
    rows = tuple(harness.Row("ruling", i) for i in range(3))
    return [
        harness.Step("reingest", rows, specs),
        harness.Step("reingest", rows, (specs[0], rejected, specs[2])),
    ]


@_needs_pg
def test_known_bug_4879_rejected_child_keeps_validation_fail_row() -> None:
    """A reingest where a previously valid child now fails validation must
    leave that child's ``validation_results`` fail row in place, attributed
    to the key (#4879).  Before the fix the split-set write removed the
    rejected child's old document as stale and deleted the fail row the same
    run had just committed for it."""
    h = harness.KeyHarness(_DSN, _RUN, -1, frozenset({"telemetry"}))
    try:
        key = harness.make_key(f"{_RUN}-bug4879")
        first, second = _bug_4879_steps()
        h.run_step(key, first, "valid split")
        rejected = next(doc for doc, label in key.slots.items() if label == "S1")
        assert rejected in {r.id for r in harness.read_key(h.checker, key.s3_key)}

        h.run_step(key, second, "child 1 rejected")

        assert rejected not in {r.id for r in harness.read_key(h.checker, key.s3_key)}
        with h.checker.cursor() as cur:
            cur.execute(
                "SELECT document_id::text, county, scraper_id, s3_key "
                "FROM validation_results WHERE s3_key = %s AND result = 'fail'",
                (key.s3_key,),
            )
            fails = cur.fetchall()
        assert fails == [(rejected, key.county, "reingest-ca-harness", key.s3_key)]
    finally:
        h.close()


@_needs_pg
def test_known_bug_4838_db_mode_keeps_stale_children() -> None:
    """DB-row reingest of a document that now parses as one ruling must not
    keep the old split children on the key (#4838).  Before #4845 DB-row
    mode skipped the stale-child cleanup for a single-ruling parse, and the
    parent lost dedup to a stale child."""
    harness.run_steps(
        _DSN,
        _BUG_4838_STEPS[1:],
        _RUN,
        name="bug4838",
        setup=_single_after_split_setup,
        invariants=frozenset({"converge"}),
    )


# ---------------------------------------------------------------------------
# #4915: a reingest moves a ruling off a case number its document lacks
# ---------------------------------------------------------------------------

#: The number the Orange PDF prints, and the LLM's misread of it (a dropped
#: digit) that an older run stored (#4907, #4915).
_PRINTED = "30-2024-01444459"
_MISREAD = "2024-0144459"


def _calendar_text(printed: str) -> str:
    """The extracted text of a one-ruling calendar PDF printing *printed*,
    long enough (200+ characters) for the case-number check."""
    return (
        f"SUPERIOR COURT OF CALIFORNIA, COUNTY OF ORANGE. Case No. {printed}. "
        + "Tentative ruling on the motion; the court has read the papers. " * 4
    )


def _rulings_with_alerts(h: harness.KeyHarness, key: harness.Key) -> list[tuple]:
    with h.checker.cursor() as cur:
        cur.execute(
            "SELECT r.id::text, c.case_number, a.ruling_id::text FROM rulings r "
            "JOIN documents d ON d.id = r.document_id JOIN cases c ON c.id = r.case_id "
            "LEFT JOIN alert_events a ON a.ruling_id = r.id WHERE d.s3_key = %s",
            (key.s3_key,),
        )
        return cur.fetchall()


def _run_misread_then_reingest(stored: str, *, tag: str, text: str | None = None) -> list[tuple]:
    """Store a ruling under *stored* the way a run before #4907 did (no
    usable document text, so no check), then reingest the key with the
    document text printing ``_PRINTED`` while the LLM reads ``_MISREAD``
    again.  *text* is the child's ruling text (the LLM's transcription).
    Returns ``(ruling id, case number, alert ruling id)`` before and after,
    concatenated."""
    h = harness.KeyHarness(_DSN, _RUN, -1)
    try:
        key = harness.make_key(f"{_RUN}-{tag}")
        text = text or harness._text(tag, 0)
        old = harness.Spec(0, stored, harness._title(0), text)
        h.run_step(key, harness.Step("reingest", (harness.Row("ruling", 0),), (old,)), "old run")
        h._attach_alerts(key)
        before = _rulings_with_alerts(h, key)

        h.event_extra = {"ruling_text": _calendar_text(_PRINTED)}
        misread = harness.Spec(0, _MISREAD, harness._title(0), text)
        h.run_step(key, harness.Step("reingest", (harness.Row("ruling", 0),), (misread,)), "fix")
        return before + _rulings_with_alerts(h, key)
    finally:
        h.close()


@_needs_pg
def test_unsupported_case_relinks_to_placeholder() -> None:
    """A ruling filed under a number its document does not print leaves
    that case on reingest, onto the child's ``UNKNOWN-`` case, even though
    the guard discards the LLM's number again (#4915).  Before the fix the
    #4788 "never relink onto a placeholder" rule kept it on the invented
    case.  The ruling keeps its row, so its alert keeps its ``ruling_id``;
    every harness invariant holds through both steps."""
    (old_id, old_case, old_alert), (new_id, new_case, new_alert) = _run_misread_then_reingest(
        _MISREAD, tag="b4915"
    )
    assert old_case == _MISREAD and old_alert == old_id
    assert new_case.startswith("UNKNOWN-")
    assert new_id == old_id and new_alert == old_id


@_needs_pg
def test_supported_case_never_relinks_to_placeholder() -> None:
    """Control for #4915: a number the document does print keeps the #4788
    protection when a reingest finds no number for the child."""
    (old_id, old_case, _), (new_id, new_case, new_alert) = _run_misread_then_reingest(
        _PRINTED, tag="b4915ok"
    )
    assert old_case == _PRINTED
    assert (new_id, new_case, new_alert) == (old_id, _PRINTED, old_id)


@_needs_pg
def test_misread_repeated_in_llm_transcription_still_relinks() -> None:
    """The LLM's transcription of the ruling can repeat its own misread
    (dev: ``2024-0139388`` in the ruling text, ``30-2024-01399388`` in the
    PDF).  That text is not evidence: the number still leaves its case
    (#4915).  Before the fix the child's text counted as support, so the
    guard kept the misread and the ruling stayed on it."""
    text = f"The motion to consolidate with case number 30-{_MISREAD} is DENIED. " * 2
    (old_id, old_case, _), (new_id, new_case, new_alert) = _run_misread_then_reingest(
        _MISREAD, tag="b4915llm", text=text
    )
    assert old_case == _MISREAD
    assert new_case.startswith("UNKNOWN-")
    assert new_id == old_id and new_alert == old_id


# ---------------------------------------------------------------------------
# #4920: a judge rename re-indexes the judge's rulings on other keys
# ---------------------------------------------------------------------------


def _judge_ruling_step(kind: str, tag: str, idx: int, judge: str) -> harness.Step:
    spec = harness.Spec(
        idx, harness._case_number(idx), harness._title(idx), harness._text(tag, idx), judge
    )
    return harness.Step(kind, (harness.Row("ruling", 0),), (spec,))


def _judge_canonical(h: harness.KeyHarness, key: harness.Key) -> list[str]:
    with h.checker.cursor() as cur:
        cur.execute(
            "SELECT j.canonical_name FROM rulings r JOIN documents d ON d.id = r.document_id "
            "JOIN judges j ON j.id = r.judge_id WHERE d.s3_key = %s",
            (key.s3_key,),
        )
        return [row[0] for row in cur.fetchall()]


@_needs_pg
def test_judge_rename_reindexes_rulings_on_other_keys() -> None:
    """A write that lengthens a judge's canonical name (``resolve_judge``'s
    near-duplicate step, "Rafael Sivilla-Jones" -> "Rafael A. Sivilla-Jones")
    re-indexes that judge's rulings on every other key (#4920).  Before the
    fix only the writing key's own doc was re-indexed, and the other key's
    search doc kept the old name until ``reindex_search_from_db.py --apply``
    (the Santa Clara #4905 reingest left 42 such docs)."""
    tag = f"{_RUN}-b4920"
    h = harness.KeyHarness(_DSN, _RUN, -1, frozenset({"search", "search_fields"}))
    try:
        first = harness.make_key(f"{tag}-a", county_tag=tag)
        second = harness.make_key(f"{tag}-b", county_tag=tag)
        h.run_step(first, _judge_ruling_step("live", tag, 1, "Rafael Sivilla-Jones"), "short")
        assert _judge_canonical(h, first) == ["Rafael Sivilla-Jones"]

        h.run_step(
            second, _judge_ruling_step("reingest", tag, 2, "Rafael A. Sivilla-Jones"), "long"
        )

        assert _judge_canonical(h, first) == ["Rafael A. Sivilla-Jones"]
        assert {m["judge_name"] for m in h.indexer.meta.values()} == {"Rafael A. Sivilla-Jones"}
    finally:
        h.close()


@_needs_pg
def test_judge_arbitration_reindexes_rulings_on_other_keys() -> None:
    """Arbitration (``_maybe_arbitrate``) promoting a corroborated spelling
    over the stored canonical re-indexes the judge's other rulings too
    (#4920).  This is the path that flipped Orange's "Craig L. Griffin"
    (469 rulings) during the #4907 reingest.  Since #4922 only a spelling
    with more document support than the stored one is promoted, so the
    stored name here is a typo one source read and the promoted one is
    read by two."""
    tag = f"{_RUN}-b4920arb"
    h = harness.KeyHarness(_DSN, _RUN, -1, frozenset({"search", "search_fields"}))
    try:
        first = harness.make_key(f"{tag}-a", county_tag=tag)
        second = harness.make_key(f"{tag}-b", county_tag=tag)
        h.run_step(first, _judge_ruling_step("live", tag, 1, "Craig L. Grifin"), "stored")
        assert _judge_canonical(h, first) == ["Craig L. Grifin"]
        # An earlier run read the judge as "Craig L. Griffin": a non-roster
        # alias corroborating that spelling.
        with h.checker.cursor() as cur:
            cur.execute(
                "INSERT INTO judge_aliases (judge_id, raw_name, source, confidence, is_verified) "
                "SELECT r.judge_id, 'Craig L. Griffin', 'rebuild-ca-harness', 1.0, FALSE "
                "FROM rulings r WHERE r.document_id = %s::uuid",
                (first.parent,),
            )

        h.run_step(second, _judge_ruling_step("reingest", tag, 2, "Craig L. Griffin"), "arbitrate")

        assert _judge_canonical(h, first) == ["Craig L. Griffin"]
        assert {m["judge_name"] for m in h.indexer.meta.values()} == {"Craig L. Griffin"}
    finally:
        h.close()
