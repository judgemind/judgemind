#!/usr/bin/env python3
# _check_shipped_pr_extract_files.py — Extract candidate file paths from an
# issue body for scripts/check-shipped-pr.sh.
#
# Reads JSON {"body": "...", "title": "..."} on stdin, prints one TAB-
# separated line per unique candidate file path:
#
#     <path>\t<context>
#
# where ``<context>`` is one of:
#   - ``target`` — narrative / Proposal / AC text. The path is a
#     load-bearing target the issue intends to change.
#   - ``search`` — appears only inside a ``Verify:`` line, a fenced
#     code block containing a shell invocation (grep/pytest/aws/curl),
#     or another search-context spot. The path is a search argument,
#     not a change target.
#
# The bash glue in scripts/check-shipped-pr.sh splits on TAB to recover
# both lists; the overlap helper applies a target-context-aware
# threshold.
#
# Pure stdlib — no external imports — so it runs from any worktree
# without venv setup.
#
# venv: none
# permanent: true
#
# The regex matches the four conventional repo roots
#   scripts/, packages/, docs/, infra/
# plus `.github/`. It stops at whitespace, `]`, `)`, backticks, or quotes —
# the boundaries that terminate inline file references in markdown bodies.
# A trailing punctuation strip removes `.`, `,`, `:`, `;` (sentence
# punctuation that often follows an inline file reference).
#
# Search-context classifier (issue #4340):
#   A line is a *search-context line* if it matches any of:
#     - ``Verify:`` (case-insensitive, anywhere in the line)
#     - a shell invocation verb followed by a space — ``grep ``,
#       ``pytest ``, ``aws ``, ``curl ``, or ``ripgrep ``/``rg ``
#   A line is also search-context if it falls inside a fenced code
#   block (between triple backticks) AND the fenced block contains
#   a shell-invocation verb anywhere in its body.
#
#   A path's classification is determined by ALL lines it appears in:
#     - If the path appears at least once on a NON-search-context line,
#       it is ``target``.
#     - Otherwise it is ``search``.
#   This precedence rule preserves the load-bearing intent of narrative
#   prose even when the same path is also re-cited inside Verify lines.

import json
import re
import sys

# Match (?:scripts/|packages/|docs/|infra/|\.github/) followed by any
# non-terminator characters. Note: the spec calls for these five roots; we
# intentionally omit `.claude/` and other roots here because issue bodies
# rarely cite them as the locus of changes — the five chosen roots cover
# the vast majority of "this lands at <path>" references.
PATH_REGEX = re.compile(r"(?:scripts/|packages/|docs/|infra/|\.github/)[^\s\]\)`\"',]+")

# Strip these trailing characters from a hit (sentence punctuation that
# often follows an inline file reference but is not part of the path).
TRAILING_STRIP = ".,:;"

# Search-context detector. A line is search-context if it carries a
# ``Verify:`` marker OR a shell-invocation verb followed by a space.
# The verbs listed cover the conventional shapes the AC author uses to
# write a re-run command (``Verify: pytest ...``, ``Verify: grep ...``,
# ``Verify: aws ...``, ``Verify: curl ...``). The space after the verb
# anchors the match to actual invocations and avoids false positives
# on prose like "use grep when..." (which would otherwise match the
# bare ``grep`` token).
SEARCH_CONTEXT_LINE_REGEX = re.compile(
    r"(?:Verify:|(?:^|\s)(?:grep|pytest|aws|curl|ripgrep|rg)\s)",
    re.IGNORECASE,
)

# Fenced-code-block boundary marker (``` at the start of a line, possibly
# preceded by indentation; with optional language label).
FENCE_LINE_REGEX = re.compile(r"^\s*```")


def _is_search_line(line: str) -> bool:
    """True if ``line`` is a search-context line (Verify: / grep / pytest / aws / curl / rg)."""
    return bool(SEARCH_CONTEXT_LINE_REGEX.search(line))


def _classify_lines(body: str) -> list[bool]:
    """Return a parallel list — one bool per line — flagging search-context lines.

    A line is search-context if any of these hold:
      - It directly matches ``SEARCH_CONTEXT_LINE_REGEX`` (Verify: /
        grep / pytest / aws / curl / rg).
      - It falls inside a fenced code block AND the fenced block
        contains at least one shell-invocation verb anywhere in its
        body. The fence boundaries themselves count as search-context
        when the block qualifies.

    Pre-pass over the body identifies fenced blocks and decides whether
    each block carries shell invocations; second pass applies the per-
    line classification using both the per-line regex and the fenced-
    block flag.
    """
    lines = body.split("\n")
    n = len(lines)
    in_fence = [False] * n
    fence_has_shell = [False] * n

    # Identify fenced blocks (start/end indices) and whether each block
    # contains a shell invocation.
    open_idx: int | None = None
    block_has_shell = False
    for i, line in enumerate(lines):
        if FENCE_LINE_REGEX.match(line):
            if open_idx is None:
                # Opening fence
                open_idx = i
                block_has_shell = False
            else:
                # Closing fence — record block flags
                for j in range(open_idx, i + 1):
                    in_fence[j] = True
                    fence_has_shell[j] = block_has_shell
                open_idx = None
                block_has_shell = False
            continue
        if open_idx is not None and _is_search_line(line):
            block_has_shell = True

    # If a block was opened but never closed, treat the lines after it
    # as out-of-block (defensive — malformed markdown shouldn't crash).

    # Per-line search-context flag.
    search_flags: list[bool] = []
    for i, line in enumerate(lines):
        if _is_search_line(line):
            search_flags.append(True)
        elif in_fence[i] and fence_has_shell[i]:
            search_flags.append(True)
        else:
            search_flags.append(False)
    return search_flags


def _path_hits_per_line(line: str) -> list[str]:
    """Return cleaned, filtered file paths cited in ``line``.

    Applies the same trailing-punctuation strip + glob/short-path
    filters as ``extract_files()``. Returned paths may include
    duplicates — dedupe is the caller's responsibility.
    """
    out: list[str] = []
    for match in PATH_REGEX.finditer(line):
        path = match.group(0)
        # Strip trailing punctuation
        while path and path[-1] in TRAILING_STRIP:
            path = path[:-1]
        if "*" in path or "?" in path:
            continue
        if path.endswith("/"):
            continue
        if len(path) < 8:
            continue
        out.append(path)
    return out


def classify_files(body: str) -> tuple[list[str], list[str]]:
    """Classify candidate file paths into (target_context, search_context).

    Returns two ordered, deduped lists in first-seen order:
      - ``target_context``: paths that appear at least once on a non-
        search-context line. These are load-bearing targets the issue
        intends to change.
      - ``search_context``: paths that appear ONLY on search-context
        lines (Verify: / grep / pytest / aws / curl / rg invocations,
        or fenced code blocks containing such invocations). These are
        search arguments, not change targets.

    Precedence: a path appearing in BOTH narrative and a Verify line is
    classified as ``target_context`` — the narrative reference is the
    load-bearing intent.
    """
    if not body:
        return [], []
    search_line_flags = _classify_lines(body)
    # path -> True if seen on at least one non-search line.
    path_targety: dict[str, bool] = {}
    # First-seen ordering (we want target_context and search_context in
    # the order paths first appear in the body).
    order: list[str] = []
    for i, line in enumerate(body.split("\n")):
        is_search = search_line_flags[i]
        for path in _path_hits_per_line(line):
            if path not in path_targety:
                order.append(path)
                path_targety[path] = not is_search
            elif not is_search:
                # Promote a previously-search-only path to target.
                path_targety[path] = True
    target_out: list[str] = []
    search_out: list[str] = []
    for path in order:
        if path_targety[path]:
            target_out.append(path)
        else:
            search_out.append(path)
    return target_out, search_out


def extract_files(body: str) -> list[str]:
    """Return unique candidate file paths from ``body`` in first-seen order.

    Legacy API — preserved for backward compatibility with callers that
    don't care about target/search classification (e.g.
    test_check_shipped_pr_extract_files.py's directory / glob / short-
    path tests). Returns the union of target_context ∪ search_context
    in first-seen order.
    """
    target, search = classify_files(body)
    # Reconstruct first-seen order from the body so the legacy contract
    # (paths in first-seen order) is preserved.
    if not body:
        return []
    seen: set[str] = set()
    out: list[str] = []
    keep = set(target) | set(search)
    for line in body.split("\n"):
        for path in _path_hits_per_line(line):
            if path in keep and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 1
    body = data.get("body") or ""
    title = data.get("title") or ""
    # Combine title + body for extraction (title rarely cites paths but
    # cheap to include). Title is treated as narrative (target-context)
    # — it never carries Verify: / grep / pytest invocations.
    combined = f"{title}\n{body}"
    target, search = classify_files(combined)
    target_set = set(target)
    # Emit one line per path: <path>\t<context>. Preserve first-seen
    # ordering from extract_files()'s legacy contract.
    for path in extract_files(combined):
        ctx = "target" if path in target_set else "search"
        print(f"{path}\t{ctx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
