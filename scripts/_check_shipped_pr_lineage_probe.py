#!/usr/bin/env python3
# _check_shipped_pr_lineage_probe.py — Retrospective-lineage "shipped" probe
# for scripts/check-shipped-pr.sh.
#
# venv: none
# permanent: true
#
# Purpose (issue #4515):
#   When two retrospective-class issues are filed against the same parent
#   issue, they almost always describe the same lesson and one of them
#   ends up being a duplicate. The path-overlap channel and the literal-
#   Verify channel cannot catch this because:
#
#     - Path-overlap requires file-path overlap with the merged PR's diff;
#       a retrospective-class issue typically describes a workflow lesson
#       (a script's UX, a SKILL.md gap), and the sibling that already
#       shipped touched DIFFERENT lines of the same script — the path
#       overlap is incidental at best.
#     - The literal-Verify channel deliberately rejects script-execution
#       clauses (#4472 docstring lines 51-74), so a Verify line of the
#       shape ``./scripts/check-foo.sh prints a Fix: block`` cannot
#       trigger a match.
#
#   But the retrospective-lineage signal — "two issues filed from the
#   same parent retrospective, one already merged, identifier overlap
#   between current-issue body and merged-PR body" — is an independent
#   channel that catches exactly this shape.
#
#   Concrete case: issue #4315 ↔ PR #4345 ↔ sibling #4322.
#     - #4315 body has ``Found by: retrospective on #4304.``
#     - sibling #4322 closed by PR #4345 (``Closes #4322``)
#     - #4315 mentions ``_DATACLASS_SCOPE``, ``*SplitRuling`` in narrative
#     - PR #4345 body mentions ``_DATACLASS_SCOPE``, ``*SplitRuling``
#     - → shipped match via lineage channel
#
# Reads ``gh issue view --json body,title`` JSON on stdin. Emits one of:
#
#   - On match: a single line ``shipped:<pr>\t<sibling>\t<identifiers>``
#     to stdout and exit 0. ``<sibling>`` is the sibling retrospective
#     issue closed by ``<pr>``. ``<identifiers>`` is a comma-separated
#     list of the load-bearing identifiers that overlap between the
#     current-issue body and the candidate-PR body (used for diagnostic
#     logs and the JSON summary's ``lineage_identifiers`` field).
#   - On no match: empty stdout and exit 1. Caller falls through to the
#     path-overlap channel.
#   - On error / malformed input: empty stdout and exit 2.
#
# Algorithm:
#   1. Parse the issue body for any recognized lineage idiom (#4519):
#      ``Found by: retrospective on #N``, ``Found while shipping #N``,
#      ``Same lesson as #N``, or ``Adjacent to #N`` (all
#      case-insensitive). Each captured number is a *lineage parent*.
#      Multiple matches in the same body are deduplicated and ordered by
#      first-occurrence position across all idioms.
#   2. For each lineage parent, list closed issues whose body references
#      the parent via any of the same lineage idioms. The probe issues
#      one ``gh search issues`` call per phrase and unions the results
#      client-side (``gh search`` OR semantics on quoted phrases are not
#      reliable across the GraphQL backend). Drop the current issue
#      itself and the parent itself from the result set.
#   3. For each sibling, fetch its merging PR via
#      ``gh issue view <sibling> --json closedByPullRequestsReferences``.
#      Pre-#3994 placeholder-titled PRs that lack ``Closes #N`` keywords
#      will not appear here — that's by design. The lineage channel
#      catches the *retrospective duplicate* shape, not the placeholder-
#      title zombie shape (which the path-overlap channel handles).
#      Take the FIRST merged PR (typically only one) as the lineage
#      candidate.
#   4. For each lineage candidate, fetch the PR body via
#      ``gh pr view <pr> --json body,mergedAt,baseRefName``. Drop if
#      ``mergedAt`` is null or ``baseRefName != main``.
#   5. Compute identifier overlap. The identifiers are extracted as
#      backtick-wrapped tokens of length ≥3 from both bodies (frozenset
#      names, function names, CamelCase types — the load-bearing
#      identifiers that AC authors and PR authors both quote in
#      backticks). Overlap = set-intersect of the two extracted sets.
#      Match threshold: ≥1 backtick-token overlap. The single-token
#      threshold is intentionally low because the gating signal —
#      "two issues filed from the same retrospective, one already
#      merged" — is already strong; the identifier check is precision
#      defense (does the merged PR actually describe the same
#      identifier the current issue prescribes), not threshold
#      tightening.
#   6. On the FIRST candidate that clears the identifier overlap
#      threshold, emit ``shipped:<pr>\t<sibling>\t<identifiers>`` and
#      exit 0. Multiple matches go through the same single-emission
#      path — the wrapper takes the first hit.
#
# Why backtick-wrapped tokens specifically:
#   - High precision. Identifiers in prose ("the SplitRuling pattern")
#     are too easy to false-positive on (different lessons can mention
#     the same generic concept). Backtick wrapping is the AC author's
#     explicit signal "this is a load-bearing literal."
#   - Symmetric. The same convention applies in PR bodies (template
#     uses ``Closes #N``, summary uses backticks for code identifiers).
#     Computing set intersection on the backtick-extracted tokens is
#     defensible without model-level entity matching.
#   - Low cost. Pure string scan, no LLM call, no extra gh requests.
#
# What is NOT a lineage match:
#   - Two retrospective siblings against the same parent that describe
#     genuinely different lessons → no backtick-token overlap → exit 1.
#     This is the precision case from the issue's AC2.
#   - The current issue has no recognized lineage idiom in its body
#     (none of ``Found by: retrospective on #N``, ``Found while shipping
#     #N``, ``Same lesson as #N``, or ``Adjacent to #N``) → the lineage
#     probe extracts zero parents and exits 1 unconditionally. Issues
#     without retrospective lineage fall through to the path-overlap
#     channel as before.
#   - The lineage parent's siblings are all still open (no merging PRs)
#     → no candidate PRs → exit 1. This is by design — the channel only
#     fires when a sibling has DEMONSTRABLY shipped (closed by a merged
#     PR), not when one is just open.
#
# Environment variables:
#   CHECK_SHIPPED_LINEAGE_REPO     — override "judgemind/judgemind"
#   CHECK_SHIPPED_LINEAGE_GH_BIN   — override "gh" binary path (for tests)
#   CHECK_SHIPPED_LINEAGE_ISSUE    — current issue number (so we drop self
#                                    from the sibling list)
#
# Why this is its own helper rather than inline shell:
#   - Multi-step gh API orchestration (search-issues, issue-view, pr-view)
#     is awkward in bash with proper error handling on each step.
#   - The backtick-token extraction + set-intersection is one-line in
#     Python, multi-line awk in bash.
#   - The classifier is unit-testable in isolation (see
#     ``scripts/tests/test_check_shipped_pr_lineage_probe.py``).

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# ─── Lineage parent extraction ─────────────────────────────────────────────

# Lineage idioms (#4519). Each regex captures a parent issue number from
# a body phrase that signals "this issue was filed because of work on #N."
# All matchers are case-insensitive.
#
# Canonical idiom:
#   - ``Found by: retrospective on #N`` (canonical, established by #4515)
#   - ``Found by retrospective on #N``  (no colon)
#   - ``found by: retrospective on #N`` (lowercase)
#
# Additional idioms (#4519) — same lineage signal, different phrasings
# observed in the corpus:
#   - ``Found while shipping #N``  (e.g. issue #4322 cites #4303 this way)
#   - ``Same lesson as #N``        (explicit cross-reference)
#   - ``Adjacent to #N``           (sibling-issue link)
#
# All four share the same precision-defense — the gating signal is
# "this issue cites another issue as its lineage source," and the
# downstream identifier-overlap check still applies. Adding more
# matchers expands recall without inflating false-positive rate.
LINEAGE_PARENT_RES: tuple[re.Pattern[str], ...] = (
    # Canonical "Found by: retrospective on #N" form.
    re.compile(
        r"Found\s+by:?\s+retrospective\s+on\s+#(\d+)",
        re.IGNORECASE,
    ),
    # "Found while shipping #N" — same idiom, different phrasing.
    re.compile(
        r"Found\s+while\s+shipping\s+#(\d+)",
        re.IGNORECASE,
    ),
    # "Same lesson as #N" — explicit cross-reference.
    re.compile(
        r"Same\s+lesson\s+as\s+#(\d+)",
        re.IGNORECASE,
    ),
    # "Adjacent to #N" — sibling-issue link.
    re.compile(
        r"Adjacent\s+to\s+#(\d+)",
        re.IGNORECASE,
    ),
)

# Backwards-compatibility alias — older call sites and tests may import
# the original singular name. It points at the canonical retrospective-on
# matcher so existing references keep their current semantics.
LINEAGE_PARENT_RE = LINEAGE_PARENT_RES[0]


def extract_lineage_parents(body: str) -> list[int]:
    """Return the deduplicated list of lineage parent issue numbers.

    Walks every recognized lineage idiom (canonical
    ``Found by: retrospective on #N`` plus the #4519 variants:
    ``Found while shipping #N``, ``Same lesson as #N``, ``Adjacent to #N``)
    and unions the captured parent numbers.

    Order is preserved by first-occurrence in the body so the caller can
    prefer earlier mentions when multiple parents are cited (rare). When
    a body uses two different idioms to cite the same parent, only the
    first-seen occurrence is kept.
    """
    seen: set[int] = set()
    out: list[int] = []
    # Walk every match across all idioms in body order. We can't iterate
    # them per-pattern and concatenate because that would lose body-order
    # — we have to scan once and pick up matches from any pattern.
    matches: list[tuple[int, int]] = []
    for pattern in LINEAGE_PARENT_RES:
        for m in pattern.finditer(body):
            matches.append((m.start(), int(m.group(1))))
    matches.sort(key=lambda x: x[0])
    for _, n in matches:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ─── Backtick-token extraction ─────────────────────────────────────────────

# A backtick-wrapped identifier is any non-whitespace, non-backtick
# character sequence between single backticks. Disallowing whitespace
# inside the span prevents the regex from matching across separate
# code-span pairs — e.g. in ``the `=` sign and `a` and `xyz```, a
# whitespace-permitting regex would match the inner span ``= sign and ``
# (3+ chars between backticks 1 and 4), producing garbage tokens.
# Disallowing whitespace also matches the AC author's convention:
# backtick-wrapped tokens in this corpus are identifiers, paths, or
# typed values — never multi-word phrases.
#
# We require length ≥3 to filter trivially-short tokens (``a``, ``=``).
# The classifier's stopword filter (below) drops common short
# identifiers that don't carry lesson-identity signal.
BACKTICK_TOKEN_RE = re.compile(r"`([^`\s\n]{3,})`")

# Stopwords — common backtick-wrapped tokens that don't carry lesson-
# identity signal. These appear in many PR / issue bodies regardless of
# the underlying lesson, so including them in overlap would inflate
# false-positive rates.
STOPWORDS: frozenset[str] = frozenset(
    {
        "main",
        "true",
        "false",
        "null",
        "none",
        "todo",
        "wip",
        "fix",
        "feat",
        "test",
        "docs",
        "chore",
        "refactor",
    }
)


def extract_backtick_tokens(body: str) -> set[str]:
    """Return the set of backtick-wrapped tokens in ``body``.

    Tokens are case-sensitive — ``_DATACLASS_SCOPE`` and
    ``_dataclass_scope`` are NOT considered the same identifier. The
    convention in this codebase is consistent casing (constants are
    SCREAMING_SNAKE, types are CamelCase, functions are snake_case),
    so case-sensitivity matches the AC author's intent.

    Stopwords are filtered. Tokens are stripped of leading/trailing
    whitespace.
    """
    out: set[str] = set()
    for m in BACKTICK_TOKEN_RE.finditer(body):
        tok = m.group(1).strip()
        if not tok:
            continue
        if tok.lower() in STOPWORDS:
            continue
        out.add(tok)
    return out


# ─── gh API orchestration ──────────────────────────────────────────────────


def _run_gh(args: list[str], *, timeout_sec: int = 30) -> tuple[int, str]:
    """Run a gh command. Returns (returncode, stdout)."""
    gh_bin = os.environ.get("CHECK_SHIPPED_LINEAGE_GH_BIN", "gh")
    try:
        proc = subprocess.run(
            [gh_bin] + args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return proc.returncode, proc.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 124, ""


# Search phrases that signal "this closed issue is a sibling
# retrospective citing the same parent" (#4519). Each entry is a phrase
# template — ``{parent}`` is substituted with the parent issue number.
# The phrases mirror the ``LINEAGE_PARENT_RES`` matchers above so any
# idiom recognized by the body parser is also discoverable via search.
#
# Why a tuple of phrases instead of one OR'd query: ``gh search
# issues "..."`` interprets the entire string as a single query token
# and the OR semantics on quoted phrases are not reliable across the
# GraphQL backend. Multiple search calls + client-side union is the
# straightforward, testable shape.
LINEAGE_SEARCH_PHRASES: tuple[str, ...] = (
    "retrospective on #{parent}",
    "Found while shipping #{parent}",
    "Same lesson as #{parent}",
    "Adjacent to #{parent}",
)


def find_sibling_retrospectives(
    parent: int, *, repo: str, current_issue: int
) -> list[int]:
    """Find closed issues whose body references ``<parent>`` via lineage idioms.

    Searches for all four lineage idioms (#4519):
    ``retrospective on #N``, ``Found while shipping #N``,
    ``Same lesson as #N``, and ``Adjacent to #N``. Unions the results
    in search-result order (the canonical retrospective-on phrase
    runs first, matching prior precedence; subsequent phrases append
    only their non-duplicate hits).

    Excludes ``current_issue`` AND ``parent`` from the result (the parent
    issue's body sometimes also matches the literal string when a
    follow-up retrospective comment references back to itself).

    Why the search query uses CLI flags instead of inline ``repo:`` /
    ``state:`` qualifiers: ``gh search issues "..."`` interprets the
    entire string as a single query token and does NOT expand inline
    qualifiers. The supported invocation shape is to pass the literal
    body match as the query argv and the qualifiers as separate flags
    (``--repo``, ``--state``). Wrapping the whole thing in one quoted
    string returns ``Invalid search query`` from gh's GraphQL backend.
    """
    siblings: list[int] = []
    seen: set[int] = set()
    for phrase_template in LINEAGE_SEARCH_PHRASES:
        # Use ``gh search issues`` with a literal-string match in the body.
        # The query string is the literal phrase to search for; --repo,
        # --state are passed as separate flags (gh-cli requirement — see
        # docstring above).
        phrase = phrase_template.format(parent=parent)
        query = f'"{phrase}"'
        rc, out = _run_gh(
            [
                "search",
                "issues",
                query,
                "--repo",
                repo,
                "--state",
                "closed",
                "--json",
                "number",
                "--limit",
                "20",
            ],
        )
        if rc != 0 or not out.strip():
            # One phrase failing (rate limit, transient API error) does
            # not abort the whole probe — keep walking the remaining
            # phrases. The probe is best-effort by design.
            continue
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            continue
        for entry in data:
            if not isinstance(entry, dict):
                continue
            n = entry.get("number")
            if not isinstance(n, int):
                continue
            if n == current_issue:
                continue
            if n == parent:
                # The parent itself can match the search when its own
                # body references the lineage phrase (rare but observed
                # when the parent retrospective summary mentions
                # follow-up issues filed against it). Drop it — the
                # parent is the lineage SOURCE, not a sibling.
                continue
            if n in seen:
                continue
            seen.add(n)
            siblings.append(n)
    return siblings


def find_closing_pr(sibling: int, *, repo: str) -> int | None:
    """Find the merged PR that closed ``sibling``.

    Returns the PR number, or None when the sibling has no merging PR
    (e.g. closed manually with ``--reason completed``, closed by a
    placeholder-titled PR that lacks ``Closes #N``, etc.).
    """
    rc, out = _run_gh(
        [
            "issue",
            "view",
            str(sibling),
            "--repo",
            repo,
            "--json",
            "closedByPullRequestsReferences",
        ],
    )
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    refs = data.get("closedByPullRequestsReferences")
    if not isinstance(refs, list):
        return None
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        n = ref.get("number")
        # gh's closedByPullRequestsReferences only includes merged PRs
        # (the `state` field is "MERGED" when present), but check
        # defensively in case the schema changes.
        state = ref.get("state", "")
        if state and state.upper() not in ("MERGED", "CLOSED"):
            continue
        if isinstance(n, int):
            return n
    return None


def fetch_pr_body(pr: int, *, repo: str) -> tuple[str, bool] | None:
    """Fetch ``(body, eligible)`` for ``pr`` or None on error.

    ``eligible`` is True only when the PR is merged onto ``main``.
    """
    rc, out = _run_gh(
        [
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "body,mergedAt,baseRefName",
        ],
    )
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    body = data.get("body") or ""
    merged_at = data.get("mergedAt")
    base_ref = data.get("baseRefName") or ""
    eligible = bool(merged_at) and base_ref == "main"
    return body, eligible


# ─── Probe entrypoint ──────────────────────────────────────────────────────


def probe(
    body: str, *, repo: str, current_issue: int
) -> tuple[int, int, list[str]] | None:
    """Run the lineage probe against ``body``.

    Returns ``(pr, sibling, identifiers)`` on the first match, or None
    when no lineage candidate clears the identifier-overlap threshold.
    """
    parents = extract_lineage_parents(body)
    if not parents:
        return None
    current_tokens = extract_backtick_tokens(body)
    if not current_tokens:
        # No identifiers to compare — refuse to fire. The single-token
        # threshold can only be cleared when the current issue has at
        # least one backtick-wrapped identifier in its body. An issue
        # with zero backtick spans is too weak a signal for a lineage
        # match no matter how the parent's siblings shipped.
        return None
    for parent in parents:
        siblings = find_sibling_retrospectives(
            parent, repo=repo, current_issue=current_issue
        )
        for sibling in siblings:
            pr = find_closing_pr(sibling, repo=repo)
            if pr is None:
                continue
            pr_data = fetch_pr_body(pr, repo=repo)
            if pr_data is None:
                continue
            pr_body, eligible = pr_data
            if not eligible:
                continue
            pr_tokens = extract_backtick_tokens(pr_body)
            overlap = current_tokens & pr_tokens
            if not overlap:
                continue
            # Sort for stable output across runs.
            return pr, sibling, sorted(overlap)
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 2
    body = data.get("body") or ""
    if not body:
        return 1
    repo = os.environ.get("CHECK_SHIPPED_LINEAGE_REPO", "judgemind/judgemind")
    current_issue_str = os.environ.get("CHECK_SHIPPED_LINEAGE_ISSUE", "")
    try:
        current_issue = int(current_issue_str) if current_issue_str else 0
    except ValueError:
        current_issue = 0
    hit = probe(body, repo=repo, current_issue=current_issue)
    if hit is None:
        return 1
    pr, sibling, identifiers = hit
    print(f"shipped:{pr}\t{sibling}\t{','.join(identifiers)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
