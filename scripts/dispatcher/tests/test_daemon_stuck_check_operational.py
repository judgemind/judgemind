"""Regression tests for the operational-phase stuck-timeout threshold (#3524).

Verifies that :meth:`~dispatcher.daemon.DispatcherDaemon._check_stuck_agents`
correctly uses the 3600s (60 min) threshold for the ``operational`` phase
added in issue #3524 to :data:`~dispatcher.daemon.STUCK_TIMEOUT_SECONDS_BY_PHASE`.

Two test cases:

* **Positive** — an operational agent at elapsed 3700s (> 3600s threshold) is
  flagged as stuck: a ``dispatcher.failures`` row is inserted with
  ``category=stuck_timeout`` and ``phase='operational'``, the agent is flipped
  to ``status='crashed'``, and a retry marker is enqueued.
* **Negative** — an operational agent at elapsed 3500s (< 3600s threshold) is
  NOT flagged; ``_check_stuck_agents`` returns 0.

Fixture style mirrors ``test_daemon_phase3c.py::TestCheckStuckAgents`` —
uses the same ``_FakeCursor`` / ``_FakeConnection`` / ``_CapturingLogHandler``
pattern and documents the exact ``fetch_queue`` ordering inline.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


from dispatcher import daemon  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — mirrors test_daemon_phase3c.py
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
    logger = logging.getLogger(f"dispatcher.test.stuck_operational.{id(tmp_path)}")
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
# Tests
# --------------------------------------------------------------------------


class TestOperationalPhaseStuckTimeout:
    def test_operational_agent_over_threshold_flagged_as_stuck(
        self, tmp_path: Path
    ) -> None:
        """An operational agent at elapsed 3700s (> 3600s threshold) is flagged.

        Verifies: failure row inserted with category=stuck_timeout and
        phase='operational', agent flipped to 'crashed', retry marker enqueued.
        (#3524 — add operational threshold to STUCK_TIMEOUT_SECONDS_BY_PHASE.)
        """
        d, conn, handler = _make_daemon(tmp_path)

        # Seeded row: one stale operational agent. Elapsed 3700s > 3600s
        # (operational threshold added in #3524). The SELECT returns
        # (agent_id, issue_number, phase, elapsed_seconds) and the
        # Python-side comparison applies the per-phase threshold.
        conn.cursor_instance.fetchall_queue = [
            [("agent-op-1", 3524, "operational", 3700.0)],
        ]
        # Order inside _check_stuck_agents + _create_retry_marker:
        # (1) stuck_timeout_s_by_phase override read (returns None → fall
        #     through to module defaults — where "operational": 3600 now lives),
        # (2) RETURNING failure_id from _write_failure INSERT,
        # (3) _has_prior_stuck_timeout_in_window SELECT (returns None →
        #     first occurrence, no repeated event),
        # (4) COUNT prior markers in _create_retry_marker,
        # (5) read backoff_seconds config.
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
            (100,),  # failure_id from _write_failure RETURNING
            None,  # _has_prior_stuck_timeout_in_window — no prior
            (0,),  # prior retry marker count
            ("[60,300,900]",),  # backoff schedule
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 1

        # Failure row inserted with correct category and agent_id.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert len(failure_inserts) == 1
        params = failure_inserts[0][1]
        assert params[0] == "agent-op-1"  # agent_id
        assert params[1] == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        assert params[2] == "supervisor"

        # Agent flipped to 'crashed'.
        crashed_updates = [
            e
            for e in conn.cursor_instance.executed
            if "UPDATE dispatcher.agents" in e[0]
            and e[1] is not None
            and "crashed" in e[1]
        ]
        assert crashed_updates

        # daemon.failure_detected event emitted with stuck_timeout category.
        detected = handler.events("failure_detected")
        assert detected
        assert detected[0].category == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT  # type: ignore[attr-defined]

        # Retry marker inserted for the operational agent.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert len(marker_inserts) == 1
        marker_params = marker_inserts[0][1]
        assert marker_params[0] == "agent-op-1"
        assert marker_params[1] == daemon.FAILURE_CATEGORY_STUCK_TIMEOUT
        assert marker_params[2] == 1  # first attempt

    def test_operational_agent_under_threshold_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """An operational agent at elapsed 3500s (< 3600s threshold) is NOT flagged.

        The Python-side per-phase threshold check filters out the agent
        before any failure row or retry marker is written. (#3524)
        """
        d, conn, handler = _make_daemon(tmp_path)

        # Seeded row: operational agent at 3500s — below the 3600s threshold.
        conn.cursor_instance.fetchall_queue = [
            [("agent-op-2", 3524, "operational", 3500.0)],
        ]
        # Only the per-sweep override read fires (fetchone for the config row).
        # The agent is filtered by elapsed < threshold, so no failure/marker
        # DB calls are made.
        conn.cursor_instance.fetch_queue = [
            None,  # stuck_timeout_s_by_phase override — unset
        ]

        flagged = d._check_stuck_agents()
        assert flagged == 0

        # No failure row written.
        failure_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.failures" in e[0]
        ]
        assert failure_inserts == []

        # No retry marker written.
        marker_inserts = [
            e
            for e in conn.cursor_instance.executed
            if "INSERT INTO dispatcher.retry_markers" in e[0]
        ]
        assert marker_inserts == []

        # No failure_detected event logged.
        detected = handler.events("failure_detected")
        assert detected == []
