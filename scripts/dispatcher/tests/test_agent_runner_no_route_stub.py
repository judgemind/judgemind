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


class TestRalphNotShipDescriptiveTerminal:
    """Issue #3586 — ``ralph_not_ship`` is now routed through the
    diagnoser via the descriptive-terminal path (BYPASSED_TERMINAL_PHASES_TO_ROUTE),
    not handled locally by the defunct ``handle_ralph_not_ship_local``
    function from #3455."""

    def test_handle_ralph_not_ship_local_function_not_defined(self) -> None:
        """The local handler must be gone — #3586 removed it."""
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        assert not re.search(
            r"^handle_ralph_not_ship_local\s*\(\s*\)\s*\{",
            text,
            re.MULTILINE,
        ), (
            "handle_ralph_not_ship_local function must not be defined (#3586 removed it)"
        )

    def test_route_to_diagnoser_dispatches_ralph_not_ship_as_descriptive_terminal(
        self,
    ) -> None:
        """Verify the ``ralph_not_ship)`` case arm now calls
        ``agent_runner_reaped_failure "ralph_not_ship"`` directly
        (descriptive-terminal path) rather than the local handler."""
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        # The case arm should call agent_runner_reaped_failure with
        # "ralph_not_ship" as the phase arg — no handle_ralph_not_ship_local call.
        assert re.search(
            r'agent_runner_reaped_failure\s*\\\s*\n\s*"ralph_not_ship"',
            text,
        ), (
            "ralph_not_ship case arm must emit descriptive terminal via "
            'agent_runner_reaped_failure "ralph_not_ship" (#3586)'
        )

    def test_ralph_not_ship_case_arm_not_calling_local_handler(self) -> None:
        """Double-check: the ralph_not_ship case arm must NOT call
        handle_ralph_not_ship_local anywhere in executable code."""
        for lineno, line in _executable_lines():
            assert "handle_ralph_not_ship_local" not in line, (
                f"handle_ralph_not_ship_local found in executable code at L{lineno} "
                "(#3586 removed this function)"
            )


class TestReapedFailureExitsUnconditionally:
    """Issue #3494 — static lint: ``agent_runner_reaped_failure`` must
    terminate the script via ``exit 0`` as its last non-comment,
    non-blank line.

    PR #3458 replaced ``advance_phase agent_runner_route_stub`` with
    ``agent_runner_reaped_failure`` calls that emit descriptive phase
    strings. None of those strings were in ``TERMINAL_PHASES``, so the
    dispatch loop re-read the phase, found no matching case arm, fell to
    ``*)``, and re-emitted ``phase_unknown`` for up to 40 iterations
    before the safety cap fired. The fix adds ``exit 0`` as the final
    statement of the function body; this test ensures it stays there.
    """

    def _function_body_lines(self) -> list[str]:
        """Extract the body lines of ``agent_runner_reaped_failure()``."""
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        # Match the function definition opening.
        m = re.search(
            r"^agent_runner_reaped_failure\s*\(\s*\)\s*\{",
            text,
            re.MULTILINE,
        )
        assert m is not None, "agent_runner_reaped_failure function not found"
        # Walk forward from the opening brace to the matching closing brace.
        # We count brace depth; the function is closed when depth returns to 0.
        start = m.end()
        body_chars: list[str] = []
        depth = 1
        for ch in text[start:]:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            body_chars.append(ch)
        return "".join(body_chars).splitlines()

    def test_agent_runner_reaped_failure_last_line_is_exit_0(self) -> None:
        """The last non-comment, non-blank line of ``agent_runner_reaped_failure``
        must be ``exit 0`` (#3494)."""
        lines = self._function_body_lines()
        # Collect non-comment, non-blank lines.
        executable = [
            line for line in lines if line.strip() and not line.lstrip().startswith("#")
        ]
        assert executable, "agent_runner_reaped_failure body has no executable lines"
        last = executable[-1].strip()
        assert last == "exit 0", (
            f"Last executable line of agent_runner_reaped_failure must be "
            f"'exit 0' (#3494) to close the dispatch loop. Got: {last!r}"
        )
