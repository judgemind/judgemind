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
# FAIL-SAFE GUARDS (issue #4666):
#   A grep hit / collected test proves only that the target EXISTS — which
#   is equally true of unfixed code. On #4661 (an open p1 data-loss bug)
#   the AC's ``grep -n "capture_timestamp" scripts/rebuild_db.py`` matched
#   the buggy line itself, and an unscoped pickaxe fallback credited PR
#   #4324, which predated the issue and never touched rebuild_db.py. The
#   probe therefore defaults to "no match" unless there is evidence of the
#   fix:
#     - The issue's ``createdAt`` must be known, and the resolved PR's
#       squash-merge commit must post-date it.
#     - PR resolution is scoped to the grep-matched / test-collected files.
#       There is no unscoped fallback.
#     - Grep clauses fire only when the AC prose after the backticked
#       command is empty or a bare-existence phrase ("returns a match").
#       Qualitative / negative prose ("sourced from X, not Y", "no
#       matches") is skipped — a hit can't confirm it.
#     - Pytest clauses with negative prose ("no longer collects") are
#       skipped.
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
from datetime import UTC, datetime
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
INLINE_BACKTICK_RE = re.compile(r"^`([^`]+)`(.*)$")


def _extract_verify_entries(body: str) -> list[tuple[str, str]]:
    """Return ``(command, prose)`` pairs for every Verify clause in ``body``.

    ``command`` is the post-``Verify:`` portion of the line, with
    leading/trailing whitespace and surrounding backticks stripped.
    Bullet markers, bold-emphasis wrappers, and indentation are consumed
    by ``VERIFY_LINE_RE``; only the actual command remains.

    ``prose`` is the text trailing a backtick-wrapped command (e.g.
    ``returns a match`` in ``Verify: `grep x y` returns a match``) — the
    AC author's statement of what the command's output must look like.
    For un-backticked clauses the command and prose can't be separated,
    so ``prose`` is empty (#4666).

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
    out: list[tuple[str, str]] = []
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
        prose = ""
        bt = INLINE_BACKTICK_RE.match(raw)
        if bt:
            prose = bt.group(2).strip()
            raw = bt.group(1).strip()
        if raw:
            out.append((raw, prose))
    return out


def _extract_verify_clauses(body: str) -> list[str]:
    """Return just the Verify clause command strings from ``body``."""
    return [cmd for cmd, _ in _extract_verify_entries(body)]


# ─── Assertion-strength gate (#4666) ───────────────────────────────────────
#
# A grep hit proves only that the pattern EXISTS. It confirms an AC only
# when the AC asserts nothing more than existence ("returns a match").
# For negative assertions ("not X", "no matches", "no longer ...") or
# qualitative ones ("shows it sourced from S3, not datetime.now") a bare
# hit cannot tell the fixed state from the bug — on #4661 the grep hit
# the buggy line itself. So a grep clause only fires when its trailing
# prose is empty or a bare-existence phrase.

_BARE_EXISTENCE_RE = re.compile(
    r"""^(?:
        (?:returns?|shows?|finds?|has|have|yields?|prints?|lists?|produces?|gives?|outputs?)
        \s+(?:at\s+least\s+one|one\s+or\s+more|a|an|some|>=\s*1|≥\s*1|1\+)?\s*
        (?:match(?:es)?|hits?|results?|lines?|output|occurrences?)
      | match(?:es)?
      | finds?\s+(?:it|them|the\s+\w+)
      | is\s+non-?empty
      | exits?\s+0
      | succeeds
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

# Negation / negative-outcome markers. Used for pytest clauses (where the
# existing "collection exists" semantics tolerate prose like "passes") to
# reject ACs that assert the test must NOT be collected.
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|none|nothing|zero|without|absent|instead|rather)\b|n't\b",
    re.IGNORECASE,
)


def _normalize_prose(prose: str) -> str:
    """Strip surrounding punctuation / separators from AC prose."""
    return prose.strip().strip(" \t.;:,—–-()").strip()


def _prose_is_bare_existence(prose: str) -> bool:
    """True when ``prose`` asserts nothing beyond "the command has output"."""
    norm = _normalize_prose(prose)
    if not norm:
        return True
    return bool(_BARE_EXISTENCE_RE.match(norm))


def _prose_is_negative(prose: str) -> bool:
    """True when ``prose`` contains a negation / negative-outcome marker."""
    return bool(_NEGATION_RE.search(prose))


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


def _parse_iso8601(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` suffix allowed); None if invalid.

    Naive timestamps are treated as UTC so comparisons never raise.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _resolve_pr_for_pattern(
    repo_root: Path, pattern: str, paths: list[str], *, timeout_sec: int
) -> tuple[int, datetime | None] | None:
    """Find the most-recent squash-merge PR that changed ``pattern`` in ``paths``.

    Uses ``git log -S <pattern>`` (the "pickaxe" search) to find commits
    where the count of occurrences of ``pattern`` changed, constrained to
    ``paths``. Returns ``(pr_number, commit_date)`` parsed from the most
    recent matching commit (the subject's trailing ``(#N)`` token and the
    committer date — for a squash-merge, the merge time), or None when no
    candidate is found.

    ``paths`` is REQUIRED and non-empty (#4666). There is deliberately no
    unscoped fallback: on #4661 the scoped search found no PR-tagged
    commit, and the unscoped fallback credited PR #4324, which never
    touched the grep-matched file. A PR that didn't change the pattern in
    the matched file is not evidence of anything.

    The pickaxe search is exact-string by default; we don't pass ``-G``
    (regex) because AC patterns are typically string literals (frozenset
    names, function names, magic strings).
    """
    if not paths:
        return None
    cmd = [
        "git",
        "log",
        "-S",
        pattern,
        "--format=%cI%x09%s",
        "--max-count=10",
        "--",
        *paths,
    ]
    proc = _run(cmd, cwd=repo_root, timeout_sec=timeout_sec)
    if proc.returncode != 0:
        return None
    # Each line is ``<committer-date>\t<subject>``. Walk in newest-first
    # order (git log default) and return the FIRST line whose subject
    # ends in ``(#N)`` — that's the squash-merge PR.
    for line in proc.stdout.splitlines():
        date_str, _, subject = line.partition("\t")
        # Match the LAST ``(#N)`` token on the subject — handles both
        # the conventional ``feat(x): foo (#1234)`` and the chained
        # ``fix(ci): squash (#2837) (#3170)`` shape (#4214 lesson —
        # always pick the trailing token, never the first).
        m = re.search(r"\(#(\d+)\)\s*$", subject)
        if m:
            return (int(m.group(1)), _parse_iso8601(date_str))
    return None


def _merged_after_issue(
    commit_date: datetime | None, issue_created_at: datetime | None
) -> bool:
    """Date-ordering guard (#4666): the PR must post-date the issue.

    A PR merged before the issue was filed cannot have fixed it — the
    grep/test target already existed when the issue was written, i.e. it
    describes the pre-fix state. Unknown dates on either side are
    ambiguous and default to False ("not shipped").
    """
    if commit_date is None or issue_created_at is None:
        return False
    return commit_date >= issue_created_at


def _probe_grep(
    argv: list[str],
    *,
    repo_root: Path,
    timeout_sec: int,
    issue_created_at: datetime | None,
) -> tuple[int, str] | None:
    """Run a grep clause against ``repo_root`` and resolve the PR on match.

    Returns (pr_number, canonical_clause) on hit, None on miss. The
    resolved PR must have changed the pattern in a grep-matched file AND
    have merged after the issue was filed (#4666).
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
    # Found at least one match — resolve the introducing PR, scoped to
    # the matched files only (``-l`` mode emits one path per line). No
    # unscoped fallback (#4666): a PR that never changed the pattern in a
    # matched file cannot be the one that shipped it.
    matched_files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    resolved = _resolve_pr_for_pattern(
        repo_root, pattern, matched_files, timeout_sec=timeout_sec
    )
    if resolved is None:
        return None
    pr, commit_date = resolved
    if not _merged_after_issue(commit_date, issue_created_at):
        return None
    canonical = f"grep {shlex.quote(pattern)} " + " ".join(
        shlex.quote(p) for p in paths
    )
    return (pr, canonical.rstrip())


def _probe_pytest(
    argv: list[str],
    *,
    repo_root: Path,
    timeout_sec: int,
    issue_created_at: datetime | None,
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
    # Resolve the PR via pickaxe search on the test name, scoped to the
    # collected tests' files (no unscoped fallback — #4666).
    test_paths = sorted({line.split("::", 1)[0] for line in test_lines})
    resolved = _resolve_pr_for_pattern(
        repo_root, expr, test_paths, timeout_sec=timeout_sec
    )
    if resolved is None:
        return None
    pr, commit_date = resolved
    if not _merged_after_issue(commit_date, issue_created_at):
        return None
    canonical = f"pytest --collect-only -q -k {shlex.quote(expr)}"
    return (pr, canonical)


# ─── Main entrypoint ───────────────────────────────────────────────────────


def probe(
    body: str,
    *,
    repo_root: Path,
    timeout_sec: int,
    issue_created_at: str | None,
) -> tuple[int, str] | None:
    """Run all Verify clauses in ``body`` against the worktree.

    Returns the FIRST hit as (pr_number, canonical_clause), or None when
    no clause matches. Clauses are evaluated in source order so the AC
    author's first verify line takes precedence — that's typically the
    most direct expression of "what the AC actually pins."

    Fail-safe (#4666): a hit only proves the grep/test target exists,
    which is also true of the unfixed code. So the probe returns None
    (→ "not shipped") unless there is evidence of the fix:
      - the issue's ``createdAt`` is known and the resolved PR merged
        after it (a PR that predates the issue cannot have fixed it);
      - the PR changed the pattern in a matched file (no unscoped
        pickaxe fallback);
      - grep clauses assert only existence (qualitative / negative AC
        prose like "sourced from X, not Y" can't be confirmed by a hit);
      - pytest clauses carry no negative prose.
    """
    created_at = _parse_iso8601(issue_created_at)
    if created_at is None:
        return None
    for clause, prose in _extract_verify_entries(body):
        cls = _classify_clause(clause)
        if cls is None:
            continue
        shape, argv = cls
        result: tuple[int, str] | None = None
        if shape == "grep":
            if not _prose_is_bare_existence(prose):
                continue
            result = _probe_grep(
                argv,
                repo_root=repo_root,
                timeout_sec=timeout_sec,
                issue_created_at=created_at,
            )
        elif shape == "pytest":
            if _prose_is_negative(prose):
                continue
            result = _probe_pytest(
                argv,
                repo_root=repo_root,
                timeout_sec=timeout_sec,
                issue_created_at=created_at,
            )
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
    created_at = data.get("createdAt")
    hit = probe(
        body,
        repo_root=repo_root,
        timeout_sec=timeout_sec,
        issue_created_at=created_at if isinstance(created_at, str) else None,
    )
    if hit is None:
        return 1
    pr, clause = hit
    print(f"shipped:{pr}\t{clause}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
