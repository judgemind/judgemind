"""Shared pytest configuration for the dispatcher test suite.

Installs a canonical ``psycopg`` stub at collection time so every
``test_daemon_*.py`` module imports against the same object regardless
of collection order.  Individual test files no longer need their own
preamble guards.

Also installs a session-scoped invariant (issue #4307) that fails the
test session loudly if any test leaks a synthetic agent worktree
under ``<repo_root>/.claude/worktrees/agent-*`` whose name does not
match the real-worktree pattern. The pre-#4307 failure mode was a
``MagicMock`` ``agent_id`` getting interpolated into a path string,
landing directories at ``agent-<MagicMock id='...'>/`` that then
tripped repo-walking hygiene checks at the next CI step.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class _UniqueViolation(Exception):
    """Test sentinel — real Exception subclass so daemon's except clauses work."""


_psycopg_stub = MagicMock()
_psycopg_errors = MagicMock()
_psycopg_errors.UniqueViolation = _UniqueViolation
_psycopg_stub.errors = _psycopg_errors
sys.modules["psycopg"] = _psycopg_stub  # always overwrite (collection runs first)


# Real per-agent worktrees follow ``agent-<hex-or-uuidlike>`` — short hex
# id (e.g. ``agent-aabbccdd``) or full uuid-like (e.g.
# ``agent-aabbccdd-eeff-0011-2233-445566778899``). Synthetic /
# Mock-tainted leak names like ``agent-<MagicMock id='4503599627368'>``
# or ``agent-test-fixture`` do NOT match; the session-scoped invariant
# (see ``_assert_no_leaked_test_worktrees``) flags those as a loud test
# failure rather than a silent leak.
_REAL_WORKTREE_NAME_RE = re.compile(r"^agent-[0-9a-f]{8,}(?:-[0-9a-f]+)*$")


def _repo_root() -> Path:
    """Return the absolute path of the repo root the tests run against.

    ``conftest.py`` lives at ``scripts/dispatcher/tests/conftest.py``;
    the repo root is three parents up.
    """
    return Path(__file__).resolve().parents[3]


def _list_leaked_synthetic_worktrees() -> list[Path]:
    """Return paths under ``<repo_root>/.claude/worktrees/`` whose names do
    NOT match the real-worktree pattern. Anything in this list is a
    test-fixture leak — see issue #4307.
    """
    worktrees_dir = _repo_root() / ".claude" / "worktrees"
    if not worktrees_dir.is_dir():
        return []
    leaked: list[Path] = []
    for entry in worktrees_dir.iterdir():
        if not entry.name.startswith("agent-"):
            continue
        if _REAL_WORKTREE_NAME_RE.match(entry.name):
            continue
        leaked.append(entry)
    return leaked


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so ``pytest -m integration`` works without warnings."""
    config.addinivalue_line(
        "markers",
        "integration: tests exercising real subprocess/filesystem/git — excluded from default CI run",
    )


@pytest.fixture(autouse=True, scope="session")
def _assert_no_leaked_test_worktrees() -> object:
    """Session-scoped invariant — fail loudly on synthetic worktree leaks (#4307).

    A real ``agent-<short_id>`` worktree always has a hex-only id
    (``agent-aabbccdd`` or full uuid). Anything else under
    ``<repo_root>/.claude/worktrees/`` after the test session ends is a
    test-fixture leak — typically a test that exercised
    :meth:`DispatcherDaemon._create_worktree` or
    :meth:`DispatcherDaemon._compute_worktree_path` against a real
    ``_repo_root`` rather than a ``tmp_path``.

    This fixture also captures the pre-session set so we only flag
    *new* leaks introduced during this session — pre-existing leftover
    directories from earlier runs aren't this session's bug to surface.

    To intentionally test the leak path (e.g. a regression test for
    this fixture itself), set ``DISPATCHER_TESTS_ALLOW_WORKTREE_LEAK=1``
    in the test process env. The fixture still runs the cleanup but
    skips the assertion.
    """
    pre_existing = {p.name for p in _list_leaked_synthetic_worktrees()}
    yield
    post = _list_leaked_synthetic_worktrees()
    new_leaks = [p for p in post if p.name not in pre_existing]
    if not new_leaks:
        return
    # Always clean up so the next session starts clean — do this
    # BEFORE asserting so a failed assertion doesn't leave the repo
    # poisoned for the next pytest invocation.
    for path in new_leaks:
        try:
            shutil.rmtree(path)
        except OSError:  # pragma: no cover — best-effort
            pass
    if os.environ.get("DISPATCHER_TESTS_ALLOW_WORKTREE_LEAK") == "1":
        return
    formatted = "\n".join(f"  - {p}" for p in new_leaks)
    raise AssertionError(
        "Dispatcher test suite leaked synthetic agent worktrees under "
        "<repo_root>/.claude/worktrees/ during the session. These names "
        "do not match the real-worktree pattern "
        f"(``{_REAL_WORKTREE_NAME_RE.pattern}``) and indicate a test "
        "interpolated a non-string (typically a ``MagicMock``) into a "
        "worktree path or failed to monkeypatch ``_repo_root``. See "
        "issue #4307 for the historical failure mode.\n\n"
        f"Leaked paths (cleaned up):\n{formatted}"
    )


@pytest.fixture
def psycopg_stub() -> MagicMock:
    """Return the conftest-installed psycopg stub for tests that need to wire .connect, etc."""
    return sys.modules["psycopg"]  # type: ignore[return-value]
