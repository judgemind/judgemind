"""Issue #3822 — verify ``agent-runner-entrypoint.sh`` stamps ``ended_at``
in the SQL UPDATE whenever it writes a terminal status to
``dispatcher.agents``.

Symptom: every recent daemon-shipped green has ``ended_at IS NULL``,
making the row invisible to the admin cockpit's "Recently Completed"
GraphQL query in ``packages/api/src/graphql/dispatcher/resolvers.ts:534``,
which filters by ``status terminal AND ended_at IS NOT NULL``.

Root cause (two code sites, both in the agent-runner entrypoint):

1. ``advance_phase`` writes ``phase = $next, status = $status`` but NOT
   ``ended_at``. The merge handler calls
   ``advance_phase "awaiting_deploy" "succeeded"`` after a squash-merge
   lands. The next outer-loop iteration sees a terminal status and
   exits via ``external_terminal_observed`` BEFORE ``mark_ended`` runs.
2. ``agent_runner_reaped_failure`` writes ``phase = $term_phase, status
   = 'failed'`` and exits — same shape.

Fix shape: when the new status is in ``TERMINAL_STATUSES``, the UPDATE
must also write ``ended_at = COALESCE(ended_at, now())``. The
``COALESCE`` keeps the write idempotent (preserves any earlier stamp,
e.g. from the diagnoser/killswitch external-writer paths) and race-safe
with the daemon-side housekeeping bulk backfill in
``daemon._backfill_terminal_ended_at``.

This is a static lint of the shell script — it parses specific patterns
in the file text rather than executing the script. The intent is to
catch regressions where one of the two terminal-write sites loses the
``ended_at`` stamp clause.
"""

from __future__ import annotations

import re
from pathlib import Path

_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "agent-runner-entrypoint.sh"


def _script_text() -> str:
    return _ENTRYPOINT_PATH.read_text(encoding="utf-8")


def _function_body(func_name: str) -> str:
    """Return the body of a top-level shell function as a single string.

    Walks brace depth from the opening ``{`` to the matching ``}`` so
    nested ``if``/``case`` blocks don't fool the extractor. Used to scope
    SQL-shape assertions to the exact function under test instead of the
    whole 6000-line file.
    """
    text = _script_text()
    pattern = rf"^{re.escape(func_name)}\s*\(\s*\)\s*\{{"
    m = re.search(pattern, text, re.MULTILINE)
    assert m is not None, f"function {func_name}() not found in entrypoint"
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


class TestEntrypointScriptExists:
    def test_entrypoint_script_exists(self) -> None:
        assert _ENTRYPOINT_PATH.exists(), (
            f"agent-runner-entrypoint.sh not found at {_ENTRYPOINT_PATH}"
        )


class TestAdvancePhaseStampsEndedAtOnTerminalStatus:
    """``advance_phase`` must include ``ended_at = COALESCE(ended_at,
    now())`` in the UPDATE when ``_status`` is in TERMINAL_STATUSES."""

    def test_advance_phase_function_exists(self) -> None:
        body = _function_body("advance_phase")
        assert body.strip(), "advance_phase function body is empty"

    def test_advance_phase_branches_on_is_terminal_status(self) -> None:
        """The function must branch on ``is_terminal_status`` so the
        terminal write gets the ``ended_at`` stamp and the non-terminal
        write does not (the latter is an in-progress phase advance and
        must NOT stamp ended_at — that would lock the row out of the
        scheduler's status reads)."""
        body = _function_body("advance_phase")
        assert "is_terminal_status" in body, (
            "advance_phase must call is_terminal_status to gate the ended_at "
            "stamp on terminal-status writes (#3822). Without the gate, "
            "every status write would stamp ended_at — locking in-flight "
            "agents out of scheduler reads."
        )

    def test_advance_phase_terminal_branch_writes_ended_at(self) -> None:
        """The terminal branch's UPDATE must include
        ``ended_at = COALESCE(ended_at, now())``. The COALESCE preserves
        any earlier stamp (idempotent + race-safe with the daemon-side
        backfill)."""
        body = _function_body("advance_phase")
        # The branching shape must contain a COALESCE-stamping UPDATE.
        # Whitespace is variable due to bash heredoc-style indentation,
        # so match the key tokens flexibly.
        assert re.search(
            r"ended_at\s*=\s*COALESCE\(\s*ended_at\s*,\s*now\(\)\s*\)",
            body,
        ), (
            "advance_phase terminal branch must write "
            "`ended_at = COALESCE(ended_at, now())` in the UPDATE (#3822). "
            "Without this, every daemon-shipped green has ended_at=NULL "
            "and is invisible to the admin cockpit's Recently Completed "
            "panel."
        )

    def test_advance_phase_non_terminal_branch_does_not_stamp_ended_at(
        self,
    ) -> None:
        """The non-terminal branch (status given but not in TERMINAL_STATUSES)
        and the no-status branch (phase-only advance) must NOT stamp
        ``ended_at``. Stamping there would mark in-flight agents as ended.

        We verify by counting ``COALESCE(ended_at, now())`` occurrences
        in the function body — there must be exactly one (the terminal
        branch). More than one means a non-terminal branch accidentally
        gained the stamp.
        """
        body = _function_body("advance_phase")
        coalesce_count = len(
            re.findall(
                r"ended_at\s*=\s*COALESCE\(\s*ended_at\s*,\s*now\(\)\s*\)",
                body,
            )
        )
        assert coalesce_count == 1, (
            "advance_phase must contain exactly one "
            "`ended_at = COALESCE(ended_at, now())` clause — the terminal "
            f"branch only. Found {coalesce_count} occurrences (#3822)."
        )


class TestAgentRunnerReapedFailureStampsEndedAt:
    """``agent_runner_reaped_failure`` must include ``ended_at =
    COALESCE(ended_at, now())`` in its terminal UPDATE.

    Without this, every reaped-failure path leaves ``ended_at`` NULL —
    same invisibility bug as the success path. The reaper's terminal
    UPDATE is unconditionally terminal (the function exits 0
    immediately after the UPDATE per #3494), so no branching guard is
    needed.
    """

    def test_function_exists(self) -> None:
        body = _function_body("agent_runner_reaped_failure")
        assert body.strip(), "agent_runner_reaped_failure function body is empty"

    def test_terminal_update_writes_ended_at(self) -> None:
        body = _function_body("agent_runner_reaped_failure")
        # Find the UPDATE to dispatcher.agents inside the function.
        # There may be multiple SQL statements (phase_outputs INSERT first,
        # then agents UPDATE) — we want the one against
        # ``dispatcher.agents``.
        m = re.search(
            r"UPDATE\s+dispatcher\.agents[\s\S]*?WHERE\s+agent_id",
            body,
        )
        assert m is not None, (
            "agent_runner_reaped_failure must contain an "
            "`UPDATE dispatcher.agents ... WHERE agent_id` statement"
        )
        update_sql = m.group(0)
        assert "status = 'failed'" in update_sql, (
            "agent_runner_reaped_failure UPDATE must set `status = 'failed'`"
        )
        assert re.search(
            r"ended_at\s*=\s*COALESCE\(\s*ended_at\s*,\s*now\(\)\s*\)",
            update_sql,
        ), (
            "agent_runner_reaped_failure UPDATE must write "
            "`ended_at = COALESCE(ended_at, now())` (#3822). Without this, "
            "reaped-failure rows have ended_at=NULL and are invisible to "
            "the admin cockpit's Recently Completed panel."
        )


class TestSchedulerStatusReadsStillSeeRunningAgents:
    """Belt-and-suspenders: the no-status branch of ``advance_phase``
    (phase-only advance, e.g. mid-loop transitions) must remain
    ``ended_at``-untouched.

    This test pins the shape: the no-status branch issues a UPDATE that
    sets only ``phase = '$_next'`` (no ``status`` column, no
    ``ended_at`` column).
    """

    def test_no_status_branch_does_not_set_status_or_ended_at(self) -> None:
        body = _function_body("advance_phase")
        # The no-status `else` branch is the third UPDATE block (the
        # terminal-status branch is first, the non-terminal-status
        # branch is second). Find all UPDATE statements inside the
        # function.
        updates = re.findall(
            r"db_exec\s+\"UPDATE dispatcher\.agents[\s\S]*?WHERE agent_id = '\$AGENT_ID';\"",
            body,
        )
        # Should be exactly 3 UPDATE call sites in the function:
        # 1. terminal-status branch (with status + ended_at COALESCE)
        # 2. non-terminal-status branch (with status, no ended_at)
        # 3. no-status branch (phase only)
        assert len(updates) == 3, (
            f"advance_phase must contain exactly 3 db_exec UPDATE call sites "
            f"(terminal, non-terminal-with-status, phase-only). Got {len(updates)}."
        )
        # The phase-only branch (3rd) must NOT contain `status` or `ended_at`.
        phase_only_update = updates[2]
        assert "status" not in phase_only_update, (
            "advance_phase phase-only branch must not write the `status` "
            "column (would clobber an earlier status). Got: " + phase_only_update
        )
        assert "ended_at" not in phase_only_update, (
            "advance_phase phase-only branch must not stamp `ended_at` "
            "(would mark in-flight agents as ended). Got: " + phase_only_update
        )
