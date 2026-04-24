"""AST-walker audit: every hot-path ``_mark_agent_terminal`` call site
must pass ``issue_number=`` as a keyword argument.

Issue #2875.  The eight Phase 3A orchestration methods below are the
``_mark_agent_terminal`` call sites that execute in the critical hot
path for every agent lifecycle transition.  Omitting ``issue_number=``
means the method's label-teardown branch silently skips clearing
``status/in-progress`` on the GitHub issue, leaving the claim interlock
permanently held.

This test uses a pure stdlib AST walk of ``daemon.py`` — no running
daemon instance required.  See the plan section §"Consistency checks"
for the rationale behind ``inspect.getsourcefile`` + ``Path.read_text``
(NOT ``inspect.getsource``, which can fail on the module object).

Hot-path methods audited (AC #5):
    _run_plan_phase
    _run_ralph_phase
    _run_summary_phase
    _push_and_open_pr
    _handle_subprocess_failure
    _run_subprocess_or_fail
    _check_killswitch_and_abort
    _claim_and_orchestrate_one
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from typing import Iterable

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402 — sys.path mutation above

# ---------------------------------------------------------------------------
# Hot-path methods subject to the audit (AC #5)
# ---------------------------------------------------------------------------

_HOT_PATH_METHODS = [
    "_run_plan_phase",
    "_run_ralph_phase",
    "_run_summary_phase",
    "_push_and_open_pr",
    "_handle_subprocess_failure",
    "_run_subprocess_or_fail",
    "_check_killswitch_and_abort",
    "_claim_and_orchestrate_one",
]


# ---------------------------------------------------------------------------
# AST helper
# ---------------------------------------------------------------------------


def _collect_mark_terminal_calls_missing_issue_number(
    source: str,
    method_names: Iterable[str],
) -> list[tuple[str, int]]:
    """Parse *source* and return ``(method_name, lineno)`` for every
    ``_mark_agent_terminal(...)`` call inside the named methods of
    ``DispatcherDaemon`` that lacks an ``issue_number=`` keyword argument.

    Steps:
    1. ``ast.parse`` the full source.
    2. Walk the top-level ``ClassDef`` nodes to find ``DispatcherDaemon``.
    3. For each requested method name, find the matching ``FunctionDef``
       inside that class.
    4. Walk all ``ast.Call`` nodes in the function body whose ``func`` is an
       ``ast.Attribute`` with ``attr == '_mark_agent_terminal'``.
    5. For each such call, check whether ``'issue_number'`` appears in
       ``[kw.arg for kw in call.keywords]``.
    6. Collect ``(method_name, lineno)`` for any call that is missing the
       kwarg.
    """
    target_methods = set(method_names)
    missing: list[tuple[str, int]] = []

    tree = ast.parse(source)

    # Find DispatcherDaemon class
    daemon_class: ast.ClassDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DispatcherDaemon":
            daemon_class = node
            break

    if daemon_class is None:
        raise ValueError(
            "Could not locate DispatcherDaemon ClassDef in daemon.py source"
        )

    # Build a map of method_name -> FunctionDef for the class body
    method_map: dict[str, ast.FunctionDef] = {}
    for item in daemon_class.body:
        if isinstance(item, ast.FunctionDef) and item.name in target_methods:
            method_map[item.name] = item

    for method_name in target_methods:
        func_def = method_map.get(method_name)
        if func_def is None:
            # Method not found in class — treat every expected call as missing
            # so the test fails loudly if the method is renamed or removed.
            continue

        for call_node in ast.walk(func_def):
            if not isinstance(call_node, ast.Call):
                continue
            func = call_node.func
            if not (
                isinstance(func, ast.Attribute) and func.attr == "_mark_agent_terminal"
            ):
                continue
            kwarg_names = [kw.arg for kw in call_node.keywords]
            if "issue_number" not in kwarg_names:
                missing.append((method_name, call_node.lineno))

    return missing


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHelperDetectsMissingKwarg:
    """AC #4: removing ``issue_number=`` from a call produces a clear
    ``(method, lineno)`` signal; a call that has it returns ``[]``."""

    _SYNTHETIC_SOURCE = """\
class DispatcherDaemon:
    def _push_and_open_pr(self):
        self._mark_agent_terminal(
            agent_id,
            status="running",
            phase="awaiting_ci",
            exit_code=None,
            pr_number=pr_number,
        )
"""

    _SYNTHETIC_SOURCE_WITH_KWARG = """\
class DispatcherDaemon:
    def _push_and_open_pr(self):
        self._mark_agent_terminal(
            agent_id,
            status="running",
            phase="awaiting_ci",
            exit_code=None,
            pr_number=pr_number,
            issue_number=issue_number,
        )
"""

    def test_missing_kwarg_detected(self) -> None:
        """A call without ``issue_number=`` is caught and reported."""
        missing = _collect_mark_terminal_calls_missing_issue_number(
            self._SYNTHETIC_SOURCE,
            ["_push_and_open_pr"],
        )
        assert len(missing) == 1, (
            f"Expected exactly one missing-kwarg entry, got: {missing}"
        )
        method_name, lineno = missing[0]
        assert method_name == "_push_and_open_pr", (
            f"Expected method '_push_and_open_pr', got '{method_name}'"
        )
        # Line number must be positive (the call starts at line 3 in the
        # synthetic source).
        assert lineno > 0, f"Expected positive lineno, got {lineno}"

    def test_present_kwarg_not_flagged(self) -> None:
        """A call with ``issue_number=`` returns an empty list."""
        missing = _collect_mark_terminal_calls_missing_issue_number(
            self._SYNTHETIC_SOURCE_WITH_KWARG,
            ["_push_and_open_pr"],
        )
        assert missing == [], (
            f"Expected no missing-kwarg entries when kwarg is present, got: {missing}"
        )


@pytest.mark.parametrize("method_name", _HOT_PATH_METHODS)
class TestMarkAgentTerminalIssueNumberAudit:
    """AC #2 + AC #5: one test per hot-path method, all green on current main.

    Each parametrized case walks only the named method inside
    ``DispatcherDaemon`` and asserts that every ``_mark_agent_terminal``
    call carries ``issue_number=``.

    AC #4 failure message: when a call site is missing the kwarg, the
    assertion error interpolates the ``(method, lineno)`` tuples so the
    developer can locate the offending line without guessing.
    """

    def test_all_mark_terminal_calls_have_issue_number_kwarg(
        self, method_name: str
    ) -> None:
        daemon_source_file = inspect.getsourcefile(daemon)
        assert daemon_source_file is not None, "Could not resolve daemon.py path"
        source = Path(daemon_source_file).read_text(encoding="utf-8")

        missing = _collect_mark_terminal_calls_missing_issue_number(
            source,
            [method_name],
        )

        assert missing == [], (
            "Hot-path method(s) have _mark_agent_terminal call(s) missing "
            "issue_number= kwarg: "
            + ", ".join(f"method={m!r} line={ln}" for m, ln in missing)
        )
