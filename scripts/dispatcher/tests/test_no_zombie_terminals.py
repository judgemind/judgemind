"""Issue #3574 — verify that every terminal-status UPDATE in
``agent-runner-entrypoint.sh`` also writes ``ended_at``.

Before the fix for #3574 two write paths stamped a terminal status
without setting ``ended_at``, leaving rows with
``status IN ('failed','succeeded',...) AND ended_at IS NULL`` — "zombie"
rows that are invisible to the ``agent_duration_s`` monitoring query.

This test is a static lint of the shell script. It does not require a
live database. Two classes:

- ``TestTerminalStatusUpdates`` — asserts that every
  ``UPDATE dispatcher.agents`` block whose SET clause contains a
  known terminal-status literal also contains ``ended_at``.
- ``TestExternalTerminalObservedCallsMarkEnded`` — asserts that the
  external-terminal-observed early-exit path calls ``mark_ended``
  before ``exit 0``.

**AC2 proof**: both classes fail against the pre-fix bash (where
``agent_runner_reaped_failure`` and ``advance_phase`` omit ``ended_at``)
and pass after the fix is applied.
"""

from __future__ import annotations

import re
from pathlib import Path

_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "agent-runner-entrypoint.sh"

# Terminal statuses from phase_transitions.TERMINAL_STATUSES.
_TERMINAL_STATUS_LITERALS = frozenset(
    {
        "failed",
        "succeeded",
        "crashed",
        "plan_blocked",
        "needs_review",
    }
)


def _function_body(text: str, func_name: str) -> str:
    """Extract the body text of a shell function definition.

    Returns the content between the opening ``{`` and the matching
    closing ``}`` (exclusive) of the named function. Raises
    ``AssertionError`` if the function is not found.
    """
    m = re.search(
        rf"^{re.escape(func_name)}\s*\(\s*\)\s*\{{",
        text,
        re.MULTILINE,
    )
    assert m is not None, f"Function {func_name!r} not found in entrypoint"
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
    return "".join(body_chars)


def _extract_update_blocks(sql_fragment: str) -> list[str]:
    """Extract contiguous UPDATE-statement blocks from a SQL fragment.

    A block starts at ``UPDATE`` and ends at the first ``;`` that appears
    outside of any quoting (naive single-quote detection sufficient for
    the shell heredoc/db_exec patterns used here).

    Returns a list of block strings (including the terminating ``;``).
    """
    blocks: list[str] = []
    # Find each UPDATE keyword position (case-insensitive).
    for m in re.finditer(r"\bUPDATE\b", sql_fragment, re.IGNORECASE):
        tail = sql_fragment[m.start() :]
        # Walk to the first unquoted semicolon.
        in_sq = False
        end = None
        for i, ch in enumerate(tail):
            if ch == "'" and not in_sq:
                in_sq = True
            elif ch == "'" and in_sq:
                in_sq = False
            elif ch == ";" and not in_sq:
                end = i + 1
                break
        if end is not None:
            blocks.append(tail[:end])
    return blocks


class TestTerminalStatusUpdates:
    """Every ``UPDATE dispatcher.agents`` that sets a terminal status must
    also write ``ended_at``. (#3574)
    """

    def test_entrypoint_script_exists(self) -> None:
        assert _ENTRYPOINT_PATH.exists(), (
            f"agent-runner-entrypoint.sh not found at {_ENTRYPOINT_PATH}"
        )

    def test_agent_runner_reaped_failure_update_writes_ended_at(self) -> None:
        """The UPDATE inside ``agent_runner_reaped_failure`` sets
        ``status = 'failed'`` — it must also set ``ended_at``.

        Pre-fix this fails because the SET clause was:
            SET phase = '$_term_phase', status = 'failed'
        Post-fix it is:
            SET phase = '$_term_phase', status = 'failed',
                ended_at = COALESCE(ended_at, now())
        """
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        body = _function_body(text, "agent_runner_reaped_failure")
        # Find UPDATE dispatcher.agents blocks inside the function body.
        update_blocks = _extract_update_blocks(body)
        agents_updates = [b for b in update_blocks if "dispatcher.agents" in b]
        assert agents_updates, (
            "agent_runner_reaped_failure must contain at least one "
            "'UPDATE dispatcher.agents' block"
        )
        for block in agents_updates:
            # Each block that touches a terminal status must also write ended_at.
            sets_terminal = any(
                f"status = '{s}'" in block or f'status = "{s}"' in block
                for s in _TERMINAL_STATUS_LITERALS
            )
            # The variable form 'failed' is hardcoded in this function.
            sets_failed_literal = "'failed'" in block or '"failed"' in block
            if sets_terminal or sets_failed_literal:
                assert "ended_at" in block, (
                    "agent_runner_reaped_failure UPDATE sets a terminal status "
                    "but does not write ended_at (#3574). Block:\n" + block
                )

    def test_advance_phase_two_arg_update_writes_ended_at(self) -> None:
        """The ``advance_phase`` 2-arg branch (terminal status write) must
        include ``ended_at`` in its SET clause.

        Pre-fix this fails because the 2-arg branch was:
            SET phase = '$_next', status = '$_status'
        Post-fix it is:
            SET phase = '$_next', status = '$_status',
                ended_at = COALESCE(ended_at, now())
        """
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        body = _function_body(text, "advance_phase")
        # Find UPDATE dispatcher.agents blocks inside advance_phase.
        update_blocks = _extract_update_blocks(body)
        agents_updates = [b for b in update_blocks if "dispatcher.agents" in b]
        assert agents_updates, (
            "advance_phase must contain at least one 'UPDATE dispatcher.agents' block"
        )
        # The 2-arg branch: it's the block that references both 'status' and
        # '$_status' (the variable). The 1-arg branch only sets phase.
        two_arg_updates = [b for b in agents_updates if "status" in b]
        assert two_arg_updates, (
            "advance_phase must have an UPDATE block that sets 'status' "
            "(the 2-arg terminal-write branch)"
        )
        for block in two_arg_updates:
            assert "ended_at" in block, (
                "advance_phase 2-arg (terminal status) UPDATE does not "
                "write ended_at (#3574). Block:\n" + block
            )

    def test_no_terminal_update_omits_ended_at(self) -> None:
        """Broad sweep: no ``UPDATE dispatcher.agents`` block in the whole
        script sets a terminal-status literal AND omits ``ended_at``.

        This catches future call sites that might be added outside the two
        known functions. The test uses the literal set
        {failed, succeeded, crashed, plan_blocked, needs_review} to detect
        hardcoded terminal writes; variable-form writes ($var) are covered
        by ``test_advance_phase_two_arg_update_writes_ended_at``.
        """
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        all_update_blocks = _extract_update_blocks(text)
        offenders: list[str] = []
        for block in all_update_blocks:
            if "dispatcher.agents" not in block:
                continue
            # Check if this block sets any terminal-status literal.
            sets_terminal_literal = any(
                f"status = '{s}'" in block or f'status = "{s}"' in block
                for s in _TERMINAL_STATUS_LITERALS
            )
            if sets_terminal_literal and "ended_at" not in block:
                offenders.append(block)
        assert offenders == [], (
            f"Found {len(offenders)} UPDATE dispatcher.agents block(s) that set "
            f"a terminal-status literal but omit ended_at (#3574):\n"
            + "\n---\n".join(offenders)
        )


class TestExternalTerminalObservedCallsMarkEnded:
    """The external-terminal-observed early-exit (loop top) must call
    ``mark_ended`` before ``exit 0`` (#3574 belt-and-suspenders).

    Pre-fix the path was:
        log "external_terminal_observed" ...
        exit 0

    Post-fix it is:
        log "external_terminal_observed" ...
        mark_ended
        exit 0
    """

    def test_external_terminal_observed_block_calls_mark_ended(self) -> None:
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        # Find the block between 'external_terminal_observed' log line
        # and the following 'exit 0'.  We look for the pattern:
        #   log "external_terminal_observed" ...
        #   ... (optional lines) ...
        #   mark_ended
        #   exit 0
        m = re.search(
            r'log\s+"external_terminal_observed"[^\n]*\n'  # log line
            r"(?:[^\n]*\n)*?"  # optional interleaved lines
            r"\s*mark_ended\b",  # mark_ended call
            text,
        )
        assert m is not None, (
            "The external_terminal_observed early-exit path must call "
            "mark_ended before exit 0 (#3574). Pattern not found in "
            "agent-runner-entrypoint.sh."
        )

    def test_mark_ended_precedes_exit_in_external_terminal_block(self) -> None:
        """mark_ended must appear BEFORE exit 0 in the external-terminal
        observed block."""
        text = _ENTRYPOINT_PATH.read_text(encoding="utf-8")
        # Locate 'external_terminal_observed' and grab the next ~10 lines.
        m = re.search(r"external_terminal_observed", text)
        assert m is not None, "'external_terminal_observed' not found in entrypoint"
        snippet = text[m.start() : m.start() + 500]
        mark_pos = snippet.find("mark_ended")
        exit_pos = snippet.find("exit 0")
        assert mark_pos != -1, (
            "mark_ended not found in the 500 chars after external_terminal_observed"
        )
        assert exit_pos != -1, (
            "exit 0 not found in the 500 chars after external_terminal_observed"
        )
        assert mark_pos < exit_pos, (
            f"mark_ended (pos {mark_pos}) must appear before exit 0 "
            f"(pos {exit_pos}) in the external_terminal_observed block"
        )
