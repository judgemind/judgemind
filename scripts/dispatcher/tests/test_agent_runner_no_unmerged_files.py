"""Issue #3465 — verify ``agent-runner-entrypoint.sh`` emits the
``no_unmerged_files`` envelope when git rebase exits non-zero with an
empty conflict-files list, and captures ``_rebase_stderr_tail`` from
the rebase stderr log for both affected rebase-failure blocks.

This is a static lint of the shell script — it parses specific patterns
in the file text rather than executing the script. The intent is to catch
regressions where one of the two rebase-failure sites loses the new
branching logic or the stderr-tail capture.
"""

from __future__ import annotations

import re
from pathlib import Path

_ENTRYPOINT_PATH = Path(__file__).resolve().parents[1] / "agent-runner-entrypoint.sh"


def _script_text() -> str:
    return _ENTRYPOINT_PATH.read_text(encoding="utf-8")


class TestEntrypointScriptExists:
    def test_entrypoint_script_exists(self) -> None:
        assert _ENTRYPOINT_PATH.exists(), (
            f"agent-runner-entrypoint.sh not found at {_ENTRYPOINT_PATH}"
        )


class TestEntrypointEmitsNoUnmergedFilesEnvelope:
    """The entrypoint must emit the no_unmerged_files JSON literal in
    both rebase-failure blocks (push_and_pr and ralph baseline)."""

    def test_no_unmerged_files_literal_present_in_push_and_pr_block(self) -> None:
        text = _script_text()
        # The push_and_pr block emits
        # ``"no_unmerged_files": true`` when conflict_files is empty.
        assert '"no_unmerged_files": true' in text, (
            'agent-runner-entrypoint.sh must contain `"no_unmerged_files": true` '
            "in the push_and_pr rebase-failure block (#3465)"
        )

    def test_no_unmerged_files_appears_at_least_twice(self) -> None:
        # Must appear in both push_and_pr AND ralph-baseline blocks.
        text = _script_text()
        count = text.count('"no_unmerged_files": true')
        assert count >= 2, (
            f'`"no_unmerged_files": true` must appear in both push_and_pr and '
            f"ralph-baseline rebase-failure blocks (#3465), found {count} occurrence(s)"
        )


class TestEntrypointCapturesRebaseStderrTail:
    """The entrypoint must capture ``_rebase_stderr_tail`` (or an
    equivalently named variable) from the git-rebase stderr log near
    each rebase-failure block."""

    def test_push_and_pr_block_captures_stderr_tail(self) -> None:
        text = _script_text()
        # The push_and_pr block captures from git-rebase.stderr.log.
        assert "git-rebase.stderr.log" in text, (
            "push_and_pr rebase-failure block must reference git-rebase.stderr.log (#3465)"
        )
        assert "_rebase_stderr_tail" in text, (
            "_rebase_stderr_tail variable must be captured near the "
            "push_and_pr rebase-failure block (#3465)"
        )

    def test_ralph_baseline_block_captures_stderr_tail(self) -> None:
        text = _script_text()
        # The ralph baseline block captures from ralph-baseline-rebase.stderr.log.
        assert "ralph-baseline-rebase.stderr.log" in text, (
            "ralph-baseline rebase-failure block must reference "
            "ralph-baseline-rebase.stderr.log (#3465)"
        )
        assert "_baseline_rebase_stderr_tail" in text, (
            "_baseline_rebase_stderr_tail variable must be captured near the "
            "ralph-baseline rebase-failure block (#3465)"
        )

    def test_stderr_tail_uses_tail_and_head_for_size_cap(self) -> None:
        # The capture pattern uses ``tail -n 50 ... | head -c 5120`` to
        # implement the ~50-line / ~5 KB cap.
        text = _script_text()
        assert "tail -n 50" in text, (
            "stderr-tail capture must use `tail -n 50` for the ~50-line cap (#3465)"
        )
        assert "head -c 5120" in text, (
            "stderr-tail capture must use `head -c 5120` for the ~5 KB size cap (#3465)"
        )


class TestRouteToDignoserListsPushAndPrNoUnmergedFiles:
    """The dispatch case statement for ``route_to_diagnoser`` must
    include ``push_and_pr_no_unmerged_files`` in the descriptive-hint
    allow-list so the new terminal phase gets picked up by the daemon's
    supervisor sweep."""

    def test_dispatch_case_includes_push_and_pr_no_unmerged_files(self) -> None:
        text = _script_text()
        # The descriptive-terminal case arm pattern:
        #   ralph_ac_infeasible|...|push_and_pr_no_unmerged_files)
        assert re.search(
            r"ralph_ac_infeasible\|[^)]*push_and_pr_no_unmerged_files",
            text,
        ), (
            "route_to_diagnoser dispatch case must include "
            "`push_and_pr_no_unmerged_files` in the descriptive-hint allow-list (#3465)"
        )


class TestPushAndPrCaseArmHasRouteToDiagnoser:
    """The push_and_pr) case-arm must contain a ``route_to_diagnoser)``
    arm and must NOT special-case ``push_and_pr_no_unmerged_files`` as a
    hint sub-case (#3558 — uniform dispatch, no inner case "$_hint")."""

    @staticmethod
    def _push_and_pr_block(text: str) -> str:
        """Return the substring between ``push_and_pr)`` and the next
        top-level ``fix_conflict)`` arm."""
        start = text.find("        push_and_pr)\n")
        end = text.find("        fix_conflict)\n", start)
        assert start != -1, "push_and_pr) arm not found in entrypoint"
        assert end != -1, "fix_conflict) arm not found after push_and_pr) arm"
        return text[start:end]

    def test_push_and_pr_arm_has_route_to_diagnoser(self) -> None:
        text = _script_text()
        block = self._push_and_pr_block(text)
        assert re.search(r"route_to_diagnoser\)", block), (
            "push_and_pr) case-arm must contain a route_to_diagnoser) arm (#3543)"
        )

    def test_push_and_pr_arm_has_no_inner_hint_case(self) -> None:
        """After #3558 the route_to_diagnoser arm must NOT contain an
        inner ``case "$_hint"`` sub-dispatch — all hints route uniformly
        to the same agent_runner_reaped_failure call."""
        text = _script_text()
        block = self._push_and_pr_block(text)
        assert not re.search(r'case "\$_hint"', block), (
            'push_and_pr) route_to_diagnoser arm must not contain an inner '
            'case "$_hint" sub-case after #3558 (uniform dispatch)'
        )
