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


class TestPushAndPrCaseArmRoutesViaCentralizedHelper:
    """The push_and_pr) case-arm must delegate to the centralized
    ``dispatch_transition_action`` helper (#3581), AND the helper must
    handle ``route_to_diagnoser`` + the ``push_and_pr_no_unmerged_files``
    hint (#3543).

    Originally (pre-#3581) these assertions checked that the per-phase
    case-statement INSIDE the push_and_pr) arm contained a
    ``route_to_diagnoser)`` sub-case + a ``push_and_pr_no_unmerged_files)``
    hint sub-case. After #3581 centralized dispatch, that logic moved
    into ``dispatch_transition_action`` — but the bug-class invariant
    is preserved: the action vocabulary AND the hint are handled
    SOMEWHERE the push_and_pr) arm reaches at runtime.
    """

    @staticmethod
    def _push_and_pr_block(text: str) -> str:
        """Return the substring between ``push_and_pr)`` and the next
        top-level ``fix_conflict)`` arm."""
        start = text.find("        push_and_pr)\n")
        end = text.find("        fix_conflict)\n", start)
        assert start != -1, "push_and_pr) arm not found in entrypoint"
        assert end != -1, "fix_conflict) arm not found after push_and_pr) arm"
        return text[start:end]

    @staticmethod
    def _dispatch_helper_body(text: str) -> str:
        """Return the body of the ``dispatch_transition_action`` helper
        function (the centralized dispatch site, #3581)."""
        start = text.find("dispatch_transition_action() {\n")
        assert start != -1, (
            "dispatch_transition_action helper not found in entrypoint (#3581)"
        )
        # The helper ends at the next top-level ``mark_ended() {`` or
        # ``# ── ...`` block — find the closing ``}`` at column 0 by
        # looking for the next bare-``}`` line that follows a non-
        # indented line.
        end_marker = text.find("\nmark_ended() {", start)
        assert end_marker != -1, (
            "Could not locate end of dispatch_transition_action helper"
        )
        return text[start:end_marker]

    def test_push_and_pr_arm_calls_dispatch_helper(self) -> None:
        text = _script_text()
        block = self._push_and_pr_block(text)
        # The centralized helper is the only correct dispatch site for
        # the push_and_pr arm post-#3581.
        assert "dispatch_transition_action" in block, (
            "push_and_pr) case-arm must call dispatch_transition_action "
            "(#3581 — centralized transition-action dispatch)"
        )
        # Helper invocation passes the phase name as the first arg so
        # log events + unrecognized-action terminals are correctly
        # tagged with ``push_and_pr``.
        assert re.search(
            r'dispatch_transition_action\s+"push_and_pr"',
            block,
        ), (
            "push_and_pr) case-arm must invoke dispatch_transition_action "
            'with phase="push_and_pr" as the first argument (#3581)'
        )

    def test_dispatch_helper_handles_route_to_diagnoser(self) -> None:
        """#3543 invariant — preserved post-#3581 in the centralized helper."""
        text = _script_text()
        body = self._dispatch_helper_body(text)
        assert re.search(r"route_to_diagnoser\)", body), (
            "dispatch_transition_action must contain a route_to_diagnoser) "
            "arm (#3543 invariant, centralized in #3581)"
        )

    def test_dispatch_helper_handles_no_unmerged_files_hint(self) -> None:
        """#3543 invariant — preserved post-#3581 in the centralized helper."""
        text = _script_text()
        body = self._dispatch_helper_body(text)
        assert "push_and_pr_no_unmerged_files" in body, (
            "dispatch_transition_action must handle the "
            "push_and_pr_no_unmerged_files hint (#3543 invariant, "
            "centralized in #3581)"
        )
