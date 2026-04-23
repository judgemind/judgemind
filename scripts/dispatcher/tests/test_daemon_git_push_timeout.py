"""Unit tests for the daemon-side ``git push`` subprocess timeout (#2882).

The dispatcher daemon fires ``git push`` in two places:

1. ``_advance_awaiting_summary`` — the post-ralph/post-summary push
   that lands the agent's branch before ``gh pr create``.
2. ``_advance_awaiting_ci`` — the fix-ci retry push after the ``fix-ci``
   phase rewrites files in response to a red CI run.

Both previously used ``timeout=120``, which was not enough to cover
the pre-push hook's package test runs. On 2026-04-19 two overnight
agents lost ralph's work when the 120s ceiling tripped mid-hook.
600s was still too tight: on 2026-04-21 agent c3a69458 (#2564) timed
out at 600s with the hook still running the 7200-test scraper-
framework suite on 4 vCPU Fargate. The fix (this change) extracts
the timeout to a module-level ``GIT_PUSH_TIMEOUT_SECONDS`` constant
set to 1800s (30 min), aligned with the ``push_and_pr`` stuck-
timeout fallback.

These tests guard against regressing back to a hardcoded value by
walking the daemon module's AST and asserting every ``git push``
subprocess invocation passes the named constant as its ``timeout``
keyword argument.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

if "psycopg" not in sys.modules:  # pragma: no cover — fresh-venv only
    sys.modules["psycopg"] = MagicMock()

from dispatcher import daemon  # noqa: E402  — sys.path mutation above


_DAEMON_PATH = Path(daemon.__file__)


def test_git_push_timeout_constant_is_1800_seconds() -> None:
    """The module-level ``GIT_PUSH_TIMEOUT_SECONDS`` constant is 1800s.

    Guards against a well-meaning "tighten this back down" edit that
    would reintroduce the 120s or 600s failure modes. If you need to
    change this value, update the call-site tests below AND the
    docstring explaining the rationale.
    """
    assert hasattr(daemon, "GIT_PUSH_TIMEOUT_SECONDS"), (
        "daemon module must define GIT_PUSH_TIMEOUT_SECONDS (#2882)"
    )
    assert daemon.GIT_PUSH_TIMEOUT_SECONDS == 1800, (
        f"GIT_PUSH_TIMEOUT_SECONDS must be 1800; got {daemon.GIT_PUSH_TIMEOUT_SECONDS}"
    )


def _list_literals_have_git_push(cmd_arg: ast.expr) -> bool:
    """Return True if ``cmd_arg`` is a list literal with ``git`` + ``push``.

    Accepts both:
      - direct list literal passed to subprocess.run
      - list literal bound to a local name then passed to either
        subprocess.run or self._subprocess_with_retry.
    """
    if not isinstance(cmd_arg, ast.List):
        return False
    literals: list[str] = []
    for elt in cmd_arg.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            literals.append(elt.value)
        else:
            literals.append("")
    return bool(literals) and literals[0] == "git" and "push" in literals


def _resolve_name_to_list(name: str, tree: ast.Module, call_line: int) -> ast.List | None:
    """Find the nearest preceding ``<name> = [...]`` assignment before call_line."""
    best: ast.List | None = None
    best_line = -1
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.lineno < call_line:
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == name
                    and isinstance(node.value, ast.List)
                    and node.lineno > best_line
                ):
                    best = node.value
                    best_line = node.lineno
    return best


def _collect_subprocess_run_push_calls() -> list[ast.Call]:
    """Return every git-push subprocess call in daemon.py.

    Matches both pre-#3089 direct ``subprocess.run([... 'git' ... 'push' ...])``
    and post-#3089 ``self._subprocess_with_retry([... 'git' ... 'push' ...])``
    (which wraps ``subprocess.run`` with retry). Also resolves the
    ``cmd = [...]; self._subprocess_with_retry(cmd, ...)`` name-binding
    pattern.
    """
    tree = ast.parse(_DAEMON_PATH.read_text())
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_subprocess_run = (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )
        is_retry_helper = (
            isinstance(func, ast.Attribute)
            and func.attr == "_subprocess_with_retry"
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        )
        if not (is_subprocess_run or is_retry_helper):
            continue
        if not node.args:
            continue
        cmd_arg = node.args[0]
        # Inline list literal.
        if _list_literals_have_git_push(cmd_arg):
            out.append(node)
            continue
        # ``cmd = [...]; self._subprocess_with_retry(cmd, ...)`` pattern.
        if isinstance(cmd_arg, ast.Name):
            resolved = _resolve_name_to_list(cmd_arg.id, tree, node.lineno)
            if resolved is not None and _list_literals_have_git_push(resolved):
                out.append(node)
    return out


def _timeout_kwarg(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "timeout":
            return kw.value
    return None


def test_git_push_call_sites_use_the_constant() -> None:
    """Both ``git push`` subprocess.run sites use GIT_PUSH_TIMEOUT_SECONDS.

    Scans daemon.py's AST for every ``subprocess.run([... 'git' ... 'push' ...])``
    call and asserts the ``timeout`` kwarg is the ``Name``
    ``GIT_PUSH_TIMEOUT_SECONDS`` — not a hardcoded integer. Regression
    guard for #2882: the old 120s ceiling tripped pre-push hooks on
    multi-package changes and lost ralph's work entirely.
    """
    push_calls = _collect_subprocess_run_push_calls()
    assert len(push_calls) >= 2, (
        f"expected at least 2 git-push subprocess.run sites in daemon.py, "
        f"found {len(push_calls)} — have the call sites moved? Update this "
        f"test alongside any legitimate refactor."
    )
    for call in push_calls:
        timeout_node = _timeout_kwarg(call)
        assert timeout_node is not None, (
            f"git push subprocess.run at line {call.lineno} is missing a "
            f"``timeout=`` kwarg — every daemon subprocess must have one."
        )
        assert isinstance(timeout_node, ast.Name), (
            f"git push subprocess.run at line {call.lineno} passes a "
            f"non-name timeout ({ast.dump(timeout_node)}); must use the "
            f"module-level GIT_PUSH_TIMEOUT_SECONDS constant so future "
            f"tuning is a 1-line change (#2882)."
        )
        assert timeout_node.id == "GIT_PUSH_TIMEOUT_SECONDS", (
            f"git push subprocess.run at line {call.lineno} passes "
            f"timeout={timeout_node.id}; must be GIT_PUSH_TIMEOUT_SECONDS "
            f"(#2882)."
        )


def test_daemon_logs_git_push_timeout_event_distinctly() -> None:
    """Timeout is logged as ``git_push_timeout`` / ``fix_ci_git_push_timeout``.

    Separating the timeout event name from the generic push_failed event
    gives operators immediate signal on what went wrong (agent ran too
    long vs git rejected the push) without grepping stderr tails. Guard
    against a future refactor that folds the ``except
    subprocess.TimeoutExpired`` branch back into the generic handler.
    """
    source = _DAEMON_PATH.read_text()
    assert '"event": "git_push_timeout"' in source, (
        "post-summary git push handler must log a distinct "
        "``git_push_timeout`` structured event when subprocess.TimeoutExpired "
        "is raised (#2882)."
    )
    assert '"event": "fix_ci_git_push_timeout"' in source, (
        "fix-ci git push handler must log a distinct "
        "``fix_ci_git_push_timeout`` structured event when "
        "subprocess.TimeoutExpired is raised (#2882)."
    )
