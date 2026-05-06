#!/usr/bin/env python3
"""Forbid HTTP-status-naming test titles that lack a statusCode assertion.

A TypeScript API test whose title names an HTTP status (e.g.
``it('returns 400 for invalid UUID', ...)``) MUST assert on
``res.statusCode`` for that status. Otherwise the title gives false
confidence: the test can pass even when the handler returns a different
status (notably 500), which is exactly how #4129 slipped through
(see PR #4218 for the fix).

Recognised title patterns: ``HTTP <NNN>``, ``returns <NNN>`` /
``return <NNN>``, ``responds with <NNN>`` / ``respond with <NNN>``,
``status <NNN>`` / ``statusCode <NNN>``. <NNN> is any 3-digit number.

Recognised assertion patterns: any line in the same ``it()``/``test()``
block where ``statusCode`` and the named status number co-occur (covers
``expect(res.statusCode).toBe(NNN)``, ``assert.equal(res.statusCode, NNN)``,
and equivalents across vitest/jest/node:assert).

Escape hatch: append ``// status-assertion-noqa`` to the ``it()`` or
``test()`` opening line to acknowledge a deliberate omission.

Usage:
    scripts/check-test-statuscode-assertions.py [PATH ...]
    scripts/check-test-statuscode-assertions.py --selftest

With no arguments, scans ``packages/api/tests`` and ``packages/api/src``.
Exit 0 = no violations, 1 = one or more violations.

Ref: https://github.com/judgemind/judgemind/issues/4220
"""
# permanent: true

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

# Default directories to scan when no paths are supplied.
_DEFAULT_DIRS: list[str] = [
    "packages/api/tests",
    "packages/api/src",
]

# Title pattern: matches an HTTP status reference inside the title string
# of an ``it()`` / ``test()`` / ``it.each()`` / ``test.each()`` call. The
# title is captured by ``_TEST_OPEN_RE``; the status numbers are pulled
# out by ``_STATUS_IN_TITLE_RE``.
_STATUS_IN_TITLE_RE = re.compile(
    r"""(?ix)
        \b
        (?:
            HTTP\s+(\d{3})                     # "HTTP 400"
          | (?:returns?|responds?\s+with)      # "returns 400" / "responds with 400"
            \s+ (\d{3})
          | status(?:Code)?\s+(\d{3})          # "status 400" / "statusCode 400"
        )
        \b
    """,
)

# Test-opening line pattern: matches ``it('title', ...)``, ``test('title', ...)``,
# ``it.each(...)('title', ...)``, ``test.each(...)('title', ...)``,
# ``it.skip('title')``, ``it.only('title')``, etc. The title is the first
# string-literal argument; we capture it loosely (between the same quote
# character it opened with), tolerating template literals and nested
# escapes via a non-greedy match.
_TEST_OPEN_RE = re.compile(
    r"""(?x)
        ^(?P<indent>\s*)
        (?P<func>
            (?:it|test)
            (?:\.(?:skip|only|each|todo|concurrent|sequential))?
            (?:\([^)]*\))?            # optional ``.each(table)`` arg
        )
        \(\s*
        (?P<quote>['"`])
        (?P<title>(?:\\.|(?!(?P=quote)).)*)
        (?P=quote)
    """,
)

# Escape-hatch marker — must appear on the same source line as the
# ``it()`` / ``test()`` opening call.
_NOQA_MARKER = "// status-assertion-noqa"

# Assertion patterns inside a test body. We accept any line where a
# ``statusCode`` reference and the expected status number appear together,
# regardless of which assertion library style is in use.
def _assertion_for_status(status: str) -> re.Pattern[str]:
    # Look for ``statusCode`` (case-insensitive, allowing ``status_code``
    # too even though TS convention is camelCase) plus the matching
    # 3-digit number on the same line. The ``status`` argument is one
    # specific number; we anchor on it to avoid false positives where a
    # test asserts on a *different* status code than the one named in
    # the title.
    return re.compile(
        rf"""
        statusCode
        .*?
        \b{re.escape(status)}\b
        |
        \b{re.escape(status)}\b
        .*?
        statusCode
        """,
        re.VERBOSE,
    )


def _find_block_end(source_lines: list[str], open_lineno_idx: int) -> int:
    """Find the line index where the test body ``})`` closes.

    ``open_lineno_idx`` is the 0-indexed line containing the opening
    ``it(`` or ``test(`` call. We walk forward, counting brace + paren
    depth across all subsequent lines, and return the index of the line
    where depth returns to its original value (or to zero, whichever
    comes first). String literals and template literals are skipped so
    their internal braces don't fool the counter.

    Falls back to the last line of the file if the structure is malformed
    — better to scan the whole tail than to silently miss a violation.
    """
    depth_paren = 0
    depth_brace = 0
    started = False
    in_string: str | None = None  # quote char if inside a string literal
    in_block_comment = False
    in_line_comment = False
    template_brace_depth = 0  # tracks ${...} nesting inside template literals

    for idx in range(open_lineno_idx, len(source_lines)):
        line = source_lines[idx]
        in_line_comment = False
        i = 0
        while i < len(line):
            ch = line[i]
            nxt = line[i + 1] if i + 1 < len(line) else ""

            if in_line_comment:
                break  # rest of line is comment; skip

            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue

            if in_string is not None:
                if ch == "\\":
                    # Skip escaped char.
                    i += 2
                    continue
                if in_string == "`" and ch == "$" and nxt == "{":
                    template_brace_depth += 1
                    in_string = None  # leave template literal mode
                    i += 2
                    continue
                if ch == in_string:
                    in_string = None
                    i += 1
                    continue
                i += 1
                continue

            if ch == "/" and nxt == "/":
                in_line_comment = True
                break
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue
            if ch in ("'", '"', "`"):
                in_string = ch
                i += 1
                continue
            if ch == "(":
                depth_paren += 1
                started = True
            elif ch == ")":
                depth_paren -= 1
            elif ch == "{":
                if template_brace_depth > 0:
                    template_brace_depth += 1
                else:
                    depth_brace += 1
                    started = True
            elif ch == "}":
                if template_brace_depth > 0:
                    template_brace_depth -= 1
                    if template_brace_depth == 0:
                        # Re-enter template-literal string mode (we know
                        # we left from one because template_brace_depth
                        # was incremented from a `).
                        in_string = "`"
                else:
                    depth_brace -= 1
            i += 1

        if started and depth_paren <= 0 and depth_brace <= 0:
            return idx

    return len(source_lines) - 1


def find_violations(filepath: Path) -> list[tuple[int, str, str]]:
    """Return ``(lineno, status, snippet)`` for each violation in the file.

    A violation is a test whose title names HTTP status ``status`` but
    whose body does not assert on ``statusCode`` for that status (and
    does not carry the ``// status-assertion-noqa`` escape hatch on the
    opening line).
    """
    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    source_lines = source.splitlines()
    violations: list[tuple[int, str, str]] = []

    for idx, line in enumerate(source_lines):
        m = _TEST_OPEN_RE.match(line)
        if not m:
            continue

        title = m.group("title")
        # Pull every status number named in the title. A title can name
        # more than one ("returns 400 or 401"); we require an assertion
        # for each.
        statuses_in_title: list[str] = []
        for sm in _STATUS_IN_TITLE_RE.finditer(title):
            for grp in sm.groups():
                if grp is not None:
                    statuses_in_title.append(grp)
                    break
        if not statuses_in_title:
            continue

        # Escape hatch: same-line noqa marker.
        if _NOQA_MARKER in line:
            continue

        end_idx = _find_block_end(source_lines, idx)
        body_text = "\n".join(source_lines[idx : end_idx + 1])

        for status in statuses_in_title:
            if not _assertion_for_status(status).search(body_text):
                snippet = line.strip()
                violations.append((idx + 1, status, snippet))

    return violations


def iter_test_files(paths: list[Path]) -> list[Path]:
    """Yield ``*.test.ts`` / ``*.test.tsx`` files under each path.

    A file passed directly is included if its name matches the test
    pattern; directories are walked recursively.
    """
    suffixes = (".test.ts", ".test.tsx", ".test.mts")
    files: list[Path] = []
    for root in paths:
        if not root.exists():
            continue
        if root.is_file():
            if root.name.endswith(suffixes):
                files.append(root)
            continue
        for candidate in root.rglob("*"):
            if candidate.is_file() and candidate.name.endswith(suffixes):
                files.append(candidate)
    return files


def _selftest() -> int:
    """Run a self-test against synthetic fixture content.

    Asserts:
      * A fixture with a missing ``statusCode`` assertion produces a
        violation.
      * A fixture with the assertion present produces no violation.
      * The escape hatch (``// status-assertion-noqa``) suppresses the
        violation.
      * A title that does NOT name an HTTP status produces no violation
        even when the body has no statusCode assertion.

    Exits 0 on success, 1 on failure.
    """
    bad_fixture = """\
import { describe, it, expect } from 'vitest';

describe('handler', () => {
  it('returns 400 for invalid UUID', async () => {
    const res = await app.inject({ url: '/foo' });
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
  });
});
"""

    good_fixture = """\
import { describe, it, expect } from 'vitest';

describe('handler', () => {
  it('returns 400 for invalid UUID', async () => {
    const res = await app.inject({ url: '/foo' });
    expect(res.statusCode).toBe(400);
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
  });
});
"""

    noqa_fixture = """\
import { describe, it, expect } from 'vitest';

describe('handler', () => {
  it('returns 400 for invalid UUID', async () => { // status-assertion-noqa
    // Asserts on the thrown error, not the inject response.
    await expect(callDirectly()).rejects.toThrow(/bad uuid/);
  });
});
"""

    not_a_status_fixture = """\
import { describe, it, expect } from 'vitest';

describe('handler', () => {
  it('rejects gracefully', async () => {
    const res = await app.inject({ url: '/foo' });
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
  });
});
"""

    multi_status_fixture = """\
import { describe, it, expect } from 'vitest';

describe('handler', () => {
  it('returns 400 for invalid UUID and HTTP 401 for missing auth', async () => {
    const res = await app.inject({ url: '/foo' });
    expect(res.statusCode).toBe(400);
    // 401 not asserted; this is a violation for status=401 only.
    const body = JSON.parse(res.body);
    expect(body.errors).toBeDefined();
  });
});
"""

    cases: list[tuple[str, str, list[str]]] = [
        ("bad", bad_fixture, ["400"]),
        ("good", good_fixture, []),
        ("noqa", noqa_fixture, []),
        ("not_a_status", not_a_status_fixture, []),
        ("multi_status", multi_status_fixture, ["401"]),
    ]

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for label, content, expected_violations in cases:
            fp = Path(tmpdir) / f"{label}.test.ts"
            fp.write_text(content, encoding="utf-8")
            got = find_violations(fp)
            got_statuses = sorted(status for _, status, _ in got)
            want_statuses = sorted(expected_violations)
            if got_statuses != want_statuses:
                failures.append(
                    f"{label}: expected violations {want_statuses}, got {got_statuses} (raw={got})"
                )

    if failures:
        print("SELFTEST FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("SELFTEST OK: all 5 fixture cases produced expected violation lists.")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--selftest":
        return _selftest()

    if argv:
        paths = [Path(p) for p in argv]
    else:
        paths = [Path(p) for p in _DEFAULT_DIRS]

    files = iter_test_files(paths)
    violations: list[tuple[Path, int, str, str]] = []
    for fp in files:
        for lineno, status, snippet in find_violations(fp):
            violations.append((fp, lineno, status, snippet))

    if not violations:
        return 0

    print(
        "ERROR: Test title names HTTP status but body lacks a `statusCode` assertion:",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for fp, lineno, status, snippet in violations:
        print(f"  {fp}:{lineno}: missing assert on statusCode={status}", file=sys.stderr)
        print(f"      {snippet}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "A test title that names an HTTP status code without a corresponding",
        file=sys.stderr,
    )
    print(
        "`expect(res.statusCode).toBe(<NNN>)` assertion gives false confidence",
        file=sys.stderr,
    )
    print(
        "— the bug it claims to catch can slip through (see #4129 / #4218 / #4220).",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(
        "Either add the assertion, or — if the test legitimately doesn't go",
        file=sys.stderr,
    )
    print(
        f"through an HTTP boundary — append `{_NOQA_MARKER}` to the same line",
        file=sys.stderr,
    )
    print("as the `it()` / `test()` opening call.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
