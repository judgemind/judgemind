"""Regression tests for the issue-#4307 worktree-path leak guards.

Pre-#4307, ``DispatcherDaemon._compute_worktree_path`` and
``DispatcherDaemon._create_worktree`` would happily interpolate any
str-typed value into the on-disk path
``<repo_root>/.claude/worktrees/agent-<short_id>``. The
``str(MagicMock())`` shape (``"<MagicMock id='...'>"``) and its
8-char-truncated form (``"<MagicMo"``) both produced real directories
that tripped the next CI step's repo-walking hygiene checks (#4300).

These tests lock in:

1. ``_compute_worktree_path`` raises ``TypeError`` on a non-``str``
   ``short_id`` (e.g. a raw ``MagicMock``).
2. ``_compute_worktree_path`` raises ``ValueError`` on a ``str`` that
   contains characters outside the safe ``[A-Za-z0-9_-]`` slug class —
   the truncated ``str(MagicMock())`` form ``"<MagicMo"`` is the
   canonical example.
3. ``_create_worktree`` raises ``TypeError`` on a non-``str``
   ``agent_id`` (defense-in-depth at the public entry point).
4. ``_compute_worktree_path`` accepts the legitimate inputs (real
   uuid-derived hex short_ids, test-fixture deterministic strings).
5. The ``conftest.py`` session-scoped invariant
   (``_assert_no_leaked_test_worktrees``) detects synthetic
   leaked worktree directories — opt-in regression coverage via the
   ``DISPATCHER_TESTS_ALLOW_WORKTREE_LEAK`` env var.

Together these guards close every path the original leak could take:
the type-narrowing TypeError catches direct Mock injection, the
slug-shape ValueError catches ``str(MagicMock())``-derived strings,
and the session-end invariant catches anything that slips past both
guards via a code path we haven't covered yet.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from dispatcher import daemon


def _make_daemon(tmp_path: Path) -> daemon.DispatcherDaemon:
    """Build a daemon with the smallest viable shape for path-construction tests."""
    d = daemon.DispatcherDaemon.__new__(daemon.DispatcherDaemon)
    d._thread_state = threading.local()  # type: ignore[attr-defined]
    d._main_conn = MagicMock()  # type: ignore[attr-defined]
    d._cfg = MagicMock(baseline_repo_root=None)  # type: ignore[attr-defined]
    d._log = logging.getLogger(  # type: ignore[attr-defined]
        f"test.daemon_worktree_path_guard.{id(tmp_path)}"
    )
    d._run_id = "test-run-id"  # type: ignore[attr-defined]
    return d


# --------------------------------------------------------------------------
# _is_valid_short_id — the predicate the guards consult.
# --------------------------------------------------------------------------


class TestIsValidShortId:
    def test_real_hex_short_id_accepted(self) -> None:
        assert daemon._is_valid_short_id("aabbccdd") is True

    def test_full_uuid_form_accepted(self) -> None:
        # The path uses the truncated short_id, but the helper is also
        # invoked on raw uuids in some peer call sites.
        assert daemon._is_valid_short_id("aabbccddeeff00112233445566778899") is True

    def test_test_fixture_string_accepted(self) -> None:
        # The pre-#4307 fixture pattern (e.g. "agentuui" from
        # "agent-uuid-...") is filename-safe and stays accepted.
        assert daemon._is_valid_short_id("agentuui") is True

    def test_test_fixture_dashed_string_accepted(self) -> None:
        assert daemon._is_valid_short_id("agent-test-fixture") is True

    def test_underscored_string_accepted(self) -> None:
        assert daemon._is_valid_short_id("agent_test") is True

    def test_truncated_magicmock_repr_rejected(self) -> None:
        # The canonical leak shape (``str(MagicMock())[:8]`` truncated
        # to ``"<MagicMo"``).
        assert daemon._is_valid_short_id("<MagicMo") is False

    def test_full_magicmock_repr_rejected(self) -> None:
        assert daemon._is_valid_short_id("<MagicMock id='4503599627368'>") is False

    def test_path_traversal_attempt_rejected(self) -> None:
        # A defense-in-depth bonus: ``..`` and ``/`` would let a malicious
        # short_id escape ``.claude/worktrees/``. The slug regex rejects
        # both.
        assert daemon._is_valid_short_id("../etc/passwd") is False
        assert daemon._is_valid_short_id("foo/bar") is False

    def test_empty_string_rejected(self) -> None:
        assert daemon._is_valid_short_id("") is False

    def test_whitespace_rejected(self) -> None:
        assert daemon._is_valid_short_id("agent test") is False


# --------------------------------------------------------------------------
# _compute_worktree_path — TypeError + ValueError guards.
# --------------------------------------------------------------------------


class TestComputeWorktreePathGuards:
    def test_non_string_short_id_raises_type_error(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        # Pre-#4307: ``f"agent-{MagicMock()}"`` would interpolate the
        # ``repr()`` and the daemon would create a real dir at
        # ``agent-<MagicMock id='...'>``. The TypeError stops it dead.
        with pytest.raises(TypeError, match="short_id must be a str"):
            d._compute_worktree_path(MagicMock())  # type: ignore[arg-type]

    def test_string_with_unsafe_chars_raises_value_error(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        # The truncated ``str(MagicMock())`` form survives the isinstance
        # check (it IS a str); the slug-shape regex catches it instead.
        with pytest.raises(ValueError, match="hex-only shape"):
            d._compute_worktree_path("<MagicMo")

    def test_full_mock_repr_string_raises_value_error(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        with pytest.raises(ValueError, match="hex-only shape"):
            d._compute_worktree_path("<MagicMock id='4503599627368'>")

    def test_real_short_id_returns_valid_path(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        # Pin _repo_root so the assertion isn't sensitive to test cwd.
        d._repo_root = lambda: tmp_path  # type: ignore[method-assign]
        wt = d._compute_worktree_path("aabbccdd")
        assert wt == tmp_path / ".claude" / "worktrees" / "agent-aabbccdd"

    def test_path_traversal_attempt_rejected(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        with pytest.raises(ValueError):
            d._compute_worktree_path("../etc/passwd")


# --------------------------------------------------------------------------
# _create_worktree — TypeError on non-string agent_id.
# --------------------------------------------------------------------------


class TestCreateWorktreeGuards:
    def test_non_string_agent_id_raises_type_error(self, tmp_path: Path) -> None:
        d = _make_daemon(tmp_path)
        with pytest.raises(TypeError, match="agent_id must be a str"):
            d._create_worktree(MagicMock())  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Conftest session-scoped invariant — _assert_no_leaked_test_worktrees.
#
# We exercise this as a unit test against the helper functions; the
# session-scoped fixture itself is exercised implicitly by every other
# test in the suite running cleanly.
# --------------------------------------------------------------------------


def _import_conftest_helpers() -> Any:
    """Load the conftest module so we can call its private helpers
    directly. ``conftest.py`` doesn't have a public name, so we go
    through ``pytest``'s already-loaded module dict."""
    import sys

    # The conftest is loaded by pytest as ``conftest`` in the test
    # collection root; it also lives in sys.modules under the path-derived
    # name. Find whichever was registered.
    for name, mod in sys.modules.items():
        if name.endswith("dispatcher.tests.conftest") or name == "conftest":
            return mod
    raise RuntimeError("conftest module not loaded — should be impossible")


class TestConftestLeakInvariant:
    def test_real_worktree_name_pattern_accepts_hex(self) -> None:
        conftest = _import_conftest_helpers()
        assert conftest._REAL_WORKTREE_NAME_RE.match("agent-aabbccdd")

    def test_real_worktree_name_pattern_accepts_full_uuid(self) -> None:
        conftest = _import_conftest_helpers()
        assert conftest._REAL_WORKTREE_NAME_RE.match(
            "agent-aabbccdd-eeff-0011-2233-445566778899"
        )

    def test_real_worktree_name_pattern_rejects_magicmock_truncation(self) -> None:
        conftest = _import_conftest_helpers()
        assert conftest._REAL_WORKTREE_NAME_RE.match("agent-<MagicMo") is None

    def test_real_worktree_name_pattern_rejects_test_fixture_name(self) -> None:
        # A test fixture name ("agent-test-fixture") is also synthetic and
        # would be flagged by the session-end invariant.
        conftest = _import_conftest_helpers()
        assert conftest._REAL_WORKTREE_NAME_RE.match("agent-test-fixture") is None
