"""Real-Postgres invariant harness for split / dedup / supersede / relink (#4845).

Random sequences of split results for one S3 key run through the live,
prefix-reingest and DB-row reingest write paths; ``split_harness`` checks the
invariants after every commit (see its docstring for the list).

- ``test_invariants_hold`` sweeps ``SPLIT_HARNESS_SEEDS`` seeds (default
  200).  ``SPLIT_HARNESS_SEED=<n>`` runs one seed.
- ``test_known_bug`` pins the seeds (and hand-built scenarios) that
  reproduce the bugs filed against this area, so each stays reproduced.
- ``test_open_bug`` pins the seeds of bugs still open (``_OPEN_BUG_SEEDS``)
  as strict xfails; the sweep runs those seeds without that invariant.
- ``test_invariants_hold_covers_evidence_paths`` checks the generated seeds
  still reach the case-number gate's branches (#4929).
- ``test_generated_fail_mode_reaches_its_branch`` and
  ``test_generated_extraction_paths`` check the generated seeds reach every
  extraction fail mode and county strategy (#4968).
- ``test_shared_court_invariants_hold`` sweeps ``SPLIT_HARNESS_SHARED_SEEDS``
  (default 100) scenarios where several keys share one court (#4969);
  ``_OPEN_SHARED_BUGS`` pins their open bugs as strict xfails.

Needs a migrated database in ``TEST_DATABASE_URL`` (``scripts/apply_migrations.sh``).
Without one every test here skips, unless ``REQUIRE_PG_TESTS=1`` (set in CI),
which turns the skip into a failure so the harness cannot silently stop
running.  Each run uses fresh keys and courts (tagged with the session's
token), so runs never see each other's rows.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from pathlib import Path

import psycopg
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


#: Seeds that break an invariant on main because of an open bug:
#: ``seed -> (issue, invariant, whole-document steps on)``.  The sweep runs
#: such a seed with every other invariant, and ``test_open_bug`` keeps the
#: bug reproduced (a strict xfail: when the fix lands it fails until the
#: entry is removed).
_OPEN_BUG_SEEDS: dict[int, tuple[str, str, bool]] = {}


@_needs_pg
@pytest.mark.parametrize("seed", _seeds())
def test_invariants_hold(seed: int) -> None:
    invariants = None
    open_bug = _OPEN_BUG_SEEDS.get(seed)
    if open_bug is not None and not open_bug[2]:
        invariants = frozenset(harness.INVARIANTS) - {open_bug[1]}
    harness.run_seed(_DSN, seed, _RUN, invariants=invariants)


@_needs_pg
@pytest.mark.parametrize(
    ("seed", "bug", "invariant", "whole"),
    [
        pytest.param(seed, *entry, id=f"{entry[0]}-{seed}")
        for seed, entry in _OPEN_BUG_SEEDS.items()
    ],
)
@pytest.mark.xfail(raises=harness.InvariantError, strict=True, reason="open bug")
def test_open_bug(seed: int, bug: str, invariant: str, whole: bool) -> None:
    harness.run_seed(
        _DSN,
        seed,
        _RUN,
        name=f"open{bug}",
        invariants=frozenset({invariant}),
        whole_documents=whole,
    )


#: A default-set seed that reaches each case-number evidence path (#4929):
#: the gate refusing an extractor's number, a reingest moving a held ruling
#: off a number its document lacks, and the gate refusing a fuzzy
#: correction.  Since #4940 a whole document's reingest moves a held ruling
#: by the split-set write too (``split_ruling_left_unsupported_case``);
#: ``ruling_left_unsupported_case`` is left to a document written whole with
#: no extractor configured (regex-only), which the harness does not
#: generate.  Since #4948 a reingest writes nothing after a failed
#: extraction.
_EVIDENCE_PATH_SEEDS = {
    "case_number_unsupported": 3,
    "split_ruling_left_unsupported_case": 8,
    "fuzzy_case_correction_unsupported": 80,
}


@_needs_pg
@pytest.mark.parametrize(("event", "seed"), sorted(_EVIDENCE_PATH_SEEDS.items()))
def test_invariants_hold_covers_evidence_paths(event: str, seed: int) -> None:
    """The generated seeds drive the case-number gate's branches, not only
    the hand-written point tests (#4929).  Each seed holds every invariant
    while it logs its path's telemetry event."""
    events = harness.run_seed(_DSN, seed, _RUN, name=f"cover-{event}", extraction_modes=False)
    assert events[event] > 0, f"seed {seed} no longer reaches {event}: {dict(events)}"


#: A default-set seed per generated fail mode (#4968), and the outcome its
#: failing step must come to: every call failing is a failed extraction;
#: the multimodal call failing with a working text fallback is complete;
#: a missing framework or county extractor that the step needs is
#: unavailable.  Each seed also holds every invariant.
_FAIL_MODE_SEEDS = {
    "all": (15, "failed", ("harness.raised.SplitExtractionFailedError",)),
    "multimodal_only": (15, "ok", ("harness.call.multimodal.fail", "harness.call.framework.ok")),
    "framework_missing": (
        6,
        "unavailable",
        ("harness.raised.ExtractorUnavailableError", "harness.refused_start.anthropic"),
    ),
    "county_missing": (
        51,
        "unavailable",
        ("harness.raised.ExtractorUnavailableError", "harness.refused_start.google"),
    ),
}


@_needs_pg
@pytest.mark.parametrize("fail_mode", harness.FAIL_MODES)
def test_generated_fail_mode_reaches_its_branch(fail_mode: str) -> None:
    """The generated seeds include every fail mode, and each reaches the
    worker branch it is for: the text fallback after a failed multimodal
    call, and the missing-extractor branch after it (#4957's production
    shape: multimodal built, the framework extractor not)."""
    seed, outcome, needed = _FAIL_MODE_SEEDS[fail_mode]
    assert any(s.fail_mode == fail_mode for s in harness.generate(seed))
    events = harness.run_seed(_DSN, seed, _RUN, name=f"mode-{fail_mode}")
    reached = [f"harness.fail_mode.{fail_mode}.{outcome}", *needed]
    assert all(events[e] > 0 for e in reached), (reached, dict(events))


#: A default-set seed per extraction path the generated ops reach (#4968),
#: with the events that show it.
_EXTRACTION_PATH_SEEDS = {
    # A multimodal county's whole document with no text and no rulings.
    "zero_rulings_no_text": (5, ("llm_split.zero_rulings_no_text_skipped",)),
    # A text-first county on its own provider's extractor (Ventura), and
    # on a provider only that county names, built at startup (#4957).
    "county_provider": (1, ("harness.call.county.ok",)),
    "county_other_provider": (39, ("harness.call.county.ok",)),
    # A text-first county's PDF with no text: nothing could be read.
    "no_extractable_text": (1, ("harness.raised.NoExtractableTextError",)),
}


@_needs_pg
@pytest.mark.parametrize(("path", "entry"), sorted(_EXTRACTION_PATH_SEEDS.items()))
def test_generated_extraction_paths(path: str, entry: tuple[int, tuple[str, ...]]) -> None:
    seed, needed = entry
    events = harness.run_seed(_DSN, seed, _RUN, name=f"path-{path}")
    assert all(events[e] > 0 for e in needed), (needed, dict(events))


@_needs_pg
def test_generated_scraper_extracted_county_runs_no_extractor() -> None:
    """A seed whose county's scraper extracts every field
    (``ExtractionMethod.NONE``, Federal's config): every step is the whole
    document and no extractor is called (#4968)."""
    seed = 18
    steps = harness.generate(seed)
    assert {s.profile for s in steps} == {"none"}
    events = harness.run_seed(_DSN, seed, _RUN, name="path-none")
    assert not [e for e in events if e.startswith("harness.call.")], dict(events)
    assert events["harness.outcome.ok"] > 0


def test_extraction_modes_keep_the_seed_s_other_streams() -> None:
    """The extraction modes come from their own random stream (#4968): a
    seed's rows, ops, model draws and step kinds are the same with them or
    without, so the seeds pinned before them keep their sequences."""
    for seed in range(200):
        on = harness.generate(seed)
        off = harness.generate(seed, extraction_modes=False)
        assert [(s.kind, s.rows, s.specs, s.ops) for s in on] == [
            (s.kind, s.rows, s.specs, s.ops) for s in off
        ]
        assert all(s.profile == "default" and not s.fail_mode for s in off)


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
    # The seeds below come from the #4929 generator (document text, LLM
    # case numbers, judge spellings); each fails on its fix's parent commit.
    #
    # The LLM split misreads CVRI2511111 as CVRI2591111 and its
    # transcription repeats the misread; the gate refutes the LLM's number
    # and the regex fallback read it back out of the transcription.  Fails
    # on c6c511d2, the parent of the #4927 fix (4a905e0).
    pytest.param("4927", 31, "evidence"),
    # The same with the prompt's example number, 2025-00912345.
    pytest.param("4927", 189, "evidence"),
    # One document printing a one-letter typo of the judge's name
    # ("H. Shaina Colovr") renamed the judge.  Fails on c1542b19, the parent
    # of the #4928 fix (5b7d6a5b).
    pytest.param("4928", 64, "judge_converge"),
    # The LLM read one ruling twice, with a typo in the judge's name; both
    # split children voted before the duplicate was superseded, so one
    # document renamed the judge.  Fails on 7a82b210, the parent of the
    # #4938 fix.  (41, 45, 120, 141, 146 and 196 fail the same way.)
    pytest.param("4938", 702, "judge_converge"),
    pytest.param("4938", 41, "judge_converge"),
    # A whole-document reingest (the extractor finds no rulings) was written
    # on its own, not as the key's split set.  Seed 21: an earlier split
    # child stayed on the key under an invented number its document does
    # not print.  Seed 107: the ruling stayed on its placeholder case when
    # the whole document found a real number.  Seed 0: a rejected document
    # kept the key's earlier ruling, where a fresh ingest stores nothing.
    # Each fails on 7a82b210, the parent of the #4940 fix.
    pytest.param("4940", 21, "evidence"),
    pytest.param("4940", 107, "converge"),
    pytest.param("4940", 0, "converge"),
    # A reingest dropped a ruling with no number and no caption; the
    # duplicate of another ruling on that slot took the dropped ruling's
    # row by (slot, placeholder case) and kept its judge.  Fails on
    # 977bad29, the parent of the #4939 fix.
    pytest.param("4939", 925, "judge_converge"),
    # The same match moved the dropped ruling's alert to the other ruling
    # on that slot's placeholder case: the third pass matched by (slot,
    # UNKNOWN case), which is evidence of the slot, not of the ruling.
    # Fails on 24d00bd6, the parent of the #4946 fix.  (232, 379, 629 and
    # 924 fail the same way.)
    pytest.param("4946", 10, "alert_case"),
    pytest.param("4946", 925, "alert_case"),
    # A PDF with no text layer in a text-first county (Ventura's config):
    # no extractor ran, the split left ``extraction_failed`` unset, and a
    # prefix reingest (139) or DB-row reingest (105) of the key raised
    # nothing, so ``--write-failed-manifest`` missed it.  Found by the
    # extraction modes' generator; fails on c88b4356, the parent of the
    # #4968 fix.
    pytest.param("4968", 139, "failed_manifest"),
    pytest.param("4968", 105, "failed_manifest"),
]


@_needs_pg
@pytest.mark.parametrize(("bug", "seed", "invariant"), _BUG_SEEDS)
def test_known_bug(bug: str, seed: int, invariant: str) -> None:
    # Seeds pinned before the extraction modes (#4968) keep their steps.
    harness.run_seed(
        _DSN,
        seed,
        _RUN,
        name=f"bug{bug}",
        invariants=frozenset({invariant}),
        extraction_modes=int(bug) >= 4968,
    )


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


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@pytest.mark.parametrize("splitter", harness.DETERMINISTIC_SPLITTERS, ids=lambda s: s.name)
def test_enrichment_case_numbers_are_per_key(splitter: harness.DeterministicSplitter) -> None:
    """The enrichment-failure keys share their splitter's real court, so each
    key gets its own case numbers (#4956): with shared numbers, parallel
    workers shared case rows and one test's retitle drifted another's search
    doc.  One key's numbers stay beyond the fuzzy matcher's reach of each
    other and fit the county's validated format."""
    from validation.deterministic import check_case_number_matches_county_format

    tags = [f"{_RUN}-{splitter.name}-{seed}" for seed in range(50)]
    numbers = {
        tag: [harness._county_case_number(splitter.county, i, tag) for i in range(6)]
        for tag in tags
    }
    every = [n for ns in numbers.values() for n in ns]
    assert len(set(every)) == len(every)
    for ns in numbers.values():
        for i, a in enumerate(ns):
            assert check_case_number_matches_county_format(a, splitter.county).result == "pass"
            assert all(_distance(a, b) > 2 for b in ns[i + 1 :]), ns


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


def _extraction_failure_seeds() -> list[int]:
    one = os.environ.get("SPLIT_HARNESS_SEED")
    if one:
        return [int(one)]
    return list(range(int(os.environ.get("SPLIT_HARNESS_FAILURE_SEEDS", "24"))))


@_needs_pg
@pytest.mark.parametrize("seed", _extraction_failure_seeds())
def test_failed_extraction_leaves_the_key_untouched(seed: int) -> None:
    """A re-ingest whose extraction fails (every API call failed) after a
    generated history leaves the key's rows and search docs exactly as they
    were (``untouched``, #4948).  Before the fix the worker wrote the whole
    document as a ruling of its own, next to the key's split children."""
    harness.run_extraction_failure(_DSN, seed, _RUN)


@_needs_pg
@pytest.mark.parametrize("seed", _extraction_failure_seeds())
def test_extractor_unavailable_leaves_the_key_untouched(seed: int) -> None:
    """A step whose required extractors cannot be built (a missing LLM
    secret), after a generated history, leaves the key's rows and search
    docs exactly as they were and raises on every step kind, a live capture
    included (#4953).  Before the fix the worker treated it as "no
    extractor configured" and wrote the whole document as a ruling."""
    harness.run_extraction_failure(_DSN, seed, _RUN, name="unavail", unavailable=True)


@_needs_pg
@pytest.mark.parametrize("seed", _extraction_failure_seeds())
def test_new_key_live_fallback_converges(seed: int) -> None:
    """A live capture of a new key whose extraction fails writes the
    document on its own (``single_document_fallback``); the generated
    sequence after it holds every invariant and converges (#4970)."""
    harness.run_new_key_fallback(_DSN, seed, _RUN)


@_needs_pg
@pytest.mark.parametrize("seed", [3, 4, 8, 15, 44])
def test_presplit_steps_reach_the_split_set_write(seed: int) -> None:
    """Generated seeds include scraper pre-split captures (#4967, #4970),
    and each child is written by the split-set write.  Before #4967 these
    seeds broke ``alert_case``, ``judge_converge`` or ``search`` on the
    pre-split path."""
    steps = harness.generate(seed)
    assert any(s.kind == "presplit" for s in steps), [s.kind for s in steps]
    from unittest.mock import patch

    from ingestion import worker as worker_mod

    real = worker_mod.IngestionWorker._write_split_batch
    calls: list[bool] = []

    def spy(self: object, event_data: dict, parent: str, batch: object) -> None:
        calls.append(batch.pre_split is not None)  # type: ignore[attr-defined]
        real(self, event_data, parent, batch)  # type: ignore[arg-type]

    with patch.object(worker_mod.IngestionWorker, "_write_split_batch", spy):
        harness.run_seed(_DSN, seed, _RUN, name="presplit-cover")
    assert any(calls), "no pre-split child reached _write_split_batch"


@_needs_pg
@pytest.mark.parametrize("kind", ["reingest", "db", "live"])
def test_known_bug_4953_extractor_unavailable_keeps_the_three_child_split(kind: str) -> None:
    """A key holding a three-child split set, re-ingested while the
    worker's extractors cannot be built, keeps exactly its three rulings and
    gains no whole-document ruling on the parent row (#4953)."""
    harness.run_extraction_failure(
        _DSN,
        -1,
        _RUN,
        name=f"bug4953-{kind}",
        steps=_three_child_split(),
        kind=kind,
        unavailable=True,
    )


def _three_child_split() -> list[harness.Step]:
    specs = tuple(
        harness.Spec(i, harness._case_number(i), harness._title(i), harness._text("b4948", i))
        for i in range(3)
    )
    rows = tuple(harness.Row("ruling", i) for i in range(3))
    doc = harness.document_text(rows, specs, "text")
    return [harness.Step("reingest", rows, specs, doc=doc)]


@_needs_pg
@pytest.mark.parametrize("kind", ["reingest", "db", "live"])
def test_known_bug_4948_failed_extraction_keeps_the_three_child_split(kind: str) -> None:
    """A key holding a three-child split set, re-ingested while the
    extraction raises ``ExtractionIncompleteError``, keeps exactly its three
    rulings and gains no whole-document ruling on the parent row (#4948).
    A prefix or DB-row reingest reports the key as failed."""
    harness.run_extraction_failure(
        _DSN, -1, _RUN, name=f"bug4948-{kind}", steps=_three_child_split(), kind=kind
    )


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


#: The number a Riverside PDF prints, and the LLM's misread of it (#4911).
_RIV_PRINTED = "CVRI2501831"
_RIV_MISREAD = "CVRI2501911"


@_needs_pg
@pytest.mark.parametrize("kind", ["live", "reingest"])
def test_regex_fallback_transcription_cannot_restore_misread(kind: str) -> None:
    """#4927: the LLM split reads ``CVRI2501911`` for a row the document
    prints as ``CVRI2501831``, and its transcription of the ruling repeats
    the misread.  The gate refutes the LLM's number; before the fix the
    regex fallback then read ``CVRI2501911`` back out of the transcription
    (the LLM's own output) and the ruling was filed under it.  Every
    candidate is now checked against the document's text, so the ruling
    lands on its ``UNKNOWN-`` case and its sibling keeps its printed one."""
    h = harness.KeyHarness(_DSN, _RUN, -1)
    try:
        key = harness.make_key(f"{_RUN}-b4927-{kind}")
        sibling = harness._case_number(1)
        h.event_extra = {
            "ruling_text": (
                "SUPERIOR COURT OF CALIFORNIA, COUNTY OF RIVERSIDE. Tentative rulings. "
                f"1. {_RIV_PRINTED} {harness._title(0)}. 2. {sibling} {harness._title(1)}. "
                + "The court has read and considered the papers filed. "
                * 4
            )
        }
        transcription = (
            f"Plaintiff's motion in {_RIV_MISREAD} is GRANTED. Defendant shall serve "
            "further responses within 20 days."
        )
        specs = (
            harness.Spec(0, _RIV_MISREAD, harness._title(0), transcription),
            harness.Spec(1, sibling, harness._title(1), harness._text("b4927", 1)),
        )
        rows = (harness.Row("ruling", 0), harness.Row("ruling", 1))
        h.run_step(key, harness.Step(kind, rows, specs), "misread")
        cases = sorted(case for _rid, case, _alert in _rulings_with_alerts(h, key))
    finally:
        h.close()
    assert _RIV_MISREAD not in cases
    assert cases[0] == sibling
    assert cases[1].startswith("UNKNOWN-")


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


def _add_name_evidence(h: harness.KeyHarness, key: harness.Key, spelling: str, count: int) -> None:
    """Record *count* other documents that printed *spelling* for *key*'s judge.

    Arbitration counts documents (#4928): a rename needs a spelling printed
    on more documents than the stored one, so the tests seed earlier ones.
    Each is an active document with a ruling on the judge: a vote counts
    only while it describes its document (#4966).
    """
    with h.checker.cursor() as cur:
        cur.execute(
            """
            WITH docs AS (
                INSERT INTO documents (court_id, document_type, s3_key, s3_bucket, format,
                                       content_hash, captured_at)
                SELECT d.court_id, 'tentative_ruling',
                       'test/evidence/' || gen_random_uuid()::text, 'test', 'pdf',
                       md5(random()::text), NOW()
                FROM documents d, generate_series(1, %s)
                WHERE d.id = %s::uuid
                RETURNING id
            ), held AS (
                INSERT INTO rulings (document_id, case_id, court_id, judge_id, is_tentative)
                SELECT docs.id, r.case_id, r.court_id, r.judge_id, TRUE
                FROM docs, rulings r
                WHERE r.document_id = %s::uuid
                RETURNING document_id, judge_id
            )
            INSERT INTO judge_name_evidence (document_id, judge_id, raw_name)
            SELECT held.document_id, held.judge_id, %s
            FROM held
            """,
            (count, key.parent, key.parent, spelling),
        )


@_needs_pg
def test_judge_rename_reindexes_rulings_on_other_keys() -> None:
    """A write that lengthens a judge's canonical name (``resolve_judge``'s
    near-duplicate step, "Rafael Sivilla-Jones" -> "Rafael A. Sivilla-Jones")
    re-indexes that judge's rulings on every other key (#4920).  Before the
    fix only the writing key's own doc was re-indexed, and the other key's
    search doc kept the old name until ``reindex_search_from_db.py --apply``
    (the Santa Clara #4905 reingest left 42 such docs).  Since #4928 the
    longer spelling needs the support of more documents than the stored one,
    so two earlier documents printed it."""
    tag = f"{_RUN}-b4920"
    h = harness.KeyHarness(_DSN, _RUN, -1, frozenset({"search", "search_fields"}))
    try:
        first = harness.make_key(f"{tag}-a", county_tag=tag)
        second = harness.make_key(f"{tag}-b", county_tag=tag)
        h.run_step(first, _judge_ruling_step("live", tag, 1, "Rafael Sivilla-Jones"), "short")
        assert _judge_canonical(h, first) == ["Rafael Sivilla-Jones"]
        _add_name_evidence(h, first, "Rafael A. Sivilla-Jones", 2)

        h.run_step(
            second, _judge_ruling_step("reingest", tag, 2, "Rafael A. Sivilla-Jones"), "long"
        )

        assert _judge_canonical(h, first) == ["Rafael A. Sivilla-Jones"]
        assert {m["judge_name"] for m in h.indexer.meta.values()} == {"Rafael A. Sivilla-Jones"}
    finally:
        h.close()


@_needs_pg
def test_one_document_with_a_longer_name_does_not_rename() -> None:
    """#4928: before, one document with an extra initial lengthened the name
    and re-indexed every ruling of the judge."""
    tag = f"{_RUN}-b4928"
    h = harness.KeyHarness(_DSN, _RUN, -1, frozenset({"search", "search_fields"}))
    try:
        first = harness.make_key(f"{tag}-a", county_tag=tag)
        second = harness.make_key(f"{tag}-b", county_tag=tag)
        h.run_step(first, _judge_ruling_step("live", tag, 1, "Rafael Sivilla-Jones"), "short")
        h.run_step(
            second, _judge_ruling_step("reingest", tag, 2, "Rafael A. Sivilla-Jones"), "long"
        )
        assert _judge_canonical(h, first) == ["Rafael Sivilla-Jones"]
        assert _judge_canonical(h, second) == ["Rafael Sivilla-Jones"]
    finally:
        h.close()


@_needs_pg
def test_judge_arbitration_reindexes_rulings_on_other_keys() -> None:
    """Arbitration (``_maybe_arbitrate``) promoting a better-supported spelling
    over the stored canonical re-indexes the judge's other rulings too
    (#4920).  This is the path that flipped Orange's "Craig L. Griffin"
    (469 rulings) during the #4907 reingest.  Since #4928 only a spelling
    printed on decisively more documents than the stored one is promoted:
    the stored name here is a typo one document printed, the promoted one
    is printed on three."""
    tag = f"{_RUN}-b4920arb"
    h = harness.KeyHarness(_DSN, _RUN, -1, frozenset({"search", "search_fields"}))
    try:
        first = harness.make_key(f"{tag}-a", county_tag=tag)
        second = harness.make_key(f"{tag}-b", county_tag=tag)
        h.run_step(first, _judge_ruling_step("live", tag, 1, "Craig L. Grifin"), "stored")
        assert _judge_canonical(h, first) == ["Craig L. Grifin"]
        # An earlier run read the judge as "Craig L. Griffin" on two
        # documents.  The alias routes that spelling to the judge (and
        # enrichment swaps it for the canonical name before resolve_judge,
        # which must still count the printed spelling).
        with h.checker.cursor() as cur:
            cur.execute(
                "INSERT INTO judge_aliases (judge_id, raw_name, source, confidence, is_verified) "
                "SELECT r.judge_id, 'Craig L. Griffin', 'rebuild-ca-harness', 1.0, FALSE "
                "FROM rulings r WHERE r.document_id = %s::uuid",
                (first.parent,),
            )
        _add_name_evidence(h, first, "Craig L. Griffin", 2)

        h.run_step(second, _judge_ruling_step("reingest", tag, 2, "Craig L. Griffin"), "arbitrate")

        assert _judge_canonical(h, first) == ["Craig L. Griffin"]
        assert {m["judge_name"] for m in h.indexer.meta.values()} == {"Craig L. Griffin"}
    finally:
        h.close()


# ---------------------------------------------------------------------------
# #4966: a document's judge vote follows the document
# ---------------------------------------------------------------------------


def _votes(h: harness.KeyHarness, key: harness.Key) -> dict[str, tuple[str, str]]:
    """``{slot label: (judge's canonical name, voted spelling)}`` of the
    evidence rows of every document id on *key*, deleted ones included."""
    ids = [key.parent, *key.slots]
    with h.checker.cursor() as cur:
        cur.execute(
            "SELECT e.document_id::text, j.canonical_name, e.raw_name "
            "FROM judge_name_evidence e JOIN judges j ON j.id = e.judge_id "
            "WHERE e.document_id = ANY(%s::uuid[])",
            (ids,),
        )
        return {key.label(doc): (name, raw) for doc, name, raw in cur.fetchall()}


def _rulings_step(kind: str, specs: list[harness.Spec], rows: list[int]) -> harness.Step:
    return harness.Step(kind, tuple(harness.Row("ruling", i) for i in rows), tuple(specs))


def _spec(tag: str, idx: int, judge: str | None) -> harness.Spec:
    return harness.Spec(
        idx, harness._case_number(idx), harness._title(idx), harness._text(tag, idx), judge
    )


@_needs_pg
@pytest.mark.parametrize("kind", ["live", "reingest"])
def test_ruling_that_loses_its_judge_stops_voting(kind: str) -> None:
    """#4966: the same ruling re-extracted without a judge keeps its row
    (content match) and ``force_update`` clears its ``judge_id``, but its
    document kept voting for the old judge.  Its vote is removed in the
    same transaction."""
    tag = f"{_RUN}-b4966-lose-{kind}"
    h = harness.KeyHarness(_DSN, _RUN, -1, frozenset({"judge_converge"}))
    try:
        key = harness.make_key(tag)
        h.run_step(key, _rulings_step(kind, [_spec(tag, 1, "Craig L. Griffin")], [0]), "judge")
        assert list(_votes(h, key).values()) == [("Craig L. Griffin", "Craig L. Griffin")]

        h.run_step(key, _rulings_step("reingest", [_spec(tag, 1, None)], [0]), "no judge")

        assert _judge_canonical(h, key) == []
        assert _votes(h, key) == {}
    finally:
        h.close()


@_needs_pg
def test_removed_and_superseded_documents_stop_voting() -> None:
    """#4966: a split slot the new split set no longer writes is deleted
    (``delete_stale_split_children``), and a slot whose ruling becomes a
    duplicate of an earlier one is superseded; neither keeps voting."""
    tag = f"{_RUN}-b4966-retire"
    h = harness.KeyHarness(_DSN, _RUN, -1, frozenset({"judge_converge"}))
    try:
        key = harness.make_key(tag)
        specs = [
            _spec(tag, 1, "Craig L. Griffin"),
            _spec(tag, 2, "Shaina H. Colover"),
            _spec(tag, 3, "Rafael Sivilla-Jones"),
        ]
        h.run_step(key, _rulings_step("reingest", specs, [0, 1, 2]), "three")
        assert set(_votes(h, key)) == {"S0", "S1", "S2"}

        # Slot 1 repeats slot 0's ruling (superseded as a duplicate); slot 2
        # is gone (deleted).
        h.run_step(key, _rulings_step("reingest", specs, [0, 0]), "duplicate")

        with h.checker.cursor() as cur:
            cur.execute(
                "SELECT id::text, status::text FROM documents WHERE id = ANY(%s::uuid[])",
                ([key.parent, *key.slots],),
            )
            status = {key.label(doc): s for doc, s in cur.fetchall()}
        assert status.get("S1") == "superseded"
        assert "S2" not in status
        assert _votes(h, key) == {"S0": ("Craig L. Griffin", "Craig L. Griffin")}
    finally:
        h.close()


@_needs_pg
def test_judge_that_loses_votes_is_rearbitrated() -> None:
    """#4966: a judge whose votes move away is arbitrated again in the same
    transaction.  "Craig L. Grifin" (two documents) holds against "Craig L.
    Griffin" (three documents, not decisive); once one Grifin document's
    ruling loses its judge, Griffin leads decisively and the judge is
    renamed, its rulings on other keys re-indexed.  Before the fix only the
    judges a write voted for were arbitrated, so the judge kept the name."""
    tag = f"{_RUN}-b4966-rearb"
    h = harness.KeyHarness(_DSN, _RUN, -1, frozenset({"search", "search_fields"}))
    try:
        first = harness.make_key(f"{tag}-a", county_tag=tag)
        second = harness.make_key(f"{tag}-b", county_tag=tag)
        h.run_step(first, _judge_ruling_step("reingest", tag, 1, "Craig L. Grifin"), "a")
        h.run_step(second, _judge_ruling_step("reingest", tag, 2, "Craig L. Grifin"), "b")
        _add_name_evidence(h, first, "Craig L. Griffin", 3)
        assert _judge_canonical(h, first) == ["Craig L. Grifin"]

        h.run_step(second, _judge_ruling_step("reingest", tag, 2, None), "b loses judge")

        assert _judge_canonical(h, second) == []
        assert _judge_canonical(h, first) == ["Craig L. Griffin"]
        on_first = {first.parent, *first.slots}
        assert {m["judge_name"] for d, m in h.indexer.meta.items() if d in on_first} == {
            "Craig L. Griffin"
        }
    finally:
        h.close()


# ---------------------------------------------------------------------------
# #4935: a case retitle re-indexes the case's rulings on other keys
# ---------------------------------------------------------------------------


@_needs_pg
@pytest.mark.parametrize("first_kind", ["live", "reingest"])
@pytest.mark.parametrize("second_kind", ["live", "reingest"])
def test_case_retitle_reindexes_rulings_on_other_keys(first_kind: str, second_kind: str) -> None:
    """A write that gives a shared case its title re-indexes that case's
    rulings on every other key (#4935).  Before the fix only the writing
    key's docs were re-indexed: the Orange rebuild for #4926 left 21 docs
    with a stale case title or type until ``reindex_search_from_db.py --apply``."""
    harness.run_shared_case_retitle(_DSN, _RUN, first_kind, second_kind)


# ---------------------------------------------------------------------------
# #4969: several keys share one court
# ---------------------------------------------------------------------------


def _shared_seeds() -> list[int]:
    one = os.environ.get("SPLIT_HARNESS_SEED")
    if one:
        return [int(one)]
    return list(range(int(os.environ.get("SPLIT_HARNESS_SHARED_SEEDS", "100"))))


#: Open bugs the shared-court scenarios reproduce: ``issue -> (invariant,
#: seeds)``.  The bug class is broad (about one shared seed in six), so the
#: sweep runs every shared seed without that invariant, and
#: ``test_open_shared_bug`` keeps the bug reproduced on *seeds* (a strict
#: xfail: when the fix lands it fails until the entry is removed, which
#: turns the invariant back on in the sweep).
_OPEN_SHARED_BUGS: dict[str, tuple[str, tuple[int, ...]]] = {
    # A repost loses content-hash dedup to the key holding the ruling; when
    # that key stops listing it, the reposting key's document stays
    # superseded and the ruling exists nowhere.
    "4982": ("printed_somewhere", (7, 8, 9, 17, 23)),
}


def _shared_invariants() -> frozenset[str]:
    return frozenset(harness.INVARIANTS) - {inv for inv, _ in _OPEN_SHARED_BUGS.values()}


@_needs_pg
@pytest.mark.parametrize("seed", _shared_seeds())
def test_shared_court_invariants_hold(seed: int) -> None:
    """Two or three keys share one court, its cases and its judge roster
    (#4969): reposts of one ruling under a second key, court renames, and
    rulings that lose their judge while sibling keys keep voting.  Every
    per-step invariant holds, and the court-wide ones (``search_fields``,
    ``judge_converge``, ``printed_somewhere``, ``held_printed``) hold
    across all the keys."""
    harness.run_shared_court(_DSN, seed, _RUN, invariants=_shared_invariants())


@_needs_pg
@pytest.mark.parametrize(
    ("bug", "invariant", "seed"),
    [
        pytest.param(bug, invariant, seed, id=f"{bug}-{seed}")
        for bug, (invariant, seeds) in _OPEN_SHARED_BUGS.items()
        for seed in seeds
    ],
)
@pytest.mark.xfail(raises=harness.InvariantError, strict=True, reason="open bug")
def test_open_shared_bug(bug: str, invariant: str, seed: int) -> None:
    harness.run_shared_court(_DSN, seed, _RUN, name=f"open{bug}", invariants=frozenset({invariant}))


def test_generate_shared_keeps_the_single_key_sequences() -> None:
    """The shared-court generator draws from its own random stream: adding
    it left every single-key seed's sequence as it was (#4969)."""
    before = [harness.generate(seed) for seed in range(20)]
    for seed in range(20):
        harness.generate_shared(seed)
    assert [harness.generate(seed) for seed in range(20)] == before
    assert harness.generate_shared(5) == harness.generate_shared(5)


@_needs_pg
@pytest.mark.parametrize("seed", [0, 5, 10])
def test_court_rename_reindexes_rulings_on_other_keys(seed: int) -> None:
    """A step whose events name the court differently renames it
    (``upsert_court``'s change branch), and the rename re-indexes the
    court's rulings on the other keys (#4935, #4969).  Before #4969 every
    generated key had its own court and always sent "Superior Court", so
    the branch never fired."""
    from unittest.mock import patch

    from ingestion import worker as worker_mod

    real = worker_mod.reindex_dependent_rulings
    renamed: list[int] = []

    def spy(conn: object, indexer: object, changes: object, **kwargs: object) -> int:
        count = real(conn, indexer, changes, **kwargs)  # type: ignore[arg-type]
        if changes.court_ids:  # type: ignore[attr-defined]
            renamed.append(count)
        return count

    _keys, steps = harness.generate_shared(seed)
    assert any("court_rename" in s.step.ops for s in steps)
    with patch.object(worker_mod, "reindex_dependent_rulings", spy):
        harness.run_shared_court(
            _DSN, seed, _RUN, name="court-rename", invariants=_shared_invariants()
        )
    assert any(count > 0 for count in renamed), renamed


class _DedupLog(logging.Handler):
    """Counts ``insert_ruling``'s content-hash dedup supersedes."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "event", None) == "content_hash_dedup_supersede":
            self.count += 1


@_needs_pg
@pytest.mark.parametrize("seed", [0, 1, 24])
def test_repost_on_another_key_loses_dedup_to_the_holder(seed: int) -> None:
    """A ruling reposted under a second key of the court (the same case
    and text) loses content-hash dedup to the key holding it: its document
    is superseded (#2458).  Generated seeds reach the cross-key supersede
    and hold every other invariant (#4969)."""
    log = _DedupLog()
    logger = logging.getLogger("ingestion")
    logger.addHandler(log)
    try:
        harness.run_shared_court(_DSN, seed, _RUN, name="repost", invariants=_shared_invariants())
    finally:
        logger.removeHandler(log)
    assert log.count > 0


@_needs_pg
@pytest.mark.parametrize("seed", [0, 2, 3])
def test_ruling_that_loses_its_judge_while_other_keys_vote(seed: int) -> None:
    """A ruling loses its judge on one key while the court's other keys
    keep voting for that judge (#4966, #4969): every document votes as it
    stands, and the judge keeps the name its remaining votes give it."""
    n_keys, steps = harness.generate_shared(seed)
    assert any("judge_none" in s.step.ops for s in steps)
    harness.run_shared_court(_DSN, seed, _RUN, name="judge-none", invariants=_shared_invariants())
    tag = f"{_RUN}-{seed}-judge-none"
    keys = [harness.make_key(f"{tag}-k{k}", county_tag=tag) for k in range(n_keys)]
    with psycopg.connect(_DSN) as conn:
        rows = conn.execute(
            "SELECT e.judge_id::text, count(DISTINCT d.s3_key) FROM judge_name_evidence e "
            "JOIN documents d ON d.id = e.document_id WHERE d.s3_key = ANY(%s) "
            "GROUP BY e.judge_id",
            ([k.s3_key for k in keys],),
        ).fetchall()
    keys_per_judge = dict(rows)
    assert max(keys_per_judge.values(), default=0) >= 2, keys_per_judge


@_needs_pg
@pytest.mark.parametrize("kind", ["reingest", "live"])
@pytest.mark.xfail(raises=harness.InvariantError, strict=True, reason="open bug #4982")
def test_repost_loser_is_revived_when_the_holder_drops_it(kind: str) -> None:
    """Key A holds ruling R; key B reposts R (the same case and text) and
    loses dedup, so B's document is superseded; then A is re-ingested
    without R.  B still prints R, so R must be held by B (#4969).  Today
    nothing revives B and R exists nowhere (#4982)."""
    tag = f"{_RUN}-revive-{kind}"
    h = harness.KeyHarness(_DSN, _RUN, -1, frozenset({"printed_somewhere"}))
    try:
        a = harness.make_key(f"{tag}-a", county_tag=tag)
        b = harness.make_key(f"{tag}-b", county_tag=tag)
        shared = _spec(tag, 1, None)
        own_a, own_b = _spec(f"{tag}-a", 2, None), _spec(f"{tag}-b", 3, None)
        h.run_step(a, _rulings_step("reingest", [shared, own_a], [0, 1]), "a holds R")
        h.run_step(b, _rulings_step("reingest", [shared, own_b], [0, 1]), "b reposts R")
        on_b = harness.read_key(h.checker, b.s3_key)
        assert shared.text not in {r.text for r in on_b}
        assert any(r.status == "superseded" for r in on_b)
        h.run_step(a, _rulings_step(kind, [own_a], [0]), "a drops R")
        printed = {a.s3_key: {own_a.text}, b.s3_key: {shared.text, own_b.text}}
        h._check_printed_somewhere([a, b], printed)
    finally:
        h.close()
