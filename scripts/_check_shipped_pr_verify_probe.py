#!/usr/bin/env python3
# _check_shipped_pr_verify_probe.py — Content-channel "shipped" probe for
# scripts/check-shipped-pr.sh.
#
# venv: none
# permanent: true
#
# Purpose (issue #4472):
#   The path-overlap heuristic in check-shipped-pr.sh fires when an issue
#   body cites the same file paths a merged PR touched. That works when
#   the AC author guesses the right filename, but breaks for the canonical
#   pre-#3994 zombie shape where the AC names a *test-content invariant*
#   (e.g. "frozenset ALLOWED_CLAUDE_FLAGS containing -p, --max-turns,
#   --model") and the actual PR uses a different filename than the issue
#   body's prose. The path channel returns zero overlap, so the issue
#   stays `agent/ready` indefinitely.
#
#   Concrete case: issue #2828 ↔ PR #3215. Issue body cited
#   ``scripts/dispatcher/tests/test_daemon_phase3a.py`` as a *guess*; the
#   PR landed at ``test_daemon_phase_argv_allowlist.py``. Path-overlap
#   sees zero hits. But the AC's literal Verify clause —
#   ``grep "ALLOWED_CLAUDE_FLAGS" scripts/dispatcher/tests/`` — matches
#   on origin/main today. A second-axis content probe that runs the AC's
#   Verify line catches this case in <1s, independent of filename.
#
# Reads `gh issue view --json body,title` JSON on stdin. Emits one of:
#
#   - On match: a single line ``shipped:<pr-number>\t<clause>`` to stdout
#     and exit 0. ``<clause>`` is the canonicalized Verify command that
#     fired (for diagnostic logs / JSON summary).
#   - On no match: empty stdout and exit 1. Caller falls through to the
#     path-overlap channel.
#   - On error / malformed input: empty stdout and exit 2.
#
# Supported Verify clause shapes (issue #4472 Proposal):
#
#   1. ``Verify: grep <pattern> <path>``
#      → run grep against the worktree, ≥1 match → shipped. Resolve PR
#        via ``git log -S <pattern> -- <path>`` (most-recent matching
#        squash-merge). Read-only; safe to execute.
#
#   2. ``Verify: pytest -k <test_name>`` or ``Verify: pytest <args> -k <test_name>``
#      → run ``pytest --collect-only -q -k <test_name>`` against the
#        worktree, ≥1 collected test → shipped. The ``--collect-only``
#        rewrite is enforced regardless of how the AC author wrote it,
#        so test side-effects (DB writes, network calls, file mutations)
#        are never triggered by a probe. Resolve PR via
#        ``git log -S <test_name>`` against the test files identified
#        by collection.
#
# DELIBERATELY UNSUPPORTED — script-execution clauses
# (``Verify: ./scripts/foo.sh ...`` / ``Verify: bash scripts/foo.sh``):
#   The issue's original proposal called for a third shape — "run the
#   probe and check exit-0". Two reasons we don't:
#     - **Safety.** Many scripts in this repo carry side effects (gh
#       writes, DB writes, AWS calls, secret reads). The probe runs
#       autonomously inside check-shipped-pr.sh; arbitrary script
#       execution is not appropriate.
#     - **Precision.** A pure-existence check ("the script is present
#       in the worktree") is too weak a signal. Most issues that cite a
#       script in a Verify clause are either (a) extending an EXISTING
#       script with new behavior — exactly the audit/extension shape
#       the script-existence channel would false-positive on, since
#       the script existed all along; or (b) asking for a new script
#       that the path-overlap channel already catches via
#       ``changeType: ADDED`` overlap on the script path.
#   We considered (and rejected) a "freshly added" filter — only fire
#   when the script's introducing commit post-dates the issue's
#   ``createdAt``. That's exactly what the path-overlap channel already
#   computes; folding it into the verify-channel as a second
#   implementation would not add precision over the existing path. The
#   verify-channel's purpose is to catch cases the path channel CAN'T
#   catch (filename guesses that don't match the actual PR's filename),
#   not to re-implement what the path channel already does.
#
# Safety contract:
#   - Only ``grep`` and ``pytest --collect-only`` are executed. No other
#     verb is invoked.
#   - All execution paths take a wall-clock timeout (default 30s per
#     subprocess) so a runaway grep / pytest cannot wedge the wrapper.
#   - The helper never writes to disk, never calls gh, never touches the
#     network outside of what pytest's collection step already does
#     (which is bounded by the repo's pytest config).
#   - Issue body content that doesn't match one of the two supported
#     shapes is silently ignored — the helper falls through to exit 1
#     and the path-overlap channel takes over.
#
# Why this is its own helper rather than inline shell:
#   - Multi-line regex parsing of Verify clauses is awkward in bash.
#   - The ``git log -S`` PR-resolution step needs careful argument
#     escaping that is less error-prone in Python than in bash heredocs.
#   - The classifier is unit-testable in isolation (see
#     ``scripts/tests/test_check_shipped_pr_verify_probe.py``).
#
# Environment variables (all optional, for testing hooks):
#   CHECK_SHIPPED_VERIFY_REPO_ROOT — repo root to run probes against
#       (defaults to ``git rev-parse --show-toplevel`` from the helper's
#       cwd when the env var is unset). Tests pass a fixture root.
#   CHECK_SHIPPED_VERIFY_TIMEOUT_SEC — per-subprocess timeout in seconds
#       (default 30). Tests pin this lower for fast failures.

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# ─── Verify clause extraction ──────────────────────────────────────────────

# A Verify clause is a line that starts with ``Verify:`` (optionally
# preceded by markdown bullet markers, indentation, or a bold-emphasis
# wrapper ``**Verify:**``). The capture group grabs everything AFTER the
# colon (with the leading whitespace stripped). Empirically the AC
# authors use these forms:
#
#   - ``- Verify: grep foo bar/``                        # bullet
#   - ``  Verify: pytest -k test_x``                     # indented continuation
#   - ``  - **Verify:** `grep foo bar/```                # bold + backticks
#   - ``Verify: ./scripts/probe.sh``                     # bare line
#
# The leading bullet/whitespace and the optional ``**`` bold wrapper are
# both consumed by the regex prefix. A trailing ``**`` from the bold
# closer is stripped from the captured remainder before parsing.
VERIFY_LINE_RE = re.compile(
    r"^[\s>*\-]*(?:\*\*)?Verify:?\*?\*?\s*(.*?)\s*$",
    re.IGNORECASE,
)

# A clause MAY embed a backtick-wrapped command (``Verify: `grep foo
# bar` returns a match``). When the FIRST backtick appears at column 0
# of the post-Verify text, the convention is "the load-bearing command
# is wrapped in backticks; trailing prose is informational." We extract
# the first backtick-balanced span as the actual command. This is a
# non-greedy match — ``Verify: `grep x y` returns a match; adding `--z`
# breaks it`` returns ``grep x y``, NOT ``grep x y` returns a match;
# adding `--z`` (which the greedy version would emit).
INLINE_BACKTICK_RE = re.compile(r"^`([^`]+)`")


def _extract_verify_clauses(body: str) -> list[str]:
    """Yield raw Verify clause command strings from ``body``.

    Each returned string is the post-``Verify:`` portion of the line,
    with leading/trailing whitespace and surrounding backticks stripped.
    Bullet markers, bold-emphasis wrappers, and indentation are consumed
    by ``VERIFY_LINE_RE``; only the actual command remains.

    Multi-clause lines (rare — most AC authors use one clause per line)
    are not split here. The grep/pytest/script classifiers each parse
    their own clause shape, so a clause that only contains prose
    ("Verify: reviewer confirms on read-through") simply fails all three
    classifiers and is silently skipped.

    Note on the bold-wrapped form (``**Verify:**``): the regex is
    intentionally lax about the trailing ``**`` because the line shape
    varies — the closer can appear immediately after the colon
    (``**Verify:**`` followed by a space and the command) or be absent
    entirely (a bullet that opens with ``**Verify:`` and forgets the
    closer). The regex consumes both forms by making the trailing
    ``**`` optional.
    """
    out: list[str] = []
    for line in body.splitlines():
        m = VERIFY_LINE_RE.match(line)
        if not m:
            continue
        raw = m.group(1).strip()
        if not raw:
            continue
        # Strip a trailing ``**`` if the bold closer landed at end-of-line.
        if raw.endswith("**"):
            raw = raw[:-2].rstrip()
        # Strip a leading ``**`` if the bold opener wrapped the value
        # (rare but observed: ``Verify: **grep foo bar**``).
        if raw.startswith("**"):
            raw = raw[2:].lstrip()
        # If the clause is wrapped in single backticks, unwrap it. Keep
        # any post-backtick prose appended (e.g. ``Verify: `grep x y`
        # returns a match`` — the ``returns a match`` is informational
        # narrative, not part of the command).
        bt = INLINE_BACKTICK_RE.match(raw)
        if bt:
            raw = bt.group(1).strip()
        if raw:
            out.append(raw)
    return out


# ─── Clause classifiers ────────────────────────────────────────────────────

# A grep clause is the canonical shape ``grep [flags] <pattern> <path>``.
# We accept any flags between ``grep`` and the pattern, and the pattern
# may be quoted (single or double) or bare. The path is everything after
# the pattern up to end-of-string.
GREP_CLAUSE_RE = re.compile(
    r"^grep\s+",
    re.IGNORECASE,
)

# A pytest clause starts with ``pytest`` (or ``python -m pytest``) and
# names a test selector via ``-k <expr>`` somewhere in its arguments.
# We only execute the ``--collect-only`` rewrite — the AC author may have
# written a full ``pytest -k ...`` invocation, but we never actually run
# the tests. The ``-k <expr>`` is the load-bearing identifier we resolve
# back to a PR via ``git log -S``.
PYTEST_CLAUSE_RE = re.compile(
    r"^(?:python(?:3)?\s+-m\s+)?pytest\s+",
    re.IGNORECASE,
)


def _classify_clause(clause: str) -> tuple[str, list[str]] | None:
    """Classify ``clause`` and return (shape, argv) or None on no-match.

    ``shape`` is one of ``"grep"`` or ``"pytest"``. ``argv`` is the
    post-``shlex.split`` token list for the matched verb (with the verb
    itself preserved as ``argv[0]`` so the executor can re-emit it).

    Script-execution clauses (``./scripts/foo.sh`` /
    ``bash scripts/foo.sh``) are intentionally NOT classified — see the
    "DELIBERATELY UNSUPPORTED" header at the top of this file. They
    fall through to None and the wrapper falls back to the path-overlap
    channel.
    """
    try:
        argv = shlex.split(clause)
    except ValueError:
        # Clause has unbalanced quotes — not parseable. Bail.
        return None
    if not argv:
        return None
    if GREP_CLAUSE_RE.match(clause):
        return ("grep", argv)
    if PYTEST_CLAUSE_RE.match(clause):
        return ("pytest", argv)
    return None


# ─── Probe executors ───────────────────────────────────────────────────────


def _run(
    cmd: list[str], *, cwd: Path, timeout_sec: int
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` synchronously, capturing stdout+stderr.

    Always returns a CompletedProcess (timeouts are caught and converted
    into a returncode=124 result with empty stdout). The helper never
    raises — every probe path is defensive against subprocess errors so
    a malformed clause cannot wedge check-shipped-pr.sh.
    """
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=124, stdout="", stderr="timeout"
        )
    except (FileNotFoundError, PermissionError, OSError) as e:
        return subprocess.CompletedProcess(
            args=cmd, returncode=126, stdout="", stderr=str(e)
        )


def _extract_grep_pattern(argv: list[str]) -> str | None:
    """Recover the grep pattern from ``argv``.

    grep argv shape: ``grep [flags...] <pattern> [path...]``. Flags start
    with ``-``; the first non-flag token is the pattern. We do NOT
    attempt to handle ``grep -e <pattern>`` here — that's vanishingly
    rare in AC clauses and the simple "first non-flag token" heuristic
    covers every observed case.
    """
    if not argv or argv[0].lower() != "grep":
        return None
    for tok in argv[1:]:
        if tok.startswith("-"):
            continue
        return tok
    return None


def _extract_pytest_k_expr(argv: list[str]) -> str | None:
    """Recover the ``-k <expr>`` selector from a pytest argv.

    Returns None when no ``-k`` appears (the clause is then unsupported —
    we don't probe by file path because that's already the path-overlap
    channel). Both ``-k expr`` (separate token) and ``-k=expr`` (joined)
    are supported, plus ``--keyword expr`` / ``--keyword=expr``.
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-k", "--keyword"):
            if i + 1 < len(argv):
                return argv[i + 1]
            return None
        if tok.startswith("-k="):
            return tok[3:]
        if tok.startswith("--keyword="):
            return tok[len("--keyword=") :]
        i += 1
    return None


def _resolve_pr_for_pattern(
    repo_root: Path, pattern: str, path: str | None, *, timeout_sec: int
) -> int | None:
    """Find the most-recent squash-merge PR that introduced ``pattern``.

    Uses ``git log -S <pattern>`` (the "pickaxe" search) to find commits
    where the count of occurrences of ``pattern`` changed. Optionally
    constrains to a path. Returns the PR number parsed from the most
    recent matching commit's subject line (the ``(#N)`` token), or None
    when no candidate is found.

    The pickaxe search is exact-string by default; we don't pass ``-G``
    (regex) because AC patterns are typically string literals (frozenset
    names, function names, magic strings).
    """
    cmd = ["git", "log", "-S", pattern, "--oneline", "--max-count=10"]
    if path:
        cmd.extend(["--", path])
    proc = _run(cmd, cwd=repo_root, timeout_sec=timeout_sec)
    if proc.returncode != 0:
        return None
    # Each line is ``<sha> <subject>``. Walk in newest-first order (git
    # log default) and return the FIRST line whose subject ends in
    # ``(#N)`` — that's the squash-merge PR.
    for line in proc.stdout.splitlines():
        # Match the LAST ``(#N)`` token on the subject — handles both
        # the conventional ``feat(x): foo (#1234)`` and the chained
        # ``fix(ci): squash (#2837) (#3170)`` shape (#4214 lesson —
        # always pick the trailing token, never the first).
        m = re.search(r"\(#(\d+)\)\s*$", line)
        if m:
            return int(m.group(1))
    return None


def _probe_grep(
    argv: list[str], *, repo_root: Path, timeout_sec: int
) -> tuple[int, str] | None:
    """Run a grep clause against ``repo_root`` and resolve the PR on match.

    Returns (pr_number, canonical_clause) on hit, None on miss.
    """
    pattern = _extract_grep_pattern(argv)
    if not pattern:
        return None
    # Reconstruct the path argument(s). After the pattern token, every
    # remaining non-flag token is a path argument.
    paths: list[str] = []
    seen_pattern = False
    for tok in argv[1:]:
        if tok.startswith("-"):
            continue
        if not seen_pattern:
            seen_pattern = True
            continue
        paths.append(tok)
    # Run grep against the worktree. The ``-r`` flag is added so a
    # directory path (like ``scripts/dispatcher/tests/``) recurses into
    # files; grep with a directory but no -r errors out on most BSD/GNU
    # implementations. ``-l`` (list-only) is added to keep output bounded.
    grep_cmd = ["grep", "-rln", pattern]
    if paths:
        grep_cmd.extend(paths)
    else:
        # No path → search the whole repo (matches the AC author's intent
        # when they wrote ``Verify: grep foo`` with no path argument).
        grep_cmd.append(".")
    proc = _run(grep_cmd, cwd=repo_root, timeout_sec=timeout_sec)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    # Found at least one match — resolve the introducing PR. Prefer the
    # first matching path as the PR-resolution scope (constrains the
    # pickaxe search to the relevant subtree). When multiple paths match,
    # pick the first one's parent directory as the scope.
    first_match_line = proc.stdout.strip().splitlines()[0]
    # ``grep -ln`` output: ``<path>:<linenum>:<text>`` — but ``-l`` mode
    # emits just ``<path>``. Combined with -n, the format is the full
    # ``<path>:<line>:<match>``. Take the path portion (everything before
    # the first colon) as the scope hint.
    scope_path = (
        first_match_line.split(":", 1)[0]
        if ":" in first_match_line
        else first_match_line
    )
    pr = _resolve_pr_for_pattern(
        repo_root, pattern, scope_path, timeout_sec=timeout_sec
    )
    if pr is None:
        # Fallback: pickaxe search without path constraint.
        pr = _resolve_pr_for_pattern(repo_root, pattern, None, timeout_sec=timeout_sec)
    if pr is None:
        return None
    canonical = f"grep {shlex.quote(pattern)} " + " ".join(
        shlex.quote(p) for p in paths
    )
    return (pr, canonical.rstrip())


def _probe_pytest(
    argv: list[str], *, repo_root: Path, timeout_sec: int
) -> tuple[int, str] | None:
    """Run a pytest clause in --collect-only mode against ``repo_root``.

    Returns (pr_number, canonical_clause) on collection-hit, None on miss.
    Side-effect-free: pytest collection imports test modules but does not
    run their bodies.
    """
    expr = _extract_pytest_k_expr(argv)
    if not expr:
        return None
    # Locate pytest. Prefer python -m pytest from the active interpreter
    # so we don't depend on a global ``pytest`` binary on PATH. The
    # active venv's pytest (if any) is what's on the helper's path.
    pytest_cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-k", expr]
    proc = _run(pytest_cmd, cwd=repo_root, timeout_sec=timeout_sec)
    # pytest --collect-only -q exits 0 with output of the form:
    #   <file>::<test_name>
    #   <file>::<test_name>
    #
    #   <N> tests collected in <T>s
    # Exit 5 means "no tests collected" — which is exactly the miss
    # case we want to drop. Any non-zero non-5 exit is treated as
    # "collection failed for an unrelated reason" — fall through to miss.
    if proc.returncode == 5:
        return None
    if proc.returncode != 0:
        return None
    # Parse out at least one ``<file>::<test>`` line. If pytest's output
    # does not contain such a line (e.g. quiet mode collected zero
    # tests), treat as miss.
    test_lines = [
        line for line in proc.stdout.splitlines() if "::" in line and "[" not in line
    ]
    if not test_lines:
        return None
    # Resolve the PR via pickaxe search on the test name. Constrain to
    # the first collected test's file for tighter PR resolution.
    test_path = test_lines[0].split("::", 1)[0]
    pr = _resolve_pr_for_pattern(repo_root, expr, test_path, timeout_sec=timeout_sec)
    if pr is None:
        pr = _resolve_pr_for_pattern(repo_root, expr, None, timeout_sec=timeout_sec)
    if pr is None:
        return None
    canonical = f"pytest --collect-only -q -k {shlex.quote(expr)}"
    return (pr, canonical)


# ─── Main entrypoint ───────────────────────────────────────────────────────


def probe(body: str, *, repo_root: Path, timeout_sec: int) -> tuple[int, str] | None:
    """Run all Verify clauses in ``body`` against the worktree.

    Returns the FIRST hit as (pr_number, canonical_clause), or None when
    no clause matches. Clauses are evaluated in source order so the AC
    author's first verify line takes precedence — that's typically the
    most direct expression of "what the AC actually pins."
    """
    for clause in _extract_verify_clauses(body):
        cls = _classify_clause(clause)
        if cls is None:
            continue
        shape, argv = cls
        result: tuple[int, str] | None = None
        if shape == "grep":
            result = _probe_grep(argv, repo_root=repo_root, timeout_sec=timeout_sec)
        elif shape == "pytest":
            result = _probe_pytest(argv, repo_root=repo_root, timeout_sec=timeout_sec)
        if result is not None:
            return result
    return None


def _resolve_repo_root() -> Path:
    """Resolve the worktree root the probes should run against.

    Precedence:
      1. ``CHECK_SHIPPED_VERIFY_REPO_ROOT`` env var (tests pass a fixture).
      2. ``git rev-parse --show-toplevel`` from the helper's cwd.
      3. The current working directory (last-resort fallback).
    """
    env_root = os.environ.get("CHECK_SHIPPED_VERIFY_REPO_ROOT")
    if env_root:
        return Path(env_root)
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return Path.cwd()


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 2
    body = data.get("body") or ""
    if not body:
        return 1
    try:
        timeout_sec = int(os.environ.get("CHECK_SHIPPED_VERIFY_TIMEOUT_SEC", "30"))
    except ValueError:
        timeout_sec = 30
    repo_root = _resolve_repo_root()
    hit = probe(body, repo_root=repo_root, timeout_sec=timeout_sec)
    if hit is None:
        return 1
    pr, clause = hit
    print(f"shipped:{pr}\t{clause}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
