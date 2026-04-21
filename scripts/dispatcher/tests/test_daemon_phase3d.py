"""Unit tests for the Phase 3D diagnoser (issue #2795).

Covers the §8 tier-2/3 diagnosis step:

* Tier-2 detection fires ONLY when a mechanical-retry failure category
  (``stuck_timeout`` / ``gh_rate_exhausted`` / ``subprocess_crash``)
  recurs for the same agent within 24h of a prior same-category
  failure. First occurrence of a tier-2 recurrence category does NOT
  fire (the tier-1 mechanical retry handles it).
* Tier-2 detection fires on **first** occurrence for
  ``subprocess_turn_limit`` (the 3D diagnoser directly chooses
  retry_with_hint vs escalate).
* Tier-3 detection fires immediately for ``ci_red_after_retries``.
* The daemon writes a ``dispatcher.diagnoses (status='pending')`` row
  with a full context bundle before spawning the subprocess.
* Each of the 5 recommendation actions is consumed deterministically:
    - ``retry`` → create retry marker, no GH side effects.
    - ``retry_with_hint`` → ``gh issue comment`` + retry marker.
    - ``reissue`` → ``gh issue comment`` + ``gh issue edit --body-file``
      + ``gh issue edit --add-label agent/ready`` + retry marker.
    - ``escalate`` → ``gh issue comment`` + add labels
      ``status/needs-human,priority/p1`` + agent status='failed'.
    - ``close`` → ``gh issue edit --add-label status/invalid`` +
      ``gh issue close --reason 'not planned'`` + agent status='failed'.
* Malformed recommendation JSON / unknown action / subprocess timeout /
  subprocess non-zero exit all flip the diagnosis to
  ``status='failed'`` AND fall back to the fixed mechanical
  escalation policy (no retry, add needs-human label).
* Circuit breaker: with total diagnoses ≥ 5 and failed fraction >
  threshold, ``dispatcher.config.diagnoser_enabled`` flips to
  ``'false'``. Insufficient samples or healthy fallback rate do NOT
  trip the breaker.
* Migration 26 seeds ``diagnoser_enabled`` + ``diagnoser_fallback_rate_threshold``.

3B fix-CI exhaustion path now writes a
``dispatcher.failures (category='ci_red_after_retries')`` row BEFORE
flipping the agent to ``status='failed'`` so the tier-3 detector can
pick it up.

All external calls (``gh``, ``claude -p``, psycopg) are mocked.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# Install a psycopg stub whose ``errors.UniqueViolation`` is a real
# Exception subclass — matches the pattern in earlier phase test files.
if "psycopg" not in sys.modules or not isinstance(
    getattr(sys.modules["psycopg"].errors, "UniqueViolation", None), type
):

    class _UniqueViolation(Exception):
        """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""

    _psycopg_stub = MagicMock()
    _psycopg_errors = MagicMock()
    _psycopg_errors.UniqueViolation = _UniqueViolation
    _psycopg_stub.errors = _psycopg_errors
    sys.modules["psycopg"] = _psycopg_stub

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — a queue-driven FakeCursor with both fetchone and fetchall.
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.fetch_queue: list[Any] = []
        self.fetchall_queue: list[list[Any]] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if not self.fetch_queue:
            return None
        return self.fetch_queue.pop(0)

    def fetchall(self) -> list[Any]:
        if not self.fetchall_queue:
            return []
        return self.fetchall_queue.pop(0)


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.autocommit = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _CapturingLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        return [r for r in self.records if getattr(r, "event", None) == name]


def _make_daemon(
    tmp_path: Path,
) -> tuple[daemon.DispatcherDaemon, _FakeConnection, _CapturingLogHandler]:
    handler = _CapturingLogHandler()
    logger = logging.getLogger(f"dispatcher.test.phase3d.{id(tmp_path)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    conn = _FakeConnection()
    cfg = daemon.DaemonConfig(
        database_url="postgres://fake",
        version_sha="deadbee",
        host="test-host",
        pid=9999,
        github_repo="judgemind/judgemind",
        dispatcher_service_name="judgemind-dispatcher-test",
    )
    d = daemon.DispatcherDaemon(cfg, logger)
    d._conn = conn  # type: ignore[assignment]
    d._run_id = "test-run-id"
    return d, conn, handler


# --------------------------------------------------------------------------
# Migration 26 — config seeds (read-path verification)
# --------------------------------------------------------------------------


class TestMigration26ConfigSeeds:
    """Migration 26 seeds ``diagnoser_enabled`` and ``diagnoser_fallback_rate_threshold``.

    Schema.sql is pg_dump --schema-only (no seed data), so the canonical
    check is on the migration SQL file itself.
    """

    def test_migration_file_present(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        migration_path = (
            repo_root
            / "packages"
            / "api"
            / "migrations"
            / "26_dispatcher-diagnoser-config-seeds.sql"
        )
        assert migration_path.exists(), f"Migration 26 file missing at {migration_path}"

    def test_migration_seeds_both_keys(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        migration_path = (
            repo_root
            / "packages"
            / "api"
            / "migrations"
            / "26_dispatcher-diagnoser-config-seeds.sql"
        )
        sql = migration_path.read_text(encoding="utf-8")
        assert "diagnoser_enabled" in sql
        assert "diagnoser_fallback_rate_threshold" in sql
        assert "INSERT INTO dispatcher.config" in sql
        # Default value for enabled is 'true' (string form of a JSON bool).
        assert "'true'" in sql
        # Default threshold is 0.30.
        assert "'0.30'" in sql
        # Idempotent seed — ON CONFLICT DO NOTHING keeps re-runs safe.
        assert "ON CONFLICT" in sql

    def test_migration_has_down_migration(self) -> None:
        repo_root = Path(__file__).resolve().parents[3]
        migration_path = (
            repo_root
            / "packages"
            / "api"
            / "migrations"
            / "26_dispatcher-diagnoser-config-seeds.sql"
        )
        sql = migration_path.read_text(encoding="utf-8")
        assert "Down Migration" in sql
        assert "DELETE FROM dispatcher.config" in sql


# --------------------------------------------------------------------------
# _diagnoser_enabled — config read + default true
# --------------------------------------------------------------------------


class TestDiagnoserEnabled:
    def test_default_true_when_row_missing(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._diagnoser_enabled() is True

    def test_true_when_row_is_true(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(True,)]
        assert d._diagnoser_enabled() is True

    def test_false_when_row_is_false(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(False,)]
        assert d._diagnoser_enabled() is False

    def test_false_when_row_is_json_string_false(self, tmp_path: Path) -> None:
        # psycopg may hand us a raw JSON string when the column is JSONB.
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("false",)]
        assert d._diagnoser_enabled() is False

    def test_defaults_true_on_malformed(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("not-json{",)]
        assert d._diagnoser_enabled() is True


class TestFallbackThreshold:
    def test_reads_float(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("0.50",)]
        assert d._diagnoser_fallback_threshold() == 0.50

    def test_reads_number(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0.25,)]
        assert d._diagnoser_fallback_threshold() == 0.25

    def test_default_when_row_missing(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._diagnoser_fallback_threshold() == (
            daemon.DEFAULT_CIRCUIT_BREAKER_THRESHOLD
        )

    def test_default_when_out_of_range(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(1.5,)]
        assert d._diagnoser_fallback_threshold() == (
            daemon.DEFAULT_CIRCUIT_BREAKER_THRESHOLD
        )


# --------------------------------------------------------------------------
# _check_diagnoser_circuit_breaker — §8 budget & safety
# --------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_does_not_trip_when_total_below_min(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # 3 total, all failed — below CIRCUIT_BREAKER_MIN_DIAGNOSES (5).
        # First fetch: threshold lookup (threshold read happens first).
        conn.cursor_instance.fetch_queue = [
            ("0.30",),  # threshold
            (3, 3),  # failed_n, total_n
        ]
        tripped = d._check_diagnoser_circuit_breaker()
        assert tripped is False
        assert not handler.events("diagnoser_circuit_breaker_tripped")
        # No UPDATE fired.
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
        ]
        assert updates == []

    def test_does_not_trip_when_rate_at_or_below_threshold(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # 10 total, 3 failed → 0.30 rate, equal to threshold → no trip
        # (spec says "> 30%", not "≥ 30%").
        conn.cursor_instance.fetch_queue = [
            ("0.30",),
            (3, 10),
        ]
        tripped = d._check_diagnoser_circuit_breaker()
        assert tripped is False
        assert not handler.events("diagnoser_circuit_breaker_tripped")

    def test_trips_when_rate_above_threshold_and_min_samples(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # 10 total, 4 failed → 0.40 rate > 0.30 → trip.
        conn.cursor_instance.fetch_queue = [
            ("0.30",),
            (4, 10),
        ]
        tripped = d._check_diagnoser_circuit_breaker()
        assert tripped is True
        events = handler.events("diagnoser_circuit_breaker_tripped")
        assert events
        assert events[0].fallback_rate == 0.4
        # UPDATE fired against diagnoser_enabled.
        updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.config" in e[0]
        ]
        assert len(updates) == 1
        assert updates[0][1] == ("diagnoser_enabled",)

    def test_db_scan_error_does_not_crash(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        call_count = {"n": 0}

        def boom(sql: str, params: Any = None) -> None:
            call_count["n"] += 1
            # First call is the threshold read (succeeds fine in fetchone);
            # fail on the 2nd call which is the COUNT aggregation.
            if call_count["n"] >= 2:
                raise RuntimeError("db lost")

        conn.cursor_instance.fetch_queue = [("0.30",)]
        conn.cursor_instance.execute = boom  # type: ignore[method-assign]
        assert d._check_diagnoser_circuit_breaker() is False
        assert handler.events("diagnoser_circuit_breaker_scan_failed")


# --------------------------------------------------------------------------
# Tier detection
# --------------------------------------------------------------------------


class TestTierDetection:
    def _seed_failures_fetch(self, conn: _FakeConnection, rows: list[Any]) -> None:
        """Seed the candidate-scan fetchall queue."""
        conn.cursor_instance.fetchall_queue = [rows]

    def test_tier_3_ci_red_after_retries_fires_immediately(
        self, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        # One tier-3 failure row.
        failure_row = (
            42,  # failure_id
            "agent-1",  # agent_id
            daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,  # category
            {"issue_number": 999, "pr_number": 101},  # details
            datetime(2026, 4, 18, tzinfo=UTC),  # ts
        )
        self._seed_failures_fetch(conn, [failure_row])
        candidates = d._find_diagnoser_candidates()
        assert len(candidates) == 1
        assert candidates[0]["tier"] == 3
        assert candidates[0]["category"] == (
            daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES
        )
        assert candidates[0]["issue_number"] == 999

    def test_tier_2_turn_limit_fires_on_first_occurrence(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        failure_row = (
            100,
            "agent-x",
            daemon.FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT,
            {"issue_number": 42},
            datetime(2026, 4, 18, tzinfo=UTC),
        )
        self._seed_failures_fetch(conn, [failure_row])
        # No recurrence check expected for turn-limit — it's
        # TIER_2_FIRST_OCCURRENCE.
        candidates = d._find_diagnoser_candidates()
        assert len(candidates) == 1
        assert candidates[0]["tier"] == 2
        assert candidates[0]["category"] == (
            daemon.FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT
        )

    def test_tier_2_recurrence_requires_prior_failure(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        failure_row = (
            100,
            "agent-x",
            daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH,
            {"issue_number": 42},
            datetime(2026, 4, 18, tzinfo=UTC),
        )
        self._seed_failures_fetch(conn, [failure_row])
        # Recurrence check returns None → first occurrence → skip.
        conn.cursor_instance.fetch_queue = [None]
        candidates = d._find_diagnoser_candidates()
        assert candidates == []

    def test_tier_2_recurrence_fires_on_second_occurrence(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        failure_row = (
            200,
            "agent-x",
            daemon.FAILURE_CATEGORY_STUCK_TIMEOUT,
            {"issue_number": 55},
            datetime(2026, 4, 18, tzinfo=UTC),
        )
        self._seed_failures_fetch(conn, [failure_row])
        # Recurrence check returns a row → prior same-category failure exists.
        conn.cursor_instance.fetch_queue = [(1,)]
        candidates = d._find_diagnoser_candidates()
        assert len(candidates) == 1
        assert candidates[0]["tier"] == 2
        assert candidates[0]["category"] == (daemon.FAILURE_CATEGORY_STUCK_TIMEOUT)

    def test_daemon_level_failure_ignored(self, tmp_path: Path) -> None:
        # agent_id=NULL → gh_rate_exhausted daemon-level signal — never diagnosed.
        d, conn, _handler = _make_daemon(tmp_path)
        failure_row = (
            300,
            None,
            daemon.FAILURE_CATEGORY_GH_RATE_EXHAUSTED,
            {"threshold": 100},
            datetime(2026, 4, 18, tzinfo=UTC),
        )
        self._seed_failures_fetch(conn, [failure_row])
        candidates = d._find_diagnoser_candidates()
        assert candidates == []


# --------------------------------------------------------------------------
# _insert_pending_diagnosis — schema shape
# --------------------------------------------------------------------------


class TestInsertPendingDiagnosis:
    def test_inserts_pending_row_and_returns_id(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(777,)]
        diagnosis_id = d._insert_pending_diagnosis(
            failure_id=42,
            agent_id="agent-1",
            context={"foo": "bar"},
        )
        assert diagnosis_id == 777
        inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.diagnoses" in e[0]
        ]
        assert len(inserts) == 1
        params = inserts[0][1]
        assert params[0] == 42  # failure_id
        assert params[1] == "agent-1"  # agent_id
        # params[2] is the context JSONB serialized via json.dumps.
        assert "foo" in params[2]
        # SQL specifies status='pending' inline — verify in the SQL text.
        assert "'pending'" in inserts[0][0]


# --------------------------------------------------------------------------
# _spawn_diagnoser_subprocess — subprocess wiring
# --------------------------------------------------------------------------


class TestSpawnDiagnoserSubprocess:
    def test_runs_claude_p_with_opus_and_max_turns(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        exit_code, tail = d._spawn_diagnoser_subprocess(777)
        assert exit_code == 0
        assert tail == ""
        assert captured["cmd"][0] == "claude"
        assert captured["cmd"][1] == "-p"
        assert "/diagnose-failure 777" in captured["cmd"][2]
        assert "--model" in captured["cmd"]
        model_idx = captured["cmd"].index("--model")
        assert captured["cmd"][model_idx + 1] == daemon.DIAGNOSER_MODEL
        assert captured["kwargs"]["timeout"] == (
            daemon.DIAGNOSER_SUBPROCESS_TIMEOUT_SECONDS
        )

    def test_timeout_returns_none_exit(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=300)

        monkeypatch.setattr(subprocess, "run", fake_run)
        exit_code, _tail = d._spawn_diagnoser_subprocess(1)
        assert exit_code is None

    def test_binary_missing_returns_none_exit(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            raise FileNotFoundError("claude missing")

        monkeypatch.setattr(subprocess, "run", fake_run)
        exit_code, tail = d._spawn_diagnoser_subprocess(1)
        assert exit_code is None
        assert "claude" in tail.lower() or "not found" in tail.lower()

    def test_command_includes_dangerously_skip_permissions_flag(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """Issue #2982 — the diagnoser subagent runs inside the Fargate
        container just like the phase subagents, and must bypass the
        Bash-tool permission policy to read its context (PR/issue state,
        log files). The narrowed preflight hook still guards the 4
        safety-critical patterns."""
        d, _conn, _handler = _make_daemon(tmp_path)
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured["cmd"] = cmd
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        d._spawn_diagnoser_subprocess(42)
        assert "--dangerously-skip-permissions" in captured["cmd"], captured["cmd"]


# --------------------------------------------------------------------------
# _read_recommendation — JSONB round-trip + validation
# --------------------------------------------------------------------------


class TestReadRecommendation:
    def test_reads_dict_directly(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [({"action": "retry", "reasoning": "ok"},)]
        result = d._read_recommendation(1)
        assert result == {"action": "retry", "reasoning": "ok"}

    def test_reads_json_string(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [
            (json.dumps({"action": "escalate", "reasoning": "abc"}),)
        ]
        result = d._read_recommendation(1)
        assert result is not None
        assert result["action"] == "escalate"

    def test_missing_row_returns_none(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [None]
        assert d._read_recommendation(1) is None

    def test_null_column_returns_none(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(None,)]
        assert d._read_recommendation(1) is None

    def test_malformed_json_string_returns_none(self, tmp_path: Path) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [("not-json{",)]
        assert d._read_recommendation(1) is None


class TestValidateRecommendation:
    def test_retry_valid(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._validate_recommendation({"action": "retry"}) == "retry"

    def test_unknown_action_rejected(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._validate_recommendation({"action": "defer"}) is None

    def test_retry_with_hint_needs_hint(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._validate_recommendation({"action": "retry_with_hint"}) is None
        assert (
            d._validate_recommendation({"action": "retry_with_hint", "hint": "  "})
            is None
        )
        assert (
            d._validate_recommendation(
                {"action": "retry_with_hint", "hint": "focus on X"}
            )
            == "retry_with_hint"
        )

    def test_reissue_needs_new_scope(self, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        assert d._validate_recommendation({"action": "reissue"}) is None
        assert (
            d._validate_recommendation(
                {"action": "reissue", "new_scope": "## Goal\n..."}
            )
            == "reissue"
        )


# --------------------------------------------------------------------------
# _consume_diagnosis — deterministic action consumption
# --------------------------------------------------------------------------


class TestConsumeDiagnosis:
    def _stub_db_reads(
        self,
        conn: _FakeConnection,
        recommendation: dict[str, Any] | None,
        retry_marker_count: int = 0,
    ) -> None:
        """Seed the fetch queue in the order the consumer issues reads.

        Order for the common retry path (post-#2927 — the #2903
        _lookup_agent_kind fetch is gone):
          1. read_recommendation → (rec,)
          2. create_retry_marker COUNT → (retry_marker_count,)
          3. _backoff_seconds → ("[60,300,900]",)
        """
        conn.cursor_instance.fetch_queue = [
            (recommendation,) if recommendation is not None else None,
            (retry_marker_count,),
            ("[60,300,900]",),
        ]

    def test_retry_creates_marker_no_gh(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        # Fail if any subprocess runs — retry must be GH-silent.
        subprocess_calls = {"n": 0}

        def bad_run(*_a: Any, **_k: Any) -> Any:
            subprocess_calls["n"] += 1
            raise AssertionError("retry must not shell out to gh")

        monkeypatch.setattr(subprocess, "run", bad_run)

        self._stub_db_reads(conn, {"action": "retry", "reasoning": "transient"})

        candidate = {
            "failure_id": 42,
            "agent_id": "a1",
            "category": daemon.FAILURE_CATEGORY_SUBPROCESS_CRASH,
            "tier": 2,
            "issue_number": 99,
            "details": {},
        }
        action = d._consume_diagnosis(diagnosis_id=1, candidate=candidate)
        assert action == "retry"
        assert subprocess_calls["n"] == 0
        markers = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert markers
        assert handler.events("diagnosis_consumed")

    def test_retry_with_hint_posts_comment_then_retries(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        gh_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            gh_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        self._stub_db_reads(
            conn,
            {
                "action": "retry_with_hint",
                "reasoning": "ralph narrowed too far",
                "hint": "focus only on the OC scraper file",
            },
        )
        candidate = {
            "failure_id": 42,
            "agent_id": "a1",
            "category": daemon.FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT,
            "tier": 2,
            "issue_number": 123,
            "details": {},
        }
        action = d._consume_diagnosis(diagnosis_id=2, candidate=candidate)
        assert action == "retry_with_hint"
        # One gh issue comment should have fired.
        comment_calls = [c for c in gh_calls if c[:3] == ["gh", "issue", "comment"]]
        assert len(comment_calls) == 1
        # Retry marker inserted.
        markers = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert markers

    def test_reissue_posts_summary_edits_body_ensures_ready_retries(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        gh_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            gh_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        self._stub_db_reads(
            conn,
            {
                "action": "reissue",
                "reasoning": "AC is stale",
                "new_scope": "## Goal\n\nUpdate the scraper.\n",
            },
        )
        candidate = {
            "failure_id": 42,
            "agent_id": "a1",
            "category": daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,
            "tier": 3,
            "issue_number": 77,
            "details": {},
        }
        action = d._consume_diagnosis(diagnosis_id=3, candidate=candidate)
        assert action == "reissue"
        # comment + edit --body-file + edit --add-label agent/ready.
        comment_calls = [c for c in gh_calls if c[:3] == ["gh", "issue", "comment"]]
        body_edit_calls = [
            c
            for c in gh_calls
            if c[:3] == ["gh", "issue", "edit"] and "--body-file" in c
        ]
        label_edit_calls = [
            c
            for c in gh_calls
            if c[:3] == ["gh", "issue", "edit"] and "--add-label" in c
        ]
        assert len(comment_calls) == 1
        assert len(body_edit_calls) == 1
        # agent/ready must be among the labels added.
        assert any(
            "agent/ready" in call for call in [" ".join(c) for c in label_edit_calls]
        )
        markers = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert markers

    def test_escalate_adds_labels_posts_comment_marks_failed(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        gh_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            gh_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Escalate doesn't create a retry marker → only 1 fetch: recommendation.
        conn.cursor_instance.fetch_queue = [
            (
                {
                    "action": "escalate",
                    "reasoning": "auth broken",
                },
            )
        ]
        candidate = {
            "failure_id": 42,
            "agent_id": "a1",
            "category": daemon.FAILURE_CATEGORY_SUBPROCESS_TURN_LIMIT,
            "tier": 2,
            "issue_number": 88,
            "details": {},
        }
        action = d._consume_diagnosis(diagnosis_id=4, candidate=candidate)
        assert action == "escalate"
        # status/needs-human + priority/p1 added.
        label_edits = [
            c
            for c in gh_calls
            if c[:3] == ["gh", "issue", "edit"] and "--add-label" in c
        ]
        assert label_edits
        joined = " ".join(label_edits[0])
        assert "status/needs-human" in joined
        assert "priority/p1" in joined
        # No retry marker.
        markers = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert markers == []
        # Agent flipped to failed.
        failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed_updates

    def test_close_closes_issue_with_reason_not_planned(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, _handler = _make_daemon(tmp_path)
        gh_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            gh_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        conn.cursor_instance.fetch_queue = [
            (
                {
                    "action": "close",
                    "reasoning": "duplicate of #1234",
                },
            )
        ]
        candidate = {
            "failure_id": 42,
            "agent_id": "a1",
            "category": daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,
            "tier": 3,
            "issue_number": 55,
            "details": {},
        }
        action = d._consume_diagnosis(diagnosis_id=5, candidate=candidate)
        assert action == "close"
        close_calls = [c for c in gh_calls if c[:3] == ["gh", "issue", "close"]]
        assert len(close_calls) == 1
        joined = " ".join(close_calls[0])
        assert "not planned" in joined
        # status/invalid label added.
        label_edits = [
            c
            for c in gh_calls
            if c[:3] == ["gh", "issue", "edit"] and "--add-label" in c
        ]
        assert any("status/invalid" in " ".join(c) for c in label_edits)
        # No retry marker.
        markers = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert markers == []

    def test_malformed_json_falls_back_to_escalate(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        gh_calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            gh_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Recommendation col contains a non-JSON string → read returns None.
        conn.cursor_instance.fetch_queue = [("corrupt{",)]
        candidate = {
            "failure_id": 42,
            "agent_id": "a1",
            "category": daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,
            "tier": 3,
            "issue_number": 77,
            "details": {},
        }
        action = d._consume_diagnosis(diagnosis_id=6, candidate=candidate)
        assert action == "escalate_fallback"
        assert handler.events("diagnoser_fallback")
        # Diagnosis row marked failed.
        mark_failed = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.diagnoses" in e[0] and "'failed'" in e[0]
        ]
        assert mark_failed
        # Mechanical escalation applied → agent marked failed + labels added.
        label_edits = [
            c
            for c in gh_calls
            if c[:3] == ["gh", "issue", "edit"] and "--add-label" in c
        ]
        assert any("status/needs-human" in " ".join(c) for c in label_edits)

    def test_unknown_action_falls_back_to_escalate(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def fake_run(*_a: Any, **_k: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)

        conn.cursor_instance.fetch_queue = [
            ({"action": "defer", "reasoning": "not in the 5"},)
        ]
        candidate = {
            "failure_id": 42,
            "agent_id": "a1",
            "category": daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,
            "tier": 3,
            "issue_number": 77,
            "details": {},
        }
        action = d._consume_diagnosis(diagnosis_id=7, candidate=candidate)
        assert action == "escalate_fallback"
        assert handler.events("diagnosis_action_unknown")


# --------------------------------------------------------------------------
# _run_diagnoser_pass — full-tick integration
# --------------------------------------------------------------------------


class TestRunDiagnoserPass:
    def test_disabled_returns_zero(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)

        monkeypatch.setattr(d, "_diagnoser_enabled", lambda: False)
        monkeypatch.setattr(
            d,
            "_find_diagnoser_candidates",
            MagicMock(
                side_effect=AssertionError("must not scan candidates when disabled")
            ),
        )
        assert d._run_diagnoser_pass() == 0

    def test_no_candidates_returns_zero(self, monkeypatch: Any, tmp_path: Path) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_diagnoser_enabled", lambda: True)
        monkeypatch.setattr(d, "_find_diagnoser_candidates", lambda: [])
        assert d._run_diagnoser_pass() == 0

    def test_subprocess_timeout_triggers_fallback(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)

        monkeypatch.setattr(d, "_diagnoser_enabled", lambda: True)
        monkeypatch.setattr(
            d,
            "_find_diagnoser_candidates",
            lambda: [
                {
                    "failure_id": 42,
                    "agent_id": "a1",
                    "category": daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,
                    "tier": 3,
                    "issue_number": 77,
                    "details": {},
                }
            ],
        )
        monkeypatch.setattr(d, "_build_diagnoser_context", lambda _c: {})
        monkeypatch.setattr(d, "_insert_pending_diagnosis", lambda **_k: 999)
        monkeypatch.setattr(d, "_spawn_diagnoser_subprocess", lambda _id: (None, ""))
        fallback_called = {"n": 0}

        def fake_fallback(candidate: dict[str, Any]) -> None:
            fallback_called["n"] += 1

        monkeypatch.setattr(d, "_apply_mechanical_escalation", fake_fallback)
        marked_failed: dict[str, Any] = {}

        def fake_mark_failed(diagnosis_id: int, reason: str) -> None:
            marked_failed["id"] = diagnosis_id
            marked_failed["reason"] = reason

        monkeypatch.setattr(d, "_mark_diagnosis_failed", fake_mark_failed)

        ran = d._run_diagnoser_pass()
        assert ran == 1
        assert fallback_called["n"] == 1
        assert marked_failed["id"] == 999
        assert "timeout" in marked_failed["reason"].lower()

    def test_subprocess_nonzero_exit_triggers_fallback(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, handler = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_diagnoser_enabled", lambda: True)
        monkeypatch.setattr(
            d,
            "_find_diagnoser_candidates",
            lambda: [
                {
                    "failure_id": 42,
                    "agent_id": "a1",
                    "category": daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,
                    "tier": 3,
                    "issue_number": 77,
                    "details": {},
                }
            ],
        )
        monkeypatch.setattr(d, "_build_diagnoser_context", lambda _c: {})
        monkeypatch.setattr(d, "_insert_pending_diagnosis", lambda **_k: 888)
        monkeypatch.setattr(d, "_spawn_diagnoser_subprocess", lambda _id: (1, "boom"))
        fallback_called = {"n": 0}
        monkeypatch.setattr(
            d,
            "_apply_mechanical_escalation",
            lambda _c: fallback_called.__setitem__("n", fallback_called["n"] + 1),
        )
        marked: dict[str, Any] = {}
        monkeypatch.setattr(
            d,
            "_mark_diagnosis_failed",
            lambda did, reason: marked.update({"id": did, "reason": reason}),
        )
        ran = d._run_diagnoser_pass()
        assert ran == 1
        assert fallback_called["n"] == 1
        assert marked["id"] == 888
        assert handler.events("diagnoser_nonzero_exit")

    def test_insert_failure_still_runs_fallback(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, _conn, _handler = _make_daemon(tmp_path)
        monkeypatch.setattr(d, "_diagnoser_enabled", lambda: True)
        monkeypatch.setattr(
            d,
            "_find_diagnoser_candidates",
            lambda: [
                {
                    "failure_id": 42,
                    "agent_id": "a1",
                    "category": daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES,
                    "tier": 3,
                    "issue_number": 77,
                    "details": {},
                }
            ],
        )
        monkeypatch.setattr(d, "_build_diagnoser_context", lambda _c: {})
        monkeypatch.setattr(d, "_insert_pending_diagnosis", lambda **_k: None)

        def bad_spawn(_id: int) -> tuple[int | None, str]:
            raise AssertionError("must not spawn when insert failed")

        monkeypatch.setattr(d, "_spawn_diagnoser_subprocess", bad_spawn)
        fallback_called = {"n": 0}
        monkeypatch.setattr(
            d,
            "_apply_mechanical_escalation",
            lambda _c: fallback_called.__setitem__("n", fallback_called["n"] + 1),
        )
        ran = d._run_diagnoser_pass()
        # Subprocess did not run; count is 0 but fallback still applied.
        assert ran == 0
        assert fallback_called["n"] == 1


# --------------------------------------------------------------------------
# Supervisor tick integration — 3D wires into tick
# --------------------------------------------------------------------------


class TestSupervisorTickDiagnoserIntegration:
    def test_tick_calls_circuit_breaker_and_diagnoser_pass(
        self, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        called: dict[str, int] = {"breaker": 0, "pass": 0}
        d._check_stuck_agents = lambda: 0  # type: ignore[method-assign]
        d._check_gh_rate_limit = lambda: None  # type: ignore[method-assign]
        d._advance_running_agents = lambda: 0  # type: ignore[method-assign]
        d._process_retry_markers = lambda: 0  # type: ignore[method-assign]
        d._emit_heartbeat_metric = lambda: True  # type: ignore[method-assign]
        d._check_diagnoser_circuit_breaker = lambda: (  # type: ignore[method-assign]
            called.__setitem__("breaker", called["breaker"] + 1) or False
        )
        d._run_diagnoser_pass = lambda: (  # type: ignore[method-assign]
            called.__setitem__("pass", called["pass"] + 1) or 0
        )
        summary = d.supervisor_tick()
        assert called["breaker"] == 1
        assert called["pass"] == 1
        assert "diagnoses_ran" in summary

    def test_tick_survives_breaker_exception(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        d._check_stuck_agents = lambda: 0  # type: ignore[method-assign]
        d._check_gh_rate_limit = lambda: None  # type: ignore[method-assign]
        d._advance_running_agents = lambda: 0  # type: ignore[method-assign]
        d._process_retry_markers = lambda: 0  # type: ignore[method-assign]
        d._emit_heartbeat_metric = lambda: True  # type: ignore[method-assign]

        def boom() -> bool:
            raise RuntimeError("breaker exploded")

        d._check_diagnoser_circuit_breaker = boom  # type: ignore[method-assign]
        d._run_diagnoser_pass = lambda: 0  # type: ignore[method-assign]
        summary = d.supervisor_tick()
        assert handler.events("diagnoser_circuit_breaker_check_failed")
        assert summary["heartbeat_metric_emitted"] == 1

    def test_tick_survives_pass_exception(self, tmp_path: Path) -> None:
        d, conn, handler = _make_daemon(tmp_path)
        conn.cursor_instance.fetch_queue = [(0,)]

        d._check_stuck_agents = lambda: 0  # type: ignore[method-assign]
        d._check_gh_rate_limit = lambda: None  # type: ignore[method-assign]
        d._advance_running_agents = lambda: 0  # type: ignore[method-assign]
        d._process_retry_markers = lambda: 0  # type: ignore[method-assign]
        d._emit_heartbeat_metric = lambda: True  # type: ignore[method-assign]
        d._check_diagnoser_circuit_breaker = lambda: False  # type: ignore[method-assign]

        def boom() -> int:
            raise RuntimeError("pass exploded")

        d._run_diagnoser_pass = boom  # type: ignore[method-assign]
        summary = d.supervisor_tick()
        assert handler.events("diagnoser_pass_failed")
        assert summary["heartbeat_metric_emitted"] == 1
        assert summary["diagnoses_ran"] == 0


# --------------------------------------------------------------------------
# 3B fix-CI exhaustion path now writes ci_red_after_retries failure row
# --------------------------------------------------------------------------


class TestFixCiExhaustionWritesFailureRow:
    """3B's fix-CI max-retries branch writes a tier-3 failure row before
    flipping the agent to 'failed', so 3D's tier-3 detector can pick it up.
    """

    def test_max_retries_writes_ci_red_after_retries_failure(
        self, monkeypatch: Any, tmp_path: Path
    ) -> None:
        d, conn, handler = _make_daemon(tmp_path)

        def fake_spawn(*_a: Any, **_k: Any) -> tuple[int, float]:
            raise AssertionError("fix-ci must not spawn past max retries")

        monkeypatch.setattr(d, "_spawn_phase_subprocess", fake_spawn)

        agent = {
            "agent_id": "a1",
            "issue_number": 42,
            "phase": "awaiting_ci",
            "pr_number": 101,
            "worktree_path": str(tmp_path),
            "retries_used": daemon.FIX_CI_MAX_RETRIES,
        }
        pr_status = {"statusCheckRollup": []}
        d._run_fix_ci(agent, pr_status)

        # Max-retries event still logged.
        assert handler.events("fix_ci_max_retries_exceeded")
        # Failure row written with category='ci_red_after_retries'.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        params = failure_inserts[0][1]
        assert params[0] == "a1"  # agent_id
        assert params[1] == daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES
        # Agent flipped to failed after the failure row.
        failed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "failed" in e[1]
        ]
        assert failed_updates


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------


class TestConstants:
    def test_ci_red_after_retries_category_string(self) -> None:
        assert daemon.FAILURE_CATEGORY_CI_RED_AFTER_RETRIES == "ci_red_after_retries"

    def test_diagnoser_actions_exhaustive(self) -> None:
        # Spec §8 lists exactly 5 actions. Lock it in so a sixth cannot
        # be added without also updating the daemon's deterministic
        # consumer (`_consume_diagnosis` has explicit branches).
        assert daemon.DIAGNOSER_ACTIONS == frozenset(
            {"retry", "retry_with_hint", "reissue", "escalate", "close"}
        )

    def test_tier_sets_are_disjoint(self) -> None:
        # A category must land in exactly one tier bucket.
        assert (
            daemon.TIER_2_RECURRENCE_CATEGORIES
            & daemon.TIER_2_FIRST_OCCURRENCE_CATEGORIES
            == frozenset()
        )
        assert (
            daemon.TIER_2_RECURRENCE_CATEGORIES & daemon.TIER_3_CATEGORIES
            == frozenset()
        )
        assert (
            daemon.TIER_2_FIRST_OCCURRENCE_CATEGORIES & daemon.TIER_3_CATEGORIES
            == frozenset()
        )

    def test_diagnoser_timeout_is_five_minutes(self) -> None:
        # Spec §8 "5-min hard wall-clock timeout on the diagnoser subprocess".
        assert daemon.DIAGNOSER_SUBPROCESS_TIMEOUT_SECONDS == 5 * 60
