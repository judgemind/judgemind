"""Issue #3455 — verify ``agent-runner-entrypoint.sh`` does not advance
to ``agent_runner_route_stub`` outside comments.

The fix replaced 5 fall-through call sites that previously emitted
``advance_phase "agent_runner_route_stub" "<status>"`` with descriptive
``agent_runner_reaped_failure`` invocations. The phase string
``route_stub`` may still appear in comments documenting the change, but
must not appear in any executable line.

This test is a static lint of the shell script — it parses the file
line by line, strips comment-only lines and trailing comments, and
greps the executable remainder for ``route_stub``. Any hit fails.
"""

from __future__ import annotations

import re
from pathlib import Path

# The agent-runner entrypoint, anchored to the dispatcher package.
_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "agent-runner-entrypoint.sh"


def _strip_comment(line: str) -> str:
    """Return ``line`` with any trailing ``#`` comment removed.

    Naive but sufficient for shell — we don't have ``#`` inside string
    literals in the dispatch sites under test. The only quoted strings
    around the call sites are the phase / category / reason args to
    ``agent_runner_reaped_failure``, none of which contain ``#``.
    """
    # Skip comment-only lines outright.
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return ""
    # Find the first ``#`` that is not preceded by a backslash. Bash
    # comments require a space-prefix when not at start-of-line, so the
    # naive scan-for-`# ` works.
    m = re.search(r"(^|[^\\])# ", line)
    if m is None:
        return line
    cut = m.start() if m.group(1) == "" else m.start() + 1
    return line[:cut]


def _executable_lines() -> list[tuple[int, str]]:
    """Return ``(lineno, executable_part)`` for every non-comment line."""
    text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        executable = _strip_comment(line)
        if executable.strip():
            out.append((i, executable))
    return out


class TestNoRouteStubInExecutableLines:
    def test_entrypoint_script_exists(self) -> None:
        assert _ENTRYPOINT_PATH.exists(), (
            f"agent-runner-entrypoint.sh not found at {_ENTRYPOINT_PATH}"
        )

    def test_no_route_stub_in_executable_code(self) -> None:
        """The bright line from #3455: zero ``route_stub`` references
        in executable shell. Comment-only references (documenting the
        prior bug or the fix) are allowed."""
        offenders = [
            (lineno, line.rstrip())
            for (lineno, line) in _executable_lines()
            if "route_stub" in line
        ]
        assert offenders == [], (
            "agent-runner-entrypoint.sh must not contain `route_stub` "
            "in executable lines (#3455). Hits:\n"
            + "\n".join(f"  L{n}: {text}" for n, text in offenders)
        )

    def test_no_advance_to_route_stub_phase(self) -> None:
        """Even-stronger check: no line advances to a phase named
        ``agent_runner_route_stub``. ``advance_phase
        "agent_runner_route_stub"`` was the buggy emit; the fix uses
        ``agent_runner_reaped_failure`` with descriptive phase names.
        """
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        # Build a regex that matches advance_phase "agent_runner_route_stub"
        # — covering single, double, and unquoted variants.
        pattern = re.compile(
            r'^[^#]*advance_phase\s+"?agent_runner_route_stub"?',
            re.MULTILINE,
        )
        hits = pattern.findall(text)
        assert hits == [], (
            f"`advance_phase agent_runner_route_stub` must not appear in "
            f"executable code (#3455). Found {len(hits)} hit(s)."
        )


class TestRalphNotShipLocalHandlerExists:
    """Issue #3455 — the new handler that handles ``ralph_not_ship``
    locally instead of routing to the diagnoser."""

    def test_handle_ralph_not_ship_local_function_defined(self) -> None:
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        # Match the function definition opening — ``handle_ralph_not_ship_local() {``.
        assert re.search(
            r"^handle_ralph_not_ship_local\s*\(\s*\)\s*\{",
            text,
            re.MULTILINE,
        ), "handle_ralph_not_ship_local function not defined"

    def test_handler_posts_comment_with_block_reason(self) -> None:
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        # The handler body should reference both gh issue comment + the
        # block_reason it relays.
        assert "gh issue comment" in text
        assert "block_reason" in text
        # And it should add status/blocked + remove agent/ready.
        assert "--add-label status/blocked" in text
        assert "--remove-label agent/ready" in text

    def test_handler_emits_ralph_not_ship_terminal(self) -> None:
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        # The handler should call agent_runner_reaped_failure with
        # the ``ralph_not_ship`` terminal phase.
        assert re.search(
            r'agent_runner_reaped_failure\s*\\?\s*\n?\s*"ralph_not_ship"',
            text,
        ), "handler must emit `ralph_not_ship` terminal via agent_runner_reaped_failure"

    def test_route_to_diagnoser_dispatches_ralph_not_ship_locally(self) -> None:
        """Verify the case-block in the post-claude dispatch arm calls
        the local handler for ``ralph_not_ship`` (not advance_phase to
        agent_runner_route_stub)."""
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        # Look for the ``ralph_not_ship)`` case arm calling
        # ``handle_ralph_not_ship_local``.
        m = re.search(
            r"ralph_not_ship\)\s*\n\s*log\b[^\n]*\n\s*handle_ralph_not_ship_local",
            text,
        )
        assert m is not None, (
            "route_to_diagnoser case arm should dispatch `ralph_not_ship` to "
            "handle_ralph_not_ship_local (not route_stub)"
        )
