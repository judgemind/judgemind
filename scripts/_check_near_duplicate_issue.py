#!/usr/bin/env python3
# _check_near_duplicate_issue.py — Near-duplicate-issue probe helper for
# scripts/check-near-duplicate-issue.sh.
#
# venv: none
# permanent: true
#
# Purpose (issue #4520):
#   ``check-shipped-pr.sh`` correctly does NOT flag a candidate PR as a
#   shipped match for an issue that was filed AFTER the PR merged — the
#   #4353 date-ordering guard rejects ``pr.mergedAt < issue.createdAt``,
#   which is correct by construction (a PR cannot have shipped an issue
#   that didn't exist yet). But there is a missed signal in this regime:
#   when issue X is filed shortly after issue Y closes AND X's body /
#   title overlap heavily with Y's body / title and the PR that closed
#   Y, X is almost certainly a near-duplicate of Y. The agent picking up
#   X should READ Y / Y's PR before re-implementing.
#
#   Concrete worked example:
#     - #4321 (closed 2026-05-08T17:07Z, by PR #4325) — "feat(dx): generic
#       splitter-carry-forward drain helper to close audit thresholds
#       post-deploy". Body cites
#       ``scripts/drain_splitter_carry_forward_clusters.py``.
#     - #4355 (filed 2026-05-08T19:49Z, ~2.5h later) — "tooling:
#       drain_splitter_carry_forward_clusters.py canonical drain helper".
#       Body cites the same script path and prescribes the same helper.
#     - The two are near-duplicates; the agent who picked up #4355 was
#       lucky enough to recognize this via direct file inspection during
#       /task §4b. Without the recognition, they would have run a full
#       ralph cycle and produced a duplicate PR.
#
#   This probe is the issue-side complement to the date-ordering guard.
#   The date guard says "PR #4325 can't have shipped #4355's exact work."
#   The near-duplicate probe says "but #4355's intent overlaps heavily
#   with #4321 — read #4321 / PR #4325 before re-implementing."
#
# Algorithm:
#   1. Read the current issue's title + body + createdAt from stdin (the
#      shell wrapper passes ``gh issue view --json body,title,createdAt``
#      output verbatim).
#   2. List recently-closed issues in a window
#      (default ``CHECK_NEAR_DUP_WINDOW_DAYS=7``) ending at the current
#      issue's createdAt. The window is ANCHORED at the issue's
#      createdAt — not at "now" — so re-running the probe against an
#      older issue is reproducible. Use ``gh issue list --state closed
#      --search "closed:<from>..<to>"``.
#   3. For each closed candidate, fetch title + body + closing PR via
#      ``gh issue view <N> --json title,body,closedAt,
#      closedByPullRequestsReferences``.
#   4. Score similarity vs the current issue. Two channels:
#        a. **Title-token overlap.** Tokenize titles (lowercased, split on
#           non-alphanumerics, drop length<3 and stopwords), set-
#           intersect. Overlap count threshold:
#           ``CHECK_NEAR_DUP_TITLE_THRESHOLD=2`` distinct tokens.
#        b. **Body file-path overlap.** Reuse the same target-context
#           extractor that ``check-shipped-pr.sh`` already uses
#           (``_check_shipped_pr_extract_files.py``) so a path cited
#           inside both bodies counts. Overlap count threshold:
#           ``CHECK_NEAR_DUP_PATH_THRESHOLD=1`` distinct path.
#      A candidate is a near-duplicate when EITHER channel clears its
#      threshold. Calibration on real corpus pairs (#4321/#4355 plus
#      five negative pairs from a 50-issue sample) shows this OR-of-
#      ANDs shape gives <1 FP per 10 pairs while still catching the
#      canonical case. See ``scripts/tests/
#      test_check_near_duplicate_issue.py``.
#   5. On the FIRST candidate that clears the threshold, emit
#      ``near-duplicate:<closed_issue>\t<closing_pr>\t<channel>\t<overlap>``
#      to stdout and exit 0. ``<channel>`` is ``title``, ``path``, or
#      ``both``. ``<overlap>`` is a comma-separated list of the
#      load-bearing tokens / paths.
#   6. On no match: empty stdout, exit 1. The shell wrapper falls open.
#   7. On error / malformed input: empty stdout, exit 2.
#
# Why title overlap AND path overlap (not just one):
#   - Title-only overlap fires on adjacency that doesn't represent a
#     duplicate (two issues both titled ``fix(scrapers): ...`` will
#     trivially clear a low title threshold without sharing intent).
#     Adding the file-path channel keeps the precision up.
#   - Path-only overlap fires on issues that touch the same file but
#     describe different changes (e.g. one issue adds a feature to
#     ``scripts/foo.sh`` while another fixes a bug in the same file).
#     Adding the title channel keeps the recall up.
#   - Either signal alone has higher FP rate than the combined OR-rule;
#     the OR rule is intentionally MORE permissive than AND because the
#     downstream action (the agent reads the closed issue's PR) is
#     low-cost — false positives cost a 30-second read, false negatives
#     cost a full ralph cycle.
#
# Environment variables:
#   CHECK_NEAR_DUP_REPO              — override "judgemind/judgemind"
#   CHECK_NEAR_DUP_GH_BIN            — override "gh" binary path (tests)
#   CHECK_NEAR_DUP_ISSUE             — current issue number (drop self
#                                      from candidate list)
#   CHECK_NEAR_DUP_WINDOW_DAYS       — lookback window in days (default 7)
#   CHECK_NEAR_DUP_TITLE_THRESHOLD   — title-token overlap floor (default 2)
#   CHECK_NEAR_DUP_PATH_THRESHOLD    — path overlap floor (default 1)
#   CHECK_NEAR_DUP_LIMIT             — candidate cap (default 30)
#
# This module is also imported as a library by the test suite. The
# pure-python helpers (``tokenize_title``, ``extract_target_paths``,
# ``score_candidate``) are independently testable — the gh subprocess
# orchestration is patched at the ``_run_gh`` boundary by the tests.

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── Title tokenization ────────────────────────────────────────────────────

# Split on any run of non-alphanumeric / non-underscore character. This
# matches the conventional-commits prefix splitter (``feat(dx):``,
# ``tooling:``) plus the natural word boundaries inside titles. We
# preserve underscores so identifier-style tokens like
# ``drain_splitter_carry_forward_clusters`` survive intact.
_TITLE_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

# Stopwords — common title fillers that don't carry intent signal. The
# set is deliberately small — over-aggressive stop-listing drops the
# canonical-pair signal (#4321 ↔ #4355 share ``drain``,
# ``carry``, ``forward``, ``clusters`` — all real signal).
_TITLE_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "via",
        "has",
        "have",
        "are",
        "was",
        "were",
        "this",
        "that",
        "those",
        "these",
        "such",
        "but",
        "not",
        "all",
        "any",
        "new",
        "use",
        "uses",
        "used",
        # Common conventional-commits scope words and labels — they fire
        # on most titles in this repo and so don't carry duplicate
        # signal. Keep ``feat`` and ``fix`` and ``test`` etc. on the
        # list — the semantic body of the title is what we're after,
        # not the commit-type prefix.
        "feat",
        "fix",
        "chore",
        "docs",
        "test",
        "tests",
        "tooling",
        "refactor",
        "ci",
        "infra",
        "dx",
        # Tier-1 type labels also fire too often.
        "type",
        "area",
        "priority",
    }
)


def tokenize_title(title: str) -> set[str]:
    """Return the stopword-filtered, lowercased token set for ``title``.

    Tokens are length ≥3 lowercase identifiers. Stopwords are dropped.
    Numeric-only tokens (e.g. ``2026``) are also dropped — they almost
    always reflect dates/years rather than intent.
    """
    out: set[str] = set()
    for m in _TITLE_TOKEN_RE.finditer(title):
        tok = m.group(0).lower()
        if len(tok) < 3:
            continue
        if tok.isdigit():
            continue
        if tok in _TITLE_STOPWORDS:
            continue
        out.add(tok)
    return out


# ─── Body file-path extraction (delegates to check-shipped-pr extractor) ───
#
# We share the existing target-context extractor so paths cited inside
# Verify: lines / fenced shell blocks / search-context strings don't
# falsely register as overlap. The extractor classifies each path as
# ``target`` or ``search``; we only count target paths for the
# near-duplicate probe.


def _load_extractor():
    """Load _check_shipped_pr_extract_files as a module by file path.

    Returns the loaded module, or None when unavailable. ``None``
    degrades gracefully — the path channel becomes a no-op and the
    title channel is the sole signal. This keeps the probe from
    crashing in odd checkout shapes (partial sparse checkouts that
    drop the sibling helper, etc.).
    """
    here = Path(__file__).resolve().parent
    extractor_path = here / "_check_shipped_pr_extract_files.py"
    if not extractor_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "check_shipped_pr_extract_files", extractor_path
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def extract_target_paths(body: str) -> set[str]:
    """Return the set of target-context file paths cited in ``body``.

    Delegates to ``_check_shipped_pr_extract_files``'s pure-python
    classifier when available. Falls back to a permissive regex
    (matches paths under conventional repo roots) when the helper is
    not loadable. The fallback over-counts (no Verify-line filter), but
    in the degraded-path mode the title channel is doing most of the
    work anyway.
    """
    mod = _load_extractor()
    if mod is not None:
        # The extractor exposes ``classify_files(body)`` returning
        # ``(target_list, search_list)`` — see
        # ``_check_shipped_pr_extract_files.py``. We only want the
        # target-context paths; search-context paths are deliberately
        # excluded (Verify: lines, fenced shell blocks).
        fn = getattr(mod, "classify_files", None)
        if fn is not None:
            try:
                target_list, _search_list = fn(body)
                return set(target_list)
            except Exception:
                # Fall through to the regex fallback below.
                pass
    # Fallback: regex-based path extraction. Same path roots as the
    # extract-files helper hardcodes (#4340).
    pattern = re.compile(
        r"(?<![\w/])"
        r"((?:scripts|packages|docs|infra|\.github)/[A-Za-z0-9_./\-]+)"
    )
    out: set[str] = set()
    for m in pattern.finditer(body):
        path = m.group(1).rstrip(".,;:")
        if path:
            out.add(path)
    return out


# ─── Candidate scoring ─────────────────────────────────────────────────────


def score_candidate(
    *,
    current_title: str,
    current_body: str,
    candidate_title: str,
    candidate_body: str,
    title_threshold: int,
    path_threshold: int,
) -> tuple[str, set[str]] | None:
    """Score ``candidate`` vs ``current``; return (channel, overlap) on match.

    Returns:
        ``("title", overlap)`` when only the title channel clears.
        ``("path", overlap)`` when only the path channel clears.
        ``("both", overlap)`` when both channels clear (overlap is the
            UNION of the two channel overlaps).
        ``None`` when neither channel clears its threshold.

    The threshold check is per-channel; an issue with one shared title
    token and one shared path will NOT clear unless one of the per-
    channel thresholds is set to 1.

    Calibration default (title_threshold=2, path_threshold=1) is the
    OR rule documented in the module docstring — at least two title
    tokens overlap OR at least one target-context path overlaps.
    """
    cur_title_tokens = tokenize_title(current_title)
    cand_title_tokens = tokenize_title(candidate_title)
    title_overlap = cur_title_tokens & cand_title_tokens

    cur_paths = extract_target_paths(current_body)
    cand_paths = extract_target_paths(candidate_body)
    path_overlap = cur_paths & cand_paths

    title_clears = len(title_overlap) >= title_threshold
    path_clears = len(path_overlap) >= path_threshold

    if title_clears and path_clears:
        return "both", title_overlap | path_overlap
    if title_clears:
        return "title", title_overlap
    if path_clears:
        return "path", path_overlap
    return None


# ─── gh subprocess wrapper ─────────────────────────────────────────────────


def _run_gh(args: list[str], *, timeout_sec: int = 30) -> tuple[int, str]:
    """Run gh and return (returncode, stdout). Captures stderr but discards."""
    gh_bin = os.environ.get("CHECK_NEAR_DUP_GH_BIN", "gh")
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


# ─── Recently-closed issue listing ─────────────────────────────────────────


def _parse_iso_utc(ts: str) -> datetime | None:
    """Parse an ISO-8601 ``Z``-suffixed UTC timestamp. Returns None on error."""
    if not ts:
        return None
    # gh emits ``YYYY-MM-DDTHH:MM:SSZ``; ``fromisoformat`` accepts that
    # in Python 3.11+ (which the repo standardizes on per
    # docs/agent/code-standards.md §Python).
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def list_recently_closed(
    *,
    repo: str,
    current_issue: int,
    created_at: datetime,
    window_days: int,
    limit: int,
) -> list[int]:
    """Return issue numbers closed within ``window_days`` ending at ``created_at``.

    Excludes ``current_issue``. Order: most-recently-closed first
    (gh's default sort).

    The window is anchored at ``created_at`` — not at "now" — so
    re-running the probe is reproducible. ``window_days=7`` plus a
    typical ~24h spread between near-duplicate pairs gives 7× safety
    margin.
    """
    from_date = (created_at - timedelta(days=window_days)).strftime("%Y-%m-%d")
    to_date = created_at.strftime("%Y-%m-%d")
    # gh's --search syntax uses ``closed:>=YYYY-MM-DD`` style ranges.
    # The closed-range form ``closed:from..to`` works with full dates.
    search_str = f"closed:{from_date}..{to_date}"
    rc, out = _run_gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--search",
            search_str,
            "--limit",
            str(limit),
            "--json",
            "number",
        ],
    )
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    nums: list[int] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        n = entry.get("number")
        if not isinstance(n, int):
            continue
        if n == current_issue:
            continue
        nums.append(n)
    return nums


def fetch_issue_detail(
    issue: int, *, repo: str
) -> tuple[str, str, datetime | None, int | None] | None:
    """Fetch (title, body, closedAt, closing_pr) for ``issue``.

    Returns None on error. ``closing_pr`` is None when the issue was
    closed without a merging PR (manual close, multiple PRs, etc.).
    """
    rc, out = _run_gh(
        [
            "issue",
            "view",
            str(issue),
            "--repo",
            repo,
            "--json",
            "title,body,closedAt,closedByPullRequestsReferences",
        ],
    )
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    title = data.get("title") or ""
    body = data.get("body") or ""
    closed_at = _parse_iso_utc(data.get("closedAt") or "")
    closing_pr: int | None = None
    refs = data.get("closedByPullRequestsReferences")
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            n = ref.get("number")
            state = ref.get("state", "")
            if state and state.upper() not in ("MERGED", "CLOSED"):
                continue
            if isinstance(n, int):
                closing_pr = n
                break
    return title, body, closed_at, closing_pr


# ─── Probe entrypoint ──────────────────────────────────────────────────────


def probe(
    *,
    current_title: str,
    current_body: str,
    current_created_at: datetime,
    current_issue: int,
    repo: str,
    window_days: int = 7,
    title_threshold: int = 2,
    path_threshold: int = 1,
    limit: int = 30,
) -> tuple[int, int | None, str, list[str]] | None:
    """Run the near-duplicate probe.

    Returns ``(closed_issue, closing_pr_or_None, channel, overlap_list)``
    on the first match, or None when no candidate clears the threshold.
    The closing_pr is None when the closed issue had no merging PR
    reference — the agent should still be prompted to read the closed
    issue itself.
    """
    candidates = list_recently_closed(
        repo=repo,
        current_issue=current_issue,
        created_at=current_created_at,
        window_days=window_days,
        limit=limit,
    )
    for cand in candidates:
        detail = fetch_issue_detail(cand, repo=repo)
        if detail is None:
            continue
        cand_title, cand_body, _closed_at, closing_pr = detail
        scored = score_candidate(
            current_title=current_title,
            current_body=current_body,
            candidate_title=cand_title,
            candidate_body=cand_body,
            title_threshold=title_threshold,
            path_threshold=path_threshold,
        )
        if scored is None:
            continue
        channel, overlap = scored
        return cand, closing_pr, channel, sorted(overlap)
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 2
    title = data.get("title") or ""
    body = data.get("body") or ""
    created_at_str = data.get("createdAt") or ""
    created_at = _parse_iso_utc(created_at_str)
    if created_at is None:
        # Without a createdAt anchor, the search window is undefined.
        # Default to "now" — agents running the probe interactively
        # may pass payloads without createdAt, and ``now`` matches
        # the operator's expectation of "look back from today."
        created_at = datetime.now(timezone.utc)

    repo = os.environ.get("CHECK_NEAR_DUP_REPO", "judgemind/judgemind")
    current_issue_str = os.environ.get("CHECK_NEAR_DUP_ISSUE", "")
    try:
        current_issue = int(current_issue_str) if current_issue_str else 0
    except ValueError:
        current_issue = 0

    def _env_int(name: str, default: int) -> int:
        raw = os.environ.get(name, "")
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    window_days = _env_int("CHECK_NEAR_DUP_WINDOW_DAYS", 7)
    title_threshold = _env_int("CHECK_NEAR_DUP_TITLE_THRESHOLD", 2)
    path_threshold = _env_int("CHECK_NEAR_DUP_PATH_THRESHOLD", 1)
    limit = _env_int("CHECK_NEAR_DUP_LIMIT", 30)

    hit = probe(
        current_title=title,
        current_body=body,
        current_created_at=created_at,
        current_issue=current_issue,
        repo=repo,
        window_days=window_days,
        title_threshold=title_threshold,
        path_threshold=path_threshold,
        limit=limit,
    )
    if hit is None:
        return 1
    closed_issue, closing_pr, channel, overlap_list = hit
    pr_str = str(closing_pr) if closing_pr is not None else ""
    overlap_str = ",".join(overlap_list)
    print(f"near-duplicate:{closed_issue}\t{pr_str}\t{channel}\t{overlap_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
