"""Regression tests for ``push_and_pr`` and ``claude -p`` subprocess
timeouts in ``agent-runner-entrypoint.sh`` (#3683).

Without explicit ``timeout`` wrappers on local git operations and on
``claude -p`` itself, a hung subprocess silently pins the ECS cap slot
for up to 30 minutes until the heartbeat reaper fires.  This module
provides two types of coverage:

1. **AST-style static lint** — walks the shell script text line by line
   and asserts that every invocation of the bounded subprocess families
   (``git commit --amend``, ``git rebase``, ``git rebase --abort``,
   ``claude -p``) is preceded by a ``timeout "$<VAR>"`` token on the
   same logical line (or on the immediately preceding continuation
   line).  Catches future regressions where someone adds an unbounded
   subprocess.

2. **Constant presence + value guards** — assert the new
   ``CLAUDE_PHASE_TIMEOUT_SECONDS``, ``LOCAL_GIT_TIMEOUT_SECONDS``,
   and ``PUSH_AND_PR_PHASE_DEADLINE_SECONDS`` constants are defined in
   the script text with their expected default values (1800, 120, 600
   respectively).  Mirrors the style of
   ``test_daemon_git_push_timeout.py``'s constant-value test.

3. **Structured-event presence guards** — assert the new log event
   names (``claude_phase_timeout``, ``push_and_pr_commit_amend_timeout``,
   ``push_and_pr_rebase_timeout``, ``push_and_pr_rebase_abort_timeout``,
   ``push_and_pr_phase_wall_clock_exceeded``) appear in the script text
   so a future refactor cannot silently drop the structured triage
   fields operators rely on for CloudWatch Logs Insights queries.
"""

from __future__ import annotations

import re
from pathlib import Path

_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "agent-runner-entrypoint.sh"
# #3775: run_claude_phase was extracted to this sourceable helper.
_HELPER_PATH = Path(__file__).resolve().parents[1] / "agent_runner_run_claude_phase.sh"


def _script_text() -> str:
    return _ENTRYPOINT_PATH.read_text(encoding="utf-8")


def _helper_text() -> str:
    """Read agent_runner_run_claude_phase.sh — contains run_claude_phase,
    phase_to_skill, write_phase_input, read_phase_output,
    claude_phase_timeout_seconds_by_phase, and the per-phase timeout
    constants (#3775)."""
    return _HELPER_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Constant presence + value tests
# ---------------------------------------------------------------------------


class TestTimeoutConstantsDefined:
    """The three new timeout constants from #3683 must be defined in the
    script with the expected default values."""

    def test_claude_phase_timeout_seconds_defined_at_1800(self) -> None:
        text = _script_text()
        assert (
            'CLAUDE_PHASE_TIMEOUT_SECONDS="${AGENT_RUNNER_CLAUDE_PHASE_TIMEOUT_SECONDS:-1800}"'
            in text
        ), (
            "CLAUDE_PHASE_TIMEOUT_SECONDS must be defined with default 1800s (#3683). "
            "If you need a different default, update this test and the rationale comment."
        )

    def test_local_git_timeout_seconds_defined_at_120(self) -> None:
        text = _script_text()
        assert (
            'LOCAL_GIT_TIMEOUT_SECONDS="${AGENT_RUNNER_LOCAL_GIT_TIMEOUT_SECONDS:-120}"'
            in text
        ), (
            "LOCAL_GIT_TIMEOUT_SECONDS must be defined with default 120s (#3683). "
            "If you need a different default, update this test and the rationale comment."
        )

    def test_push_and_pr_phase_deadline_seconds_defined_at_600(self) -> None:
        text = _script_text()
        assert (
            'PUSH_AND_PR_PHASE_DEADLINE_SECONDS="${AGENT_RUNNER_PUSH_AND_PR_DEADLINE_SECONDS:-600}"'
            in text
        ), (
            "PUSH_AND_PR_PHASE_DEADLINE_SECONDS must be defined with default 600s (#3683). "
            "If you need a different default, update this test and the rationale comment."
        )


# ---------------------------------------------------------------------------
# Structured-event presence tests
# ---------------------------------------------------------------------------


class TestTimeoutLogEventsPresent:
    """Each new timeout site emits a distinct structured log event name
    that operators and CloudWatch Logs Insights queries rely on.  These
    checks guard against a refactor that silently drops the event."""

    def test_claude_phase_timeout_event_present(self) -> None:
        text = _script_text()
        assert (
            '"claude_phase_timeout"' in text
            or "'claude_phase_timeout'" in text
            or "claude_phase_timeout" in text
        ), (
            "agent-runner-entrypoint.sh must emit a ``claude_phase_timeout`` log "
            "event when the timeout wrapper fires on ``claude -p`` (#3683)."
        )

    def test_push_and_pr_commit_amend_timeout_event_present(self) -> None:
        text = _script_text()
        assert "push_and_pr_commit_amend_timeout" in text, (
            "agent-runner-entrypoint.sh must emit a ``push_and_pr_commit_amend_timeout`` "
            "log event when LOCAL_GIT_TIMEOUT_SECONDS fires on ``git commit --amend`` (#3683)."
        )

    def test_push_and_pr_rebase_timeout_event_present(self) -> None:
        text = _script_text()
        assert "push_and_pr_rebase_timeout" in text, (
            "agent-runner-entrypoint.sh must emit a ``push_and_pr_rebase_timeout`` "
            "log event when LOCAL_GIT_TIMEOUT_SECONDS fires on ``git rebase origin/main`` (#3683)."
        )

    def test_push_and_pr_rebase_abort_timeout_event_present(self) -> None:
        text = _script_text()
        assert "push_and_pr_rebase_abort_timeout" in text, (
            "agent-runner-entrypoint.sh must emit a ``push_and_pr_rebase_abort_timeout`` "
            "log event when LOCAL_GIT_TIMEOUT_SECONDS fires on ``git rebase --abort`` (#3683)."
        )

    def test_push_and_pr_phase_wall_clock_exceeded_event_present(self) -> None:
        text = _script_text()
        assert "push_and_pr_phase_wall_clock_exceeded" in text, (
            "agent-runner-entrypoint.sh must emit a ``push_and_pr_phase_wall_clock_exceeded`` "
            "log event when the wall-clock deadline fires in assert_phase_deadline_not_exceeded (#3683)."
        )


# ---------------------------------------------------------------------------
# assert_phase_deadline_not_exceeded helper presence
# ---------------------------------------------------------------------------


class TestAssertPhaseDeadlineHelper:
    """The ``assert_phase_deadline_not_exceeded`` helper must be defined
    and called at the required sites inside ``handle_push_and_pr``."""

    def test_helper_function_defined(self) -> None:
        text = _script_text()
        assert re.search(
            r"^assert_phase_deadline_not_exceeded\s*\(\s*\)\s*\{",
            text,
            re.MULTILINE,
        ), (
            "assert_phase_deadline_not_exceeded function must be defined in "
            "agent-runner-entrypoint.sh (#3683)"
        )

    def test_helper_called_at_least_three_times(self) -> None:
        """The helper must be called at a minimum before: the amend block,
        the fetch/rebase block, and the push step.  Three call sites is the
        floor; future hardening may add more."""
        text = _script_text()
        # Count executable (non-comment) occurrences of the helper call.
        call_count = 0
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "assert_phase_deadline_not_exceeded" in line and "() {" not in line:
                call_count += 1
        assert call_count >= 3, (
            f"assert_phase_deadline_not_exceeded must be called at least 3 times "
            f"inside handle_push_and_pr (#3683); found {call_count} call(s). "
            "Expected call sites: before commit-amend, before fetch/rebase, before push."
        )

    def test_helper_uses_phase_start_variable(self) -> None:
        """The helper computes elapsed from ``_phase_start`` — guard against
        a rename that would silently make the deadline always 0."""
        text = _script_text()
        assert "_phase_start" in text, (
            "_phase_start variable must be used by assert_phase_deadline_not_exceeded (#3683)"
        )


# ---------------------------------------------------------------------------
# timeout wrapper presence — static lint
# ---------------------------------------------------------------------------


def _extract_function_body(text: str, func_name: str) -> tuple[int, str]:
    """Return (start_lineno, body_text) for the named shell function.

    Scans for ``func_name() {`` and extracts everything up to the matching
    closing brace (tracking brace depth).  Returns the line number of the
    opening line and the body text.  Raises AssertionError if not found.
    """
    lines = text.splitlines()
    start_lineno: int | None = None
    body_lines: list[str] = []
    depth = 0
    in_body = False

    for i, line in enumerate(lines, start=1):
        if not in_body:
            # Match ``func_name() {`` or ``func_name () {``.
            if re.match(rf"^{re.escape(func_name)}\s*\(\s*\)\s*\{{", line):
                start_lineno = i
                in_body = True
                depth = 1
                continue
        else:
            for ch in line:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            if depth <= 0:
                break
            body_lines.append(line)

    assert start_lineno is not None, f"Function {func_name!r} not found in script"
    return start_lineno, "\n".join(body_lines)


def _logical_lines(text: str, base_lineno: int = 1) -> list[tuple[int, str]]:
    """Join shell continuation lines (trailing backslash) and return
    (start_lineno, joined_text) tuples.  Strips comment-only lines.

    ``base_lineno`` is added to each index so callers that pass a function
    body excerpt get correct absolute line numbers.
    """
    result: list[tuple[int, str]] = []
    pending: list[str] = []
    start_lineno = base_lineno
    for i, raw in enumerate(text.splitlines(), start=base_lineno):
        stripped = raw.lstrip()
        # Skip comment-only lines.
        if stripped.startswith("#"):
            if not pending:
                continue
        if not pending:
            start_lineno = i
        pending.append(raw.rstrip())
        if raw.rstrip().endswith("\\"):
            # continuation — keep accumulating.
            continue
        joined = " ".join(part.rstrip("\\").strip() for part in pending)
        if joined.strip():
            result.append((start_lineno, joined))
        pending = []
    if pending:
        joined = " ".join(part.rstrip("\\").strip() for part in pending)
        if joined.strip():
            result.append((start_lineno, joined))
    return result


class TestTimeoutWrappersPresent:
    """Every ``git rebase``, ``git commit --amend``, and ``claude -p``
    invocation inside ``handle_push_and_pr`` / ``run_claude_phase`` must be
    wrapped by a ``timeout "$<VAR>"`` prefix on the same logical line.

    Scope: only ``handle_push_and_pr`` (for git ops) and ``run_claude_phase``
    (for ``claude -p``).  ``handle_ralph_baseline_rebase`` is explicitly out
    of scope per issue #3683.

    This is an AST-style static lint of the shell script — it doesn't
    execute the script or require a running Bash.  The intent is to catch
    regressions where someone adds an unbounded subprocess call.
    """

    def _push_and_pr_lines(self) -> list[tuple[int, str]]:
        text = _script_text()
        start_lineno, body = _extract_function_body(text, "handle_push_and_pr")
        return _logical_lines(body, base_lineno=start_lineno + 1)

    def _run_claude_phase_lines(self) -> list[tuple[int, str]]:
        # #3775: run_claude_phase was extracted to agent_runner_run_claude_phase.sh.
        text = _helper_text()
        start_lineno, body = _extract_function_body(text, "run_claude_phase")
        return _logical_lines(body, base_lineno=start_lineno + 1)

    def test_git_commit_amend_has_timeout_wrapper(self) -> None:
        """Every ``git commit --amend`` logical line inside
        ``handle_push_and_pr`` must be wrapped by
        ``timeout "$<VAR>_TIMEOUT_SECONDS"``."""
        for lineno, line in self._push_and_pr_lines():
            if line.strip().startswith("#"):
                continue
            if "git" in line and "commit" in line and "--amend" in line:
                assert re.search(r'timeout\s+"\$\w*TIMEOUT_SECONDS"', line), (
                    f"L{lineno}: ``git commit --amend`` in handle_push_and_pr is not "
                    f'wrapped by ``timeout "$<VAR>_TIMEOUT_SECONDS"`` (#3683). '
                    f"Line: {line!r}"
                )

    def test_git_rebase_origin_main_has_timeout_wrapper(self) -> None:
        """Every ``git rebase origin/main`` logical line inside
        ``handle_push_and_pr`` must be wrapped by
        ``timeout "$<VAR>_TIMEOUT_SECONDS"``."""
        for lineno, line in self._push_and_pr_lines():
            if line.strip().startswith("#"):
                continue
            # Match lines that call ``git rebase origin/main`` specifically.
            # Exclude ``rev-list`` / ``rev-parse`` / ``diff`` lines that
            # happen to contain ``origin/main`` as an argument — those are
            # not rebase invocations and should not be timed out.
            if (
                re.search(r"\bgit\b.*\brebase\b", line)
                and "origin/main" in line
                and "--abort" not in line
            ):
                assert re.search(r'timeout\s+"\$\w*TIMEOUT_SECONDS"', line), (
                    f"L{lineno}: ``git rebase origin/main`` in handle_push_and_pr is not "
                    f'wrapped by ``timeout "$<VAR>_TIMEOUT_SECONDS"`` (#3683). '
                    f"Line: {line!r}"
                )

    def test_git_rebase_abort_has_timeout_wrapper(self) -> None:
        """Every ``git rebase --abort`` logical line inside
        ``handle_push_and_pr`` must be wrapped by
        ``timeout "$<VAR>_TIMEOUT_SECONDS"``."""
        for lineno, line in self._push_and_pr_lines():
            if line.strip().startswith("#"):
                continue
            if re.search(r"\bgit\b.*\brebase\b", line) and "--abort" in line:
                assert re.search(r'timeout\s+"\$\w*TIMEOUT_SECONDS"', line), (
                    f"L{lineno}: ``git rebase --abort`` in handle_push_and_pr is not "
                    f'wrapped by ``timeout "$<VAR>_TIMEOUT_SECONDS"`` (#3683). '
                    f"Line: {line!r}"
                )

    def test_claude_p_has_timeout_wrapper_in_run_claude_phase(self) -> None:
        """Every ``claude -p`` logical line inside ``run_claude_phase`` must
        be wrapped by ``timeout "$CLAUDE_PHASE_TIMEOUT_SECONDS"`` (or
        similar ``$<VAR>_TIMEOUT_SECONDS``).

        #3766 — also accept ``timeout "$_phase_timeout"`` (the per-phase
        lookup result variable populated from
        ``CLAUDE_PHASE_TIMEOUT_SECONDS_BY_PHASE``). The semantic intent
        is unchanged — the value is still derived from a TIMEOUT_SECONDS
        env-driven constant — but the lookup happens via the per-phase
        table at function-call time.
        """
        for lineno, line in self._run_claude_phase_lines():
            if line.strip().startswith("#"):
                continue
            if "claude" in line and "-p" in line and "--output-format" in line:
                # Accept either the legacy ``timeout "$<VAR>_TIMEOUT_SECONDS"``
                # form OR the #3766 ``timeout "$_phase_timeout"`` form.
                assert re.search(
                    r'timeout\s+"\$\w*TIMEOUT_SECONDS"', line
                ) or re.search(r'timeout\s+"\$_phase_timeout"', line), (
                    f"L{lineno}: ``claude -p`` in run_claude_phase is not wrapped by "
                    f'``timeout "$<VAR>_TIMEOUT_SECONDS"`` or ``timeout "$_phase_timeout"`` '
                    f"(#3683, #3766). Line: {line!r}"
                )


# ---------------------------------------------------------------------------
# --no-verify presence tests (#3800)
# ---------------------------------------------------------------------------


class TestPushNoVerify:
    """Every machine ``git push`` in agent-runner-entrypoint.sh must pass
    ``--no-verify`` so the pre-push hook (already run by ralph's worker
    during iteration) is not redundantly re-executed on the Fargate host,
    where it consumes ~5+ minutes of wall-clock against the 300s
    NETWORK_TIMEOUT_SECONDS cap (#3800).

    Three push sites are guarded:
    - ``handle_push_and_pr`` — the bounded timeout-wrapped primary push.
    - ``handle_fix_ci`` — the CI-fix commit push.
    - ``merge_unstick`` — the empty-commit push used to unblock stuck merges.
    """

    def test_push_and_pr_uses_no_verify(self) -> None:
        """The ``handle_push_and_pr`` push line must include ``--no-verify``
        (#3800)."""
        text = _script_text()
        _, body = _extract_function_body(text, "handle_push_and_pr")
        logical = _logical_lines(body)
        push_lines = [
            (lineno, line)
            for lineno, line in logical
            if re.search(r"\bgit\b.*\bpush\b", line)
            and "origin" in line
            and "$BRANCH_NAME" in line
        ]
        assert push_lines, (
            "handle_push_and_pr must contain a ``git ... push ... origin ... $BRANCH_NAME`` "
            "line (#3800)"
        )
        for lineno, line in push_lines:
            assert "--no-verify" in line, (
                f"L{lineno}: handle_push_and_pr push line must include ``--no-verify`` "
                f"so the pre-push hook is not redundantly re-run on the Fargate host "
                f"(#3800). Line: {line!r}"
            )

    def test_fix_ci_push_uses_no_verify(self) -> None:
        """The ``handle_fix_ci`` push line must include ``--no-verify``
        (#3800)."""
        text = _script_text()
        _, body = _extract_function_body(text, "handle_fix_ci")
        logical = _logical_lines(body)
        push_lines = [
            (lineno, line)
            for lineno, line in logical
            if re.search(r"\bgit\b.*\bpush\b", line)
            and "origin" in line
            and "$BRANCH_NAME" in line
        ]
        assert push_lines, (
            "handle_fix_ci must contain a ``git ... push ... origin ... $BRANCH_NAME`` "
            "line (#3800)"
        )
        for lineno, line in push_lines:
            assert "--no-verify" in line, (
                f"L{lineno}: handle_fix_ci push line must include ``--no-verify`` "
                f"so the pre-push hook is not redundantly re-run on the Fargate host "
                f"(#3800). Line: {line!r}"
            )

    def test_merge_unstick_push_uses_no_verify(self) -> None:
        """The ``try_auto_unstick_merge`` push line must include ``--no-verify``
        (#3800)."""
        text = _script_text()
        _, body = _extract_function_body(text, "try_auto_unstick_merge")
        logical = _logical_lines(body)
        push_lines = [
            (lineno, line)
            for lineno, line in logical
            if re.search(r"\bgit\b.*\bpush\b", line)
            and "origin" in line
            and "$BRANCH_NAME" in line
        ]
        assert push_lines, (
            "try_auto_unstick_merge must contain a ``git ... push ... origin ... $BRANCH_NAME`` "
            "line (#3800)"
        )
        for lineno, line in push_lines:
            assert "--no-verify" in line, (
                f"L{lineno}: try_auto_unstick_merge push line must include ``--no-verify`` "
                f"so the pre-push hook is not redundantly re-run on the Fargate host "
                f"(#3800). Line: {line!r}"
            )
