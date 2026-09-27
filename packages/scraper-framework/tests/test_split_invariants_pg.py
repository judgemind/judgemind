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


_OPEN_BUGS = pytest.mark.xfail(
    reason="#4845: the worker's split write path breaks invariants until PR 3 lands",
    raises=harness.InvariantError,
    strict=False,
)


@_needs_pg
@_OPEN_BUGS
@pytest.mark.parametrize("seed", _seeds())
def test_invariants_hold(seed: int) -> None:
    harness.run_seed(_DSN, seed, _RUN)


# ---------------------------------------------------------------------------
# Reproductions of filed bugs
# ---------------------------------------------------------------------------


def _open(bug: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(
        reason=f"#{bug} open until #4845 PR 3 lands",
        raises=harness.InvariantError,
        strict=True,
    )


#: (bug, seed, invariant the seed breaks).  Found by the sweep; each run
#: enforces only that invariant, so an unrelated bug cannot mask or fake the
#: reproduction.  All of them failed before #4845 (PR 1's CI run records the
#: failures).
_BUG_SEEDS = [
    # A textless row at slot 0 shifts every ruling one slot right; the
    # write of slot j+1 finds its ruling on the earlier, never-dispatched
    # slot j, dedup supersedes j+1 and deletes j+1's ruling.
    pytest.param("4836", 9, "gap", marks=_open("4836")),
    pytest.param("4836", 11, "gap", marks=_open("4836")),
    # A ruling moves off its holder slot, whose own write is then rejected
    # by validation: the holder keeps a search doc with no ruling behind it.
    pytest.param("4837", 69, "search", marks=_open("4837")),
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
