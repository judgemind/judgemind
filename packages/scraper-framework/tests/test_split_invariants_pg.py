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
    events = harness.run_seed(_DSN, seed, _RUN, name=f"cover-{event}")
    assert events[event] > 0, f"seed {seed} no longer reaches {event}: {dict(events)}"


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
            )
            INSERT INTO judge_name_evidence (document_id, judge_id, raw_name)
            SELECT docs.id, r.judge_id, %s
            FROM docs, rulings r
            WHERE r.document_id = %s::uuid
            """,
            (count, key.parent, spelling, key.parent),
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
