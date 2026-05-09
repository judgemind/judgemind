#!/usr/bin/env python3
# venv: none
# permanent: true
"""check-issue-verify-test-filename.py — Validate test filenames
referenced in ``Verify:`` lines of a GitHub issue body against the
``scripts/run-scripts-tests.sh::is_helper`` filter.

Why this check exists
---------------------
The shell-test runner ``scripts/run-scripts-tests.sh`` classifies any
``scripts/tests/_*.sh`` file as a sourceable helper (the ``is_helper``
function at lines ~100-111) and silently skips execution. When an
issue's ``Verify:`` line prescribes a test filename that matches the
helper filter (e.g. ``Verify: scripts/tests/_test-helpers-test.sh
exits 0``), the test is silently skipped by CI — the implementer
either has to deviate from the AC (rename to ``test__<thing>.sh`` to
keep CI coverage) or, worse, lands a "test" file that never actually
runs.

#4545 landed the docs warning in ``docs/agent/issue-authoring.md``
to address this for human authors at the docs layer. This script is
the automated half: parse the issue body's ``Verify:`` lines and
flag any path under ``scripts/tests/`` whose basename starts with
``_``. We also flag the historical pre-#4545 anti-pattern
``*-test.sh`` (any ``-test.sh`` suffix anywhere in the line — this
was the original buggy filename in #4540).

CLI
---
::

    scripts/check-issue-verify-test-filename.py --issue 4549      # fetch via gh, validate
    scripts/check-issue-verify-test-filename.py --body-file body.txt  # validate a local body

Exit codes
----------
* ``0`` — all ``Verify:`` lines reference test filenames the runner
  will discover (or no test filenames at all).
* ``1`` — at least one offending line (with a per-line diagnostic
  naming the bad path and the recommended ``test_<thing>.sh`` /
  ``test__<thing>.sh`` alternative).
* ``2`` — parse / ``gh`` error.

Out-of-scope
------------
* SQL-column validation (covered by ``check-issue-verify-sql.py``).
* Verify lines naming non-test files (e.g. ``Verify: grep -n ...
  scripts/tests/_helper.sh`` where the file is genuinely a helper —
  the recogniser is conservative and only flags patterns that look
  like "run this test").

Tracking: issue #4549 (parent: #4545, surfaced via #4540).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Match a Verify: line prefix. Tolerate `- Verify:`, `Verify:`, leading
# whitespace, and case variation.
_VERIFY_LINE_RE = re.compile(r"^\s*-?\s*Verify:", re.IGNORECASE)

# Match any path under scripts/tests/ whose basename starts with `_`.
# The basename pattern intentionally excludes `__` (test__<thing>.sh is
# the canonical double-underscore disambiguator) — but `_*.sh` always
# matches because `_` is a single underscore at start, and double-
# underscore filenames begin with `t` not `_`. So the `_*.sh` regex is
# all we need to detect helper-prefix filenames.
_HELPER_PREFIX_RE = re.compile(
    r"\bscripts/tests/(_[A-Za-z0-9_-]*\.sh)\b",
)

# Match the historical `*-test.sh` antipattern anywhere in the line.
# This is a separate signal: even outside scripts/tests/, an AC that
# names a `<thing>-test.sh` file is using the deprecated dash-suffix
# shape that #4540 / #4545 superseded with `test_<thing>.sh`.
_DASH_TEST_SUFFIX_RE = re.compile(
    r"\b([A-Za-z0-9_-]+-test\.sh)\b",
)


# ---------------------------------------------------------------------------
# Issue body extraction
# ---------------------------------------------------------------------------


def fetch_issue_body(issue_number: int) -> str:
    """Return the body text of the GitHub issue via ``gh issue view``."""
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                "judgemind/judgemind",
                "--json",
                "body",
                "-q",
                ".body",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gh CLI not installed") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise RuntimeError(
            f"gh issue view failed (exit {exc.returncode}): {stderr}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("gh issue view timed out after 30s") from exc
    return result.stdout


def extract_verify_lines(body: str) -> list[tuple[int, str]]:
    """Return a list of ``(lineno, line)`` for every ``Verify:`` line in
    ``body``. Lineno is 1-indexed.

    The recognizer collects only the line where ``Verify:`` first
    appears — we don't follow continuations, since the offending
    pattern (a test path) appears on the same line as the verb in
    every realistic AC shape.
    """
    out: list[tuple[int, str]] = []
    for i, line in enumerate(body.splitlines(), start=1):
        if _VERIFY_LINE_RE.match(line):
            out.append((i, line))
    return out


# ---------------------------------------------------------------------------
# Filename validation
# ---------------------------------------------------------------------------


def _suggest_replacement(bad: str) -> str:
    """Return the recommended replacement filename for ``bad``.

    For ``scripts/tests/_<thing>.sh``: suggest ``scripts/tests/test_<thing>.sh``
    (default) and ``scripts/tests/test__<thing>.sh`` (helper-test variant).
    For ``<thing>-test.sh`` antipattern: suggest ``test_<thing>.sh``.
    """
    base = bad.rsplit("/", 1)[-1]
    # Strip leading underscore(s) and trailing .sh.
    if base.startswith("_") and base.endswith(".sh"):
        stem = base[1:-3]
        # Convert dashes to underscores for the test_<thing>.sh form
        # (test names use underscore convention).
        stem_under = stem.replace("-", "_")
        return (
            f"scripts/tests/test_{stem_under}.sh "
            f"(default) or scripts/tests/test__{stem_under}.sh "
            f"(helper-test variant)"
        )
    if base.endswith("-test.sh"):
        stem = base[: -len("-test.sh")]
        stem_under = stem.replace("-", "_")
        return f"scripts/tests/test_{stem_under}.sh"
    # Fallback (should not happen given the regexes above).
    return f"a basename starting with `test_` (e.g. test_{base})"


def find_violations(line: str) -> list[tuple[str, str]]:
    """Return ``[(bad_filename, suggested_replacement), ...]`` for any
    helper-prefix or dash-test-suffix pattern in ``line``.

    Both regexes can fire on the same line — for example
    ``Verify: scripts/tests/_foo-test.sh`` matches both. We return a
    deduplicated list keyed by ``bad_filename``.
    """
    seen: set[str] = set()
    violations: list[tuple[str, str]] = []

    for m in _HELPER_PREFIX_RE.finditer(line):
        bad = f"scripts/tests/{m.group(1)}"
        if bad in seen:
            continue
        seen.add(bad)
        violations.append((bad, _suggest_replacement(bad)))

    for m in _DASH_TEST_SUFFIX_RE.finditer(line):
        # Skip if already flagged via the helper-prefix path (e.g.
        # `_foo-test.sh` matches both regexes — surface the
        # helper-prefix diagnostic, not the dash-suffix one).
        full_match = m.group(0)
        if any(full_match in s for s in seen):
            continue
        seen.add(full_match)
        violations.append((full_match, _suggest_replacement(full_match)))

    return violations


# ---------------------------------------------------------------------------
# Top-level: tie it together
# ---------------------------------------------------------------------------


def check_body(body: str) -> list[str]:
    """Return a list of human-readable error strings, one per offending
    ``Verify:`` line. Empty list means clean."""
    errors: list[str] = []
    for lineno, line in extract_verify_lines(body):
        violations = find_violations(line)
        for bad, suggestion in violations:
            errors.append(
                f"  line {lineno}: {bad} would be silently skipped by "
                f"scripts/run-scripts-tests.sh::is_helper.\n"
                f"    Verify line: {line.strip()}\n"
                f"    Use instead: {suggestion}"
            )
    return errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate test filenames referenced in Verify: lines of a "
            "GitHub issue body against scripts/run-scripts-tests.sh's "
            "is_helper filter."
        )
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--issue",
        type=int,
        help="GitHub issue number (fetched via gh issue view).",
    )
    src.add_argument(
        "--body-file",
        type=Path,
        help="Path to a local file containing the issue body.",
    )
    args = parser.parse_args(argv)

    # Load issue body.
    if args.issue is not None:
        try:
            body = fetch_issue_body(args.issue)
        except RuntimeError as exc:
            sys.stderr.write(f"ERROR: {exc}\n")
            return 2
        source_label = f"issue #{args.issue}"
    else:
        try:
            body = args.body_file.read_text()
        except OSError as exc:
            sys.stderr.write(
                f"ERROR: failed to read body file {args.body_file}: {exc}\n"
            )
            return 2
        source_label = str(args.body_file)

    errors = check_body(body)
    if errors:
        sys.stderr.write(f"ERROR: test-filename violations in {source_label}:\n\n")
        for err in errors:
            sys.stderr.write(err + "\n")
        sys.stderr.write(
            "\nFix: rename the prescribed test filename to one of the canonical "
            "patterns:\n"
            "  - scripts/tests/test_<thing>.sh         (default)\n"
            "  - scripts/tests/test__<thing>.sh        (when the test exercises "
            "a shared _<thing>.sh helper)\n"
            "\n"
            "Background: scripts/run-scripts-tests.sh::is_helper (lines ~100-111) "
            "classifies any scripts/tests/_*.sh file as a sourceable helper and "
            "silently skips it — a test prescribed under that pattern would never "
            "execute in CI. The *-test.sh suffix is the historical pre-#4545 "
            "anti-pattern that surfaced via #4540. See "
            "docs/agent/issue-authoring.md §\"Don't prescribe `_`-prefixed test "
            'filenames under `scripts/tests/`" and the load-bearing filter at '
            "scripts/run-scripts-tests.sh::is_helper. Tracking: issue #4549.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
