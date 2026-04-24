"""Allowlist test for ``_spawn_phase_subprocess`` argv flags.

Every flag token that ``_spawn_phase_subprocess`` passes to the ``claude``
CLI must be in :data:`ALLOWED_CLAUDE_FLAGS`. The allowlist exists to prevent
a recurrence of the #2821 incident, where ``--cwd <worktree>`` was
accidentally added to the subprocess command. The ``claude`` CLI does not
accept ``--cwd``, so every phase subprocess exited within 400 ms with
``error: unknown option '--cwd'``, silently failing all ongoing tasks.

Any addition to ``_spawn_phase_subprocess``'s flag list requires a
corresponding update to :data:`ALLOWED_CLAUDE_FLAGS` here — that code-review
gate is the point. The three flags named in AC2 of issue #2828 (`-p`,
``--max-turns``, ``--model``) are the minimum set; the remaining two flags
(``--output-format``, ``--dangerously-skip-permissions``) are required for
the tests to pass against the current implementation.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher.tests._popen_fake import make_popen_factory  # noqa: E402
from dispatcher.tests.test_daemon_phase3a import _make_daemon  # noqa: E402

# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

ALLOWED_CLAUDE_FLAGS: frozenset[str] = frozenset(
    {
        "-p",
        "--max-turns",
        "--model",
        "--output-format",
        "--dangerously-skip-permissions",
    }
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase",
    ["plan", "ralph", "summary", "fix-ci", "verify", "retro"],
)
def test_spawn_phase_subprocess_uses_only_allowed_flags(
    monkeypatch: Any, tmp_path: Path, phase: str
) -> None:
    """Every flag token in the phase-subprocess argv must be in ALLOWED_CLAUDE_FLAGS.

    Parametrized over all six phases so a rogue flag introduced for a single
    phase is caught immediately rather than surfacing as a runtime failure.
    """
    d, _conn, _handler = _make_daemon(tmp_path)
    captured: dict[str, Any] = {}

    def on_start(cmd: list[str], kwargs: dict[str, Any]) -> None:
        captured["cmd"] = cmd

    monkeypatch.setattr(subprocess, "Popen", make_popen_factory(on_start=on_start))
    d._spawn_phase_subprocess(phase, tmp_path, f"agent-{phase}")

    assert "cmd" in captured, f"on_start was never called for phase={phase}"
    for token in captured["cmd"]:
        if token.startswith("-"):
            assert token in ALLOWED_CLAUDE_FLAGS, (
                f"{token!r} is not in ALLOWED_CLAUDE_FLAGS — "
                f"see #2821 (the --cwd incident). "
                f"Full argv: {captured['cmd']}"
            )


def test_cwd_flag_would_fail_allowlist() -> None:
    """Sanity-check: inserting ``--cwd`` into a synthetic argv fails the allowlist.

    This proves the allowlist check bites. We use a toy list so we don't need
    to monkeypatch daemon internals — the guard logic is identical to what
    ``test_spawn_phase_subprocess_uses_only_allowed_flags`` applies.
    """
    synthetic_cmd = [
        "claude",
        "-p",
        "/task-v2-plan agent-test",
        "--max-turns",
        "500",
        "--model",
        "opus",
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--cwd",  # the bad flag from #2821
        "/some/worktree/path",
    ]
    rogue_flags = [
        t for t in synthetic_cmd if t.startswith("-") and t not in ALLOWED_CLAUDE_FLAGS
    ]
    assert rogue_flags, (
        "Expected '--cwd' to be absent from ALLOWED_CLAUDE_FLAGS, "
        f"but no rogue flags were found. ALLOWED_CLAUDE_FLAGS={ALLOWED_CLAUDE_FLAGS}"
    )
    assert "--cwd" in rogue_flags


def test_allowlist_contains_required_flags() -> None:
    """ALLOWED_CLAUDE_FLAGS must contain the three flags named in issue #2828 AC2."""
    required = {"-p", "--max-turns", "--model"}
    assert required <= ALLOWED_CLAUDE_FLAGS, (
        f"Missing required flags: {required - ALLOWED_CLAUDE_FLAGS}"
    )
