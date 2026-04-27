"""Broad AST guard for duplicate event= kwargs in top-level data scripts (#3540).

Covers every scripts/backfill_*.py and scripts/cleanup_*.py.

structlog's bound logger maps the first positional argument to 'event='
automatically.  Passing both a positional arg and an explicit event= keyword
raises TypeError at runtime.  This guard catches the pattern statically via
ast.parse so it surfaces in CI without requiring live services.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOGGER_LEVELS = {"info", "warning", "error", "debug", "critical"}
_SCRIPT_GLOBS = ("scripts/backfill_*.py", "scripts/cleanup_*.py")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _walk_logger_violations(source: str, filename: str) -> list[tuple[str, int]]:
    """Return (filename, lineno) for each logger.<level> call that has both
    a positional arg and an explicit event= keyword argument.

    These are always bugs: structlog maps the first positional arg to event=
    automatically, so supplying both raises TypeError at runtime.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr in _LOGGER_LEVELS
            and isinstance(func.value, ast.Name)
            and func.value.id == "logger"
        ):
            continue
        has_positional = len(node.args) > 0
        has_event_kwarg = any(kw.arg == "event" for kw in node.keywords)
        if has_positional and has_event_kwarg:
            violations.append((filename, node.lineno))
    return violations


def _discover_scripts() -> list[Path]:
    """Return all scripts matching _SCRIPT_GLOBS, excluding scripts/archive/."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    paths: list[Path] = []
    for glob_pattern in _SCRIPT_GLOBS:
        for path in sorted(repo_root.glob(glob_pattern)):
            # Defensive filter: skip anything under scripts/archive/ in case
            # a glob pattern is broadened in the future.
            if "archive" in path.parts:
                continue
            paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Sanity check: discovery must be non-empty
# ---------------------------------------------------------------------------


def test_glob_finds_at_least_one_script() -> None:
    """_discover_scripts() must return at least one script.

    If this fails, the glob patterns in _SCRIPT_GLOBS no longer match any
    file and the parametrized guard below is silently a no-op.
    """
    scripts = _discover_scripts()
    assert len(scripts) >= 1, (
        f"_discover_scripts() returned no scripts — patterns {_SCRIPT_GLOBS} "
        "matched nothing under scripts/. Update _SCRIPT_GLOBS if the files moved."
    )


# ---------------------------------------------------------------------------
# Broad parametrized guard
# ---------------------------------------------------------------------------


class TestNoDuplicateEventKwargAcrossScripts:
    """AST guard that runs against every discovered data script."""

    @pytest.mark.parametrize("script_path", _discover_scripts(), ids=lambda p: p.name)
    def test_no_logger_call_has_both_positional_and_event_kwarg(
        self, script_path: Path
    ) -> None:
        source = script_path.read_text()
        repo_root = Path(__file__).resolve().parent.parent.parent
        rel = script_path.relative_to(repo_root)
        violations = _walk_logger_violations(source, str(rel))
        assert violations == [], (
            f"Duplicate event= kwarg found in {rel} at lines: "
            f"{[lineno for _, lineno in violations]}. "
            "Remove the explicit event= kwarg — structlog maps the first "
            "positional arg to event= automatically."
        )


# ---------------------------------------------------------------------------
# Self-check: verify the helper itself works correctly
# ---------------------------------------------------------------------------


class TestGuardSelfCheck:
    """Verify the AST walker detects violations and passes clean calls."""

    def test_helper_flags_known_violation(self) -> None:
        """Inline regression fixture: logger.info with both positional and event=."""
        source = """\
import structlog
logger = structlog.get_logger()
logger.info("x", event="y")
"""
        violations = _walk_logger_violations(source, "fixture.py")
        assert len(violations) == 1, (
            f"Expected exactly one violation, got: {violations}"
        )
        assert violations[0] == ("fixture.py", 3)

    def test_helper_passes_clean_call(self) -> None:
        """A clean logger.info call with only a positional arg must pass."""
        source = """\
import structlog
logger = structlog.get_logger()
logger.info("x")
"""
        violations = _walk_logger_violations(source, "fixture.py")
        assert violations == []
