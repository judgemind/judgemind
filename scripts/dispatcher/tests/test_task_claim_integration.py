"""Integration tests for ``scripts.dispatcher.task_claim`` + daemon claim path.

Issue #2895. The unit suite in :mod:`test_task_claim` runs against a
``MagicMock()``-stubbed psycopg module — fast, but blind to column-type
constraints. Issue #2892 landed a UUID-type mismatch between the /task
helper and the ``dispatcher.agents.agent_id`` column that the mocked
unit tests happily accepted; the bug only surfaced in production.

These tests exercise the real claim path against a real Postgres with
the real dispatcher schema loaded so future column-type contract drift
(e.g. adding a CHECK constraint on ``kind``, narrowing ``phase``, a NOT
NULL on a column the helper leaves unset) is caught on the PR, not in
production.

Scope:

* ``task_claim.do_claim`` — inserts a real row with the right shape,
  catches the partial UNIQUE INDEX on a duplicate, emits exit 3 when a
  non-UUID ``agent_id`` round-trips to the DB.
* ``task_claim.do_terminal`` — marks the row ``succeeded`` + stamps
  ``ended_at`` so the partial UNIQUE INDEX releases and the next claim
  can land.
* ``DispatcherDaemon._atomic_claim`` — the daemon's parallel write
  path. Must also fail atomically on a duplicate from the same issue
  so subagent↔daemon races are caught by the index, not racing
  pre-check-then-insert logic.

These tests require a running Postgres reachable via
``INTEGRATION_DATABASE_URL``. Locally, bring one up with:

    docker compose up -d dispatcher-test-postgres
    export INTEGRATION_DATABASE_URL=postgresql://postgres:localdev@localhost:5433/dispatcher_test
    /path/to/venv/bin/pytest scripts/dispatcher/tests/test_task_claim_integration.py -v

In CI, the ``dispatcher-integration-tests`` job in ``.github/workflows/
ci.yml`` spins up a ``postgres:16-alpine`` service and sets the env
var for the pytest run.

When the env var is unset the whole module skips cleanly — no silent
"no tests collected" — the skip reason is printed on each test.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# --------------------------------------------------------------------------
# Import path setup
# --------------------------------------------------------------------------
# Make ``scripts`` importable so ``from dispatcher import task_claim``
# resolves to ``scripts/dispatcher/task_claim.py``. Mirror the pattern
# from test_task_claim.py / test_daemon_phase3a.py.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATIONS_DIR = _REPO_ROOT / "packages" / "api" / "migrations"

# --------------------------------------------------------------------------
# Module-level skip gate
# --------------------------------------------------------------------------
# When ``INTEGRATION_DATABASE_URL`` is unset we skip the whole file at
# collection time so unit-test runs (``pytest scripts/dispatcher/tests/``
# without the env var) don't try to import psycopg against a non-existent
# DB. The skip reason is visible — ``pytest -v`` shows it per-test.

_INTEGRATION_DB_URL = os.environ.get("INTEGRATION_DATABASE_URL", "").strip()
_SKIP_REASON = (
    "INTEGRATION_DATABASE_URL is unset. To run these tests locally: "
    "``docker compose up -d dispatcher-test-postgres`` and set "
    "``INTEGRATION_DATABASE_URL=postgresql://postgres:localdev@localhost:5433/"
    "dispatcher_test``. CI sets this env var automatically via the "
    "``dispatcher-integration-tests`` job's ``services:`` block."
)

pytestmark = pytest.mark.skipif(not _INTEGRATION_DB_URL, reason=_SKIP_REASON)

# --------------------------------------------------------------------------
# psycopg loading strategy
# --------------------------------------------------------------------------
# The unit suite in the sibling ``test_task_claim.py`` / ``test_daemon_
# phase3a.py`` installs a ``MagicMock()`` psycopg stub at import time.
# Other unit tests rely on that stub being present in ``sys.modules``
# during their own test runs — purging it at this module's import time
# breaks them (collection order between modules is not guaranteed to
# match execution order, and pytest does not re-import modules between
# tests).
#
# Instead, we lazily resolve the real ``psycopg`` inside each fixture /
# helper that needs it. :func:`_real_psycopg` temporarily purges the
# stub, imports the real package from the venv's site-packages, and
# restores the stub for any later unit test. The real module handle is
# cached so repeated calls are cheap.
#
# task_claim itself is safe to import eagerly — its ``import psycopg``
# is lazy (inside each function), so it picks up whichever module is
# in ``sys.modules`` at *call* time, not import time.

from dispatcher import task_claim  # noqa: E402  — sys.path mutated above

_real_psycopg_cache: dict[str, Any] | None = None


def _capture_real_psycopg() -> dict[str, Any]:
    """Return a snapshot of the real ``psycopg`` + ``psycopg.errors`` modules.

    Temporarily purges any ``MagicMock``-shaped ``psycopg*`` entries from
    ``sys.modules``, imports the real modules from the venv's
    site-packages, and returns them as a name→module dict. Does NOT
    restore the stub — the caller is expected to install one of these
    outcomes explicitly (either keep the real in sys.modules for the
    duration of a test, or put the stub back). This keeps the caching
    story simple: we resolve the real modules once, cache them as
    objects, and the fixture chooses when they're visible.
    """
    global _real_psycopg_cache
    if _real_psycopg_cache is not None:
        return _real_psycopg_cache

    import importlib

    # Take the stub aside so importlib resolves from site-packages.
    stubbed: dict[str, Any] = {}
    for name in list(sys.modules):
        if name == "psycopg" or name.startswith("psycopg."):
            mod = sys.modules[name]
            if isinstance(mod, MagicMock):
                stubbed[name] = mod
                sys.modules.pop(name, None)

    try:
        real_psycopg = importlib.import_module("psycopg")
        real_errors = importlib.import_module("psycopg.errors")
        snapshot = {
            name: mod
            for name, mod in sys.modules.items()
            if name == "psycopg" or name.startswith("psycopg.")
        }
    finally:
        # Remove the real modules from sys.modules and restore the stubs
        # so sibling unit tests collected alongside us keep seeing them.
        for name in list(sys.modules):
            if name == "psycopg" or name.startswith("psycopg."):
                sys.modules.pop(name, None)
        for name, mod in stubbed.items():
            sys.modules[name] = mod

    # Ensure the canonical names are present in the snapshot — belt-and-
    # suspenders against an import machinery that might not populate
    # them identically on every Python version.
    snapshot.setdefault("psycopg", real_psycopg)
    snapshot.setdefault("psycopg.errors", real_errors)
    _real_psycopg_cache = snapshot
    return snapshot


@pytest.fixture(autouse=True)
def _swap_in_real_psycopg() -> Any:
    """Install the real ``psycopg`` modules in ``sys.modules`` for the test.

    task_claim and daemon both do ``import psycopg`` lazily inside each
    function, so whatever's in ``sys.modules`` at call time wins. The
    unit suite installs a ``MagicMock`` stub at module import; we swap
    it out for the real modules here and restore the stub at teardown
    so unit tests collected alongside these integration tests keep
    passing.

    Autouse so every integration test picks it up without per-test
    boilerplate. Yields the real ``psycopg`` module for callers that
    want a direct handle.
    """
    real_snapshot = _capture_real_psycopg()

    # Snapshot the current (stub) state so we can restore it at teardown.
    restore: dict[str, Any] = {}
    for name in list(sys.modules):
        if name == "psycopg" or name.startswith("psycopg."):
            restore[name] = sys.modules[name]
            sys.modules.pop(name, None)

    # Install the real modules verbatim from the snapshot.
    for name, mod in real_snapshot.items():
        sys.modules[name] = mod

    try:
        yield real_snapshot["psycopg"]
    finally:
        # Remove the real modules, then restore the stub entries.
        for name in list(sys.modules):
            if name == "psycopg" or name.startswith("psycopg."):
                sys.modules.pop(name, None)
        for name, mod in restore.items():
            sys.modules[name] = mod


def _psycopg_connect(database_url: str) -> Any:
    """Open a real psycopg connection, regardless of any active stub.

    Used by helpers that run outside a test (e.g. the ``_schema_loaded``
    session fixture, which runs before the autouse function fixture)
    and by helpers called during a test when the autouse fixture has
    already swapped the real module in. Works in both scopes because we
    always pull the real module out of the cached snapshot rather than
    trusting ``sys.modules`` state.
    """
    real_psycopg = _capture_real_psycopg()["psycopg"]
    return real_psycopg.connect(database_url, connect_timeout=10)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Earliest migration that defines the live dispatcher schema.
#: Everything before this (18/19/20 — the "dispatcher spike") was dropped
#: by migration 20. Start the rebuild from the v2 foundation.
_DISPATCHER_FOUNDATION_MIGRATION = 21


def _discover_dispatcher_migrations() -> list[Path]:
    """Return every migration that touches ``dispatcher.*``, in order.

    We auto-discover rather than hard-coding because the suite should
    validate against the *current* schema, whatever it is. Hard-coding
    the list would mean every new dispatcher migration requires an edit
    to this file — that's exactly the kind of drift the suite is meant
    to prevent.

    Selection rules:

    * File name matches ``<number>_dispatcher-<slug>.sql`` with
      ``<number>`` >= :data:`_DISPATCHER_FOUNDATION_MIGRATION`. Earlier
      numbers are the dropped spike migrations.
    * Sort numerically by the leading number prefix so ordering is
      stable regardless of lexicographic surprises (e.g. ``21`` vs
      ``210`` if a future migration ever hits that).
    """
    entries: list[tuple[int, Path]] = []
    for entry in _MIGRATIONS_DIR.iterdir():
        name = entry.name
        if not name.endswith(".sql"):
            continue
        prefix, _, rest = name.partition("_")
        if not prefix.isdigit():
            continue
        number = int(prefix)
        if number < _DISPATCHER_FOUNDATION_MIGRATION:
            continue
        if not rest.startswith("dispatcher-"):
            continue
        entries.append((number, entry))
    entries.sort(key=lambda pair: pair[0])
    return [path for _, path in entries]

#: Test issue numbers — pick values that don't collide with real repo
#: issues (above 999_000) so if someone accidentally points the test
#: suite at dev RDS the fallout is small.
_TEST_ISSUE_BASE = 999_001


def _fresh_issue_number(bump: int = 0) -> int:
    """Return a unique issue number for the current test.

    Uses ``os.getpid()`` so parallel pytest workers get different values.
    Accepts ``bump`` to let a single test use multiple distinct issues.
    """
    return _TEST_ISSUE_BASE + (os.getpid() % 1000) * 10 + bump


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def integration_db_url() -> str:
    """The integration DB URL; module-level skip ensures it's set."""
    assert _INTEGRATION_DB_URL, "module-level skip should have fired"
    return _INTEGRATION_DB_URL


@pytest.fixture(scope="session")
def _schema_loaded(integration_db_url: str) -> None:
    """Apply the dispatcher migrations once per session.

    Re-runs are safe: we DROP SCHEMA IF EXISTS first so a re-run after
    a schema-shape change (e.g. the developer just pulled migration 33)
    picks up the new shape. DROP CASCADE handles inter-table FKs in one
    statement — matches the Down Migration in migration 21.

    Note: ``CREATE SCHEMA dispatcher`` is inside migration 21 itself so
    we don't pre-create it.
    """
    migrations = _discover_dispatcher_migrations()
    assert migrations, (
        "No dispatcher migrations discovered — auto-discovery would skip every "
        "test silently. Check ``packages/api/migrations/`` and "
        "``_DISPATCHER_FOUNDATION_MIGRATION``."
    )
    with _psycopg_connect(integration_db_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS dispatcher CASCADE")
        for migration_path in migrations:
            sql = migration_path.read_text(encoding="utf-8")
            sql_up = _extract_up_migration(sql)
            with conn.cursor() as cur:
                cur.execute(sql_up)


def _extract_up_migration(sql: str) -> str:
    """Return the Up-migration half of a migration file.

    The repo's migration format uses ``-- Up Migration`` / ``-- Down
    Migration`` headers. We want to apply only the Up half, because the
    Down half contains ``DROP SCHEMA`` statements that would undo what
    we just applied.

    If no markers are present, the whole file is treated as Up.
    """
    down_marker = "-- Down Migration"
    idx = sql.find(down_marker)
    if idx == -1:
        return sql
    return sql[:idx]


@pytest.fixture
def clean_agents(_schema_loaded: None, integration_db_url: str) -> None:
    """TRUNCATE dispatcher state between tests.

    Keeps the schema intact (fast) while guaranteeing each test starts
    with an empty agents + runs table:

    * ``dispatcher.agents`` — so the partial UNIQUE INDEX on active
      rows is empty and tests can claim without "claim_lost" from
      a prior test's leftover row.
    * ``dispatcher.runs`` — so ``DispatcherDaemon.check_lease_and_
      register_run`` doesn't observe the prior test's still-active
      heartbeat and raise :class:`LeaseError`. The mark_stopped path
      is best-effort inside the daemon and a test crash can leave a
      row with NULL ``stopped_at`` and a recent heartbeat.

    ``CASCADE`` on ``agents`` also clears every child table that FKs
    into it (phase_transitions, failures, retry_markers, diagnoses,
    phase_outputs). Inexpensive on an empty integration DB.
    """
    with _psycopg_connect(integration_db_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("TRUNCATE dispatcher.agents CASCADE")
            cur.execute("TRUNCATE dispatcher.runs CASCADE")


@pytest.fixture
def database_url_env(
    monkeypatch: pytest.MonkeyPatch, integration_db_url: str
) -> str:
    """Set ``DATABASE_URL`` so ``task_claim`` uses its psycopg path.

    ``task_claim`` picks psycopg vs. ``dev-db-query.sh`` by checking
    ``os.environ["DATABASE_URL"]``. The dev-db-query.sh path is not
    reachable from a local integration test (it shells into ECS Exec),
    so we always go through psycopg here.
    """
    monkeypatch.setenv("DATABASE_URL", integration_db_url)
    return integration_db_url


# --------------------------------------------------------------------------
# task_claim.do_claim — the /task skill's write path
# --------------------------------------------------------------------------


class TestClaimInsertsRealRow:
    """Acceptance criterion #2: ``claim`` inserts a row with the right kind."""

    def test_claim_inserts_row_with_task_skill_kind(
        self,
        database_url_env: str,
        integration_db_url: str,
        clean_agents: None,
        tmp_path: Path,
    ) -> None:
        """``do_claim`` inserts a row observable via SELECT.

        Verifies the column-level contract — ``kind``, ``issue_number``,
        ``worktree_path``, ``phase``, ``status`` all populate. If a
        future migration adds a NOT NULL column the helper doesn't set,
        this test fails on the INSERT.
        """
        issue = _fresh_issue_number(0)
        agent_id = str(uuid.uuid4())

        rc = task_claim.do_claim(
            issue_number=issue,
            agent_id=agent_id,
            worktree_path=str(tmp_path),
            issue_title="integration-test",
        )
        assert rc == 0

        row = _fetch_agent_row(integration_db_url, issue)
        assert row is not None
        assert row["agent_id"] == agent_id
        assert row["kind"] == task_claim.TASK_SKILL_KIND
        assert row["issue_number"] == issue
        assert row["issue_title"] == "integration-test"
        assert row["worktree_path"] == str(tmp_path)
        assert row["phase"] == "claiming"
        assert row["status"] == "running"
        assert row["ended_at"] is None

    def test_claim_without_agent_id_generates_uuid_and_writes_sidecar(
        self,
        database_url_env: str,
        integration_db_url: str,
        clean_agents: None,
        tmp_path: Path,
    ) -> None:
        """When ``agent_id`` is None, ``do_claim`` generates a UUID, writes
        it to ``<worktree>/.task-agent-id``, and the DB row carries it.

        This is the path the /task skill actually uses in production —
        the sidecar handoff between ``claim`` and ``terminal``.
        """
        issue = _fresh_issue_number(1)

        rc = task_claim.do_claim(
            issue_number=issue,
            agent_id=None,
            worktree_path=str(tmp_path),
            issue_title=None,
        )
        assert rc == 0

        sidecar = tmp_path / task_claim.AGENT_ID_SIDECAR_NAME
        assert sidecar.exists()
        generated_agent_id = sidecar.read_text(encoding="utf-8").strip()
        assert task_claim._is_uuid(generated_agent_id)

        row = _fetch_agent_row(integration_db_url, issue)
        assert row is not None
        assert row["agent_id"] == generated_agent_id


# --------------------------------------------------------------------------
# task_claim.do_terminal — terminal UPDATE
# --------------------------------------------------------------------------


class TestTerminalUpdatesEndedAt:
    """Acceptance criterion #2: ``terminal`` updates status/ended_at."""

    def test_terminal_marks_row_succeeded_and_stamps_ended_at(
        self,
        database_url_env: str,
        integration_db_url: str,
        clean_agents: None,
        tmp_path: Path,
    ) -> None:
        """After a claim+terminal round-trip:

        * ``status`` transitions from ``running`` to ``succeeded``.
        * ``ended_at`` is populated (was NULL after claim).
        * ``pr_number`` carries the value passed to ``do_terminal``.

        This is the UPDATE shape the /task skill emits at the end of a
        successful PR merge.
        """
        issue = _fresh_issue_number(2)
        agent_id = str(uuid.uuid4())

        rc = task_claim.do_claim(issue, agent_id, str(tmp_path), None)
        assert rc == 0

        rc = task_claim.do_terminal(
            agent_id=agent_id,
            status="succeeded",
            pr_number=4242,
            worktree_path=None,
        )
        assert rc == 0

        row = _fetch_agent_row(integration_db_url, issue)
        assert row is not None
        assert row["status"] == "succeeded"
        assert row["ended_at"] is not None
        assert row["pr_number"] == 4242

    def test_terminal_releases_partial_unique_index_slot(
        self,
        database_url_env: str,
        integration_db_url: str,
        clean_agents: None,
        tmp_path: Path,
    ) -> None:
        """After a terminal UPDATE, the partial UNIQUE INDEX releases so a
        fresh claim on the same issue can land.

        The partial UNIQUE INDEX is scoped to ``status IN ('running',
        'retrying')``. Terminal flips status to ``succeeded`` which falls
        outside that scope — the slot becomes available. This is the
        property that lets /task retry a failed task on the same issue.
        """
        issue = _fresh_issue_number(3)
        first_agent = str(uuid.uuid4())
        rc = task_claim.do_claim(issue, first_agent, str(tmp_path), None)
        assert rc == 0
        rc = task_claim.do_terminal(first_agent, "failed", None, None)
        assert rc == 0

        # Second claim on the same issue — must succeed, not conflict.
        second_agent = str(uuid.uuid4())
        rc = task_claim.do_claim(issue, second_agent, str(tmp_path), None)
        assert rc == 0

        # Both rows exist, only one is currently ``running``.
        active = _count_active_agents(integration_db_url, issue)
        assert active == 1


# --------------------------------------------------------------------------
# Unique violation — the race-detection contract
# --------------------------------------------------------------------------


class TestUniqueViolationOnDuplicateClaim:
    """Acceptance criterion #2: UniqueViolation catches duplicate claim.

    This is the /task↔/task and /task↔daemon race-detection property —
    the partial UNIQUE INDEX turns a concurrent second INSERT into a
    benign ``exit 2 claim_lost`` rather than a silently duplicated row.
    """

    def test_duplicate_claim_on_active_row_returns_exit_2(
        self,
        database_url_env: str,
        clean_agents: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Two ``do_claim`` calls on the same issue_number — second returns
        exit code 2 with a ``claim_lost`` JSON payload identifying the
        owner of the index slot."""
        issue = _fresh_issue_number(4)
        first_agent = str(uuid.uuid4())

        rc = task_claim.do_claim(issue, first_agent, str(tmp_path), None)
        assert rc == 0
        capsys.readouterr()  # drop the first-claim JSON output

        # Second claim — must fail with exit 2 (claim_lost).
        second_agent = str(uuid.uuid4())
        rc = task_claim.do_claim(issue, second_agent, str(tmp_path), None)
        assert rc == 2

        import json as _json

        out = capsys.readouterr().out.strip()
        payload = _json.loads(out)
        # Owner lookup identifies the prior claim's owner.
        assert payload["owner_agent_id"] == first_agent
        assert payload["owner_kind"] == task_claim.TASK_SKILL_KIND
        assert payload["owner_status"] == "running"
        assert payload["issue_number"] == issue
        assert payload["reason"] == "claim_lost"


# --------------------------------------------------------------------------
# #2892 regression — UUID column-type contract
# --------------------------------------------------------------------------


class TestUuidColumnTypeContract:
    """Acceptance criterion #4: reintroduce the #2892 bug, confirm catch.

    #2892 was a column-type contract drift: the /task helper passed
    ``agent-<8-hex-chars>`` (a worktree basename) as ``agent_id`` while
    the column is a Postgres ``uuid NOT NULL``. The unit suite's
    MagicMock psycopg accepts any string, so the bug slipped through
    20+ PRs. The real DB surfaces the mismatch as
    ``InvalidTextRepresentation`` at INSERT time.

    This test reintroduces the bug shape and asserts the helper now
    catches it: client-side ``_is_uuid`` validation returns exit 3
    before we even round-trip to the DB. If someone regresses the
    validation the DB also returns exit 3 via the
    ``InvalidTextRepresentation`` classifier — either way the caller
    sees "configuration error, fix the caller" not "silent success".
    """

    def test_bare_hex_agent_id_short_circuits_at_validator(
        self,
        database_url_env: str,
        clean_agents: None,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The exact shape #2892 documented — ``agent-a5d7e546``."""
        issue = _fresh_issue_number(5)
        rc = task_claim.do_claim(
            issue_number=issue,
            agent_id="agent-a5d7e546",
            worktree_path=str(tmp_path),
            issue_title=None,
        )
        assert rc == 3
        err = capsys.readouterr().err
        assert "agent-a5d7e546" in err
        assert "UUID" in err

    def test_direct_insert_with_bare_hex_raises_configuration_error(
        self,
        database_url_env: str,
        clean_agents: None,
        tmp_path: Path,
    ) -> None:
        """Bypass ``do_claim``'s validator; confirm the DB itself rejects.

        This is the belt-and-suspenders half — if someone regresses the
        client-side validator, the real DB still catches the shape at
        INSERT time as ``InvalidTextRepresentation``, which the helper
        translates to :class:`ClaimConfigurationError`. The unit suite
        cannot verify this because MagicMock psycopg doesn't raise
        ``InvalidTextRepresentation`` for a bare-hex string.
        """
        issue = _fresh_issue_number(6)
        # ``_claim_via_psycopg`` is the inner DB call — bypasses
        # ``do_claim``'s ``_is_uuid`` gate. With a real Postgres, the
        # INSERT must raise ``InvalidTextRepresentation`` which the
        # helper re-raises as ``ClaimConfigurationError``.
        with pytest.raises(task_claim.ClaimConfigurationError):
            task_claim._claim_via_psycopg(
                database_url=database_url_env,
                issue_number=issue,
                agent_id="agent-a5d7e546",
                worktree_path=str(tmp_path),
                issue_title=None,
            )


# --------------------------------------------------------------------------
# Daemon parallel write path — DispatcherDaemon._atomic_claim
# --------------------------------------------------------------------------


class TestDaemonAtomicClaimIntegration:
    """Acceptance criterion bonus: ``_atomic_claim`` also covered.

    The daemon's ``_atomic_claim`` is the other end of the claim
    interlock. A helper↔daemon race (subagent claims on laptop, daemon's
    queue scan claims on Fargate) is caught by the same partial UNIQUE
    INDEX. Must validate the daemon side against the real schema too,
    otherwise the same column-type drift class can creep into the daemon
    path.
    """

    def _make_daemon(
        self,
        database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Construct a minimally-configured :class:`DispatcherDaemon`.

        Import the module freshly — lower-indexed unit tests install a
        MagicMock psycopg stub at module load, which breaks the
        daemon's real psycopg ``UniqueViolation`` catch.
        """
        sys.modules.pop("dispatcher.daemon", None)
        from dispatcher import daemon as daemon_mod  # noqa: PLC0415 — re-import

        cfg = daemon_mod.DaemonConfig(database_url=database_url, log_level="WARNING")
        log = __import__("logging").getLogger("test_integration_daemon")
        d = daemon_mod.DispatcherDaemon(cfg, log)
        d.connect()
        # Register a run so ``_atomic_claim`` has a valid ``parent_run_id``
        # to FK against.
        d.check_lease_and_register_run()
        # Stub out ``gh issue edit --add-label`` — no network access
        # from the test runner. ``_atomic_claim`` calls it after a
        # successful INSERT and swallows subprocess errors, but stubbing
        # avoids the ``gh: command not found`` log spam.
        monkeypatch.setattr(
            d,
            "_gh_issue_add_labels",
            lambda _issue, _labels: None,
        )
        return d

    def test_atomic_claim_inserts_row_and_second_attempt_is_race_lost(
        self,
        database_url_env: str,
        integration_db_url: str,
        clean_agents: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First ``_atomic_claim`` inserts; second on the same issue is
        caught by ``UniqueViolation`` and returns ``False``.

        Mirrors the daemon↔daemon race path. If the partial UNIQUE
        INDEX is ever dropped or its predicate narrowed, this test
        starts catching duplicates silently — which is the bug we
        want to detect.
        """
        d = self._make_daemon(database_url_env, monkeypatch)
        try:
            issue = _fresh_issue_number(7)
            first_agent = str(uuid.uuid4())
            assert d._atomic_claim(issue, first_agent, str(tmp_path)) is True

            # The daemon's status is ``running`` by default, so the
            # second claim on the same issue must hit the partial
            # UNIQUE INDEX and return False.
            second_agent = str(uuid.uuid4())
            assert d._atomic_claim(issue, second_agent, str(tmp_path)) is False

            # Exactly one active row, owned by the first claimant.
            active = _count_active_agents(integration_db_url, issue)
            assert active == 1
            row = _fetch_agent_row(integration_db_url, issue)
            assert row is not None
            assert row["agent_id"] == first_agent
            assert row["kind"] == "task"
        finally:
            d.close()

    def test_atomic_claim_rejects_non_uuid_agent_id(
        self,
        database_url_env: str,
        clean_agents: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#2892 regression on the daemon side.

        The daemon uses ``str(uuid.uuid4())`` today, so in the shipped
        code path this is already safe. But if a future refactor were
        to pass ``agent_id=worktree_basename`` (which is what the /task
        helper used to do), the real DB would reject the INSERT and
        ``_atomic_claim`` returns False. We assert that contract so
        any regression is caught on the PR.
        """
        d = self._make_daemon(database_url_env, monkeypatch)
        try:
            issue = _fresh_issue_number(8)
            # Bare-hex (worktree-basename shape). Real DB rejects as
            # ``InvalidTextRepresentation``; ``_atomic_claim`` treats any
            # non-UniqueViolation exception as ``claim_failed`` and
            # returns False — the caller (``_claim_and_orchestrate_one``)
            # then moves on to the next candidate.
            assert (
                d._atomic_claim(issue, "agent-a5d7e546", str(tmp_path)) is False
            )
            # No row was inserted.
            assert _fetch_agent_row(database_url_env, issue) is None
        finally:
            d.close()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _fetch_agent_row(database_url: str, issue_number: int) -> dict[str, Any] | None:
    """Return the most recent ``dispatcher.agents`` row for ``issue_number``.

    Uses ``ORDER BY started_at DESC`` so tests that insert → terminal
    → re-claim see the latest row, not the first one. Returns ``None``
    when no row exists.
    """
    with _psycopg_connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_id::text, kind, issue_number, issue_title, "
                "       worktree_path, phase, status, ended_at, pr_number "
                "FROM dispatcher.agents "
                "WHERE issue_number = %s "
                "ORDER BY started_at DESC "
                "LIMIT 1",
                (issue_number,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "agent_id": row[0],
        "kind": row[1],
        "issue_number": row[2],
        "issue_title": row[3],
        "worktree_path": row[4],
        "phase": row[5],
        "status": row[6],
        "ended_at": row[7],
        "pr_number": row[8],
    }


def _count_active_agents(database_url: str, issue_number: int) -> int:
    """Count ``running``/``retrying`` rows for an issue.

    Validates the partial UNIQUE INDEX predicate — this count must
    never exceed 1 after a successful claim.
    """
    with _psycopg_connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM dispatcher.agents "
                "WHERE issue_number = %s "
                "  AND status IN ('running', 'retrying')",
                (issue_number,),
            )
            row = cur.fetchone()
    return int(row[0]) if row is not None else 0
