"""Unit tests for ``dispatcher_v3.runners``.

The tests pin the exact argv shapes for each registered runner. They
double as living spec for the ``--worktree=NAME`` foot-gun: if a future
PR "simplifies" the claude form to ``-w "<prompt>"``, the
``test_claude_uses_equals_worktree_form`` assertion fails loudly.
"""

from __future__ import annotations

import pytest

from dispatcher_v3.runners import RUNNERS, build_argv


def test_runners_dict_has_expected_keys() -> None:
    """The four registered runners are claude, gemini, opencode, cursor."""
    assert sorted(RUNNERS.keys()) == ["claude", "cursor", "gemini", "opencode"]


def test_build_argv_claude_returns_exact_argv() -> None:
    """The claude argv is pinned, single-token --worktree=NAME and trailing prompt."""
    argv = build_argv("claude", "3835", "abc-123")
    assert argv == [
        "claude",
        "-p",
        "--worktree=agent-abc-123",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "/task #3835",
    ]


def test_claude_uses_equals_worktree_form() -> None:
    """``--worktree=agent-...`` is a single argv element (the equals form).

    Regression guard for PR #3868 / issue #3835: ``-w "<prompt>"`` causes
    the prompt to be consumed as the worktree name because ``-w`` takes
    an optional value. The fix is the ``=`` form. If anyone splits this
    into two argv elements (``--worktree`` then ``agent-...``) or
    switches to ``-w``, this test fails.
    """
    argv = build_argv("claude", "1", "fixture-id")
    assert "--worktree=agent-fixture-id" in argv
    # --worktree must NOT appear as a bare token followed by a separate value.
    assert "--worktree" not in argv
    assert "-w" not in argv


def test_build_argv_gemini_returns_expected_argv() -> None:
    """The gemini argv shape is the per-spec ``gemini -p '<prompt>' --output-format stream-json``."""
    argv = build_argv("gemini", "42", "agent-xyz")
    assert argv == [
        "gemini",
        "-p",
        "Run /task for issue #42",
        "--output-format",
        "stream-json",
    ]


def test_build_argv_opencode_returns_expected_argv() -> None:
    """The opencode argv shape is wrapped in ``timeout 6h`` per spec §12."""
    argv = build_argv("opencode", "100", "agent-foo")
    assert argv == [
        "timeout",
        "6h",
        "opencode",
        "run",
        "Run /task for issue #100",
    ]


def test_build_argv_cursor_returns_expected_argv_with_model_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cursor argv pulls the model from the ``MODEL`` env var at call time."""
    monkeypatch.setenv("MODEL", "gpt-5")
    argv = build_argv("cursor", "7", "agent-bar")
    assert argv == [
        "timeout",
        "6h",
        "cursor-agent",
        "-p",
        "Run /task for issue #7",
        "-m",
        "gpt-5",
    ]


def test_build_argv_cursor_falls_back_to_empty_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``MODEL`` is unset, cursor's ``-m`` arg is the empty string.

    Lambdas in ``RUNNERS`` read ``os.environ`` lazily at call time, so an
    unset env var resolves to ``""`` rather than ``None``. The cursor
    CLI handles the empty default; the test pins this behavior so a
    future refactor doesn't accidentally pass ``None``.
    """
    monkeypatch.delenv("MODEL", raising=False)
    argv = build_argv("cursor", "7", "agent-bar")
    assert argv[-1] == ""
    assert argv[-2] == "-m"


def test_build_argv_unknown_runner_raises_systemexit() -> None:
    """Unknown runner names exit the process with a usage error."""
    with pytest.raises(SystemExit) as excinfo:
        build_argv("unknown", "1", "agent-1")
    # SystemExit can carry a string or an int; ours is a string message.
    assert "unknown runner: unknown" in str(excinfo.value)
