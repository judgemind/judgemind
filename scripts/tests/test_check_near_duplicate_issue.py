# venv: none
"""Tests for ``scripts/_check_near_duplicate_issue.py``.

The near-duplicate-issue probe (issue #4520) is a workflow accelerator
that catches the missed signal in ``check-shipped-pr.sh``'s
date-ordering guard regime (#4353): when issue X is filed shortly
AFTER issue Y closes AND X's title / body overlap heavily with Y's,
X is almost certainly a near-duplicate of Y. The agent picking up X
should READ Y / Y's PR before re-implementing.

These tests cover four AC scenarios:

  1. Match within window — #4321 ↔ #4355 canonical (PR #4325 closed
     #4321 ~2.5h before #4355 was filed).
  2. No match (different area) — closed issue in window has no
     title-token / path overlap.
  3. Threshold near miss — closed issue shares one title token (below
     the default threshold of 2) and no paths.
  4. Closed PR with no `Closes #N` — closed issue has no
     ``closedByPullRequestsReferences``; probe still emits the match
     with empty ``closing_pr`` so the agent can read the closed issue
     itself.

Plus pure-python tests for the title tokenizer and the path extractor
fallback, and end-to-end tests for the OR-of-channels rule (title-only,
path-only, both).

Loads the helper module via ``importlib.util.spec_from_file_location``
because the filename starts with an underscore.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "_check_near_duplicate_issue.py"


def _import_probe_module():
    """Load the near-duplicate-issue probe as ``check_near_duplicate_issue``."""
    spec = importlib.util.spec_from_file_location(
        "check_near_duplicate_issue", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_near_duplicate_issue"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def probe_module():
    return _import_probe_module()


# ─── Layer 1: Title tokenization ──────────────────────────────────────────


def test_tokenize_title_drops_short_tokens(probe_module):
    """Tokens of length <3 are dropped."""
    out = probe_module.tokenize_title("a b cd efg hij")
    assert out == {"efg", "hij"}


def test_tokenize_title_drops_stopwords(probe_module):
    """Common stopwords (``feat``, ``the``, ``via``) are filtered."""
    out = probe_module.tokenize_title("feat(dx): the splitter via the new helper")
    # feat, the, via are stopwords; (dx) and : strip via the regex.
    # ``dx`` is a stopword (commit-type tier-1).
    # ``new`` is a stopword (filler).
    assert out == {"splitter", "helper"}


def test_tokenize_title_lowercases(probe_module):
    """Tokens are lowercased — ``Drain`` and ``DRAIN`` collapse."""
    out_a = probe_module.tokenize_title("Drain Splitter")
    out_b = probe_module.tokenize_title("DRAIN splitter")
    assert out_a == out_b == {"drain", "splitter"}


def test_tokenize_title_preserves_underscores(probe_module):
    """Underscores are token-internal (``drain_splitter`` is one token)."""
    out = probe_module.tokenize_title("Run drain_splitter_carry_forward_clusters")
    assert out == {"run", "drain_splitter_carry_forward_clusters"}


def test_tokenize_title_drops_numeric_only(probe_module):
    """Pure-digit tokens (``2026``) are dropped."""
    out = probe_module.tokenize_title("Audit 2026 widget_loader rollouts")
    assert out == {"audit", "widget_loader", "rollouts"}


def test_tokenize_title_canonical_pair_4321_vs_4355(probe_module):
    """The canonical pair shares enough non-stopword tokens.

    #4321: ``feat(dx): generic splitter-carry-forward drain helper to
            close audit thresholds post-deploy``
    #4355: ``tooling: drain_splitter_carry_forward_clusters.py canonical
            drain helper``
    Shared tokens (after stopword + length filtering): ``drain``,
    ``helper``, plus ``carry`` and ``forward`` (from #4321's hyphenated
    splitter-carry-forward and #4355's underscored form). At least
    2-token overlap → title channel clears at default threshold.
    """
    a = probe_module.tokenize_title(
        "feat(dx): generic splitter-carry-forward drain helper to close "
        "audit thresholds post-deploy"
    )
    b = probe_module.tokenize_title(
        "tooling: drain_splitter_carry_forward_clusters.py canonical drain helper"
    )
    overlap = a & b
    # ``drain`` + ``helper`` are shared; ``carry`` and ``forward`` are
    # in #4321 (hyphenated) but NOT in #4355 (where ``carry``,
    # ``forward``, ``clusters`` are subtokens of an underscored
    # identifier and so collapse into the single token
    # ``drain_splitter_carry_forward_clusters``).
    assert "drain" in overlap
    assert "helper" in overlap
    assert len(overlap) >= 2


# ─── Layer 2: Path extraction ─────────────────────────────────────────────


def test_extract_target_paths_basic(probe_module):
    """Paths under conventional roots are extracted from narrative."""
    body = (
        "## Proposal\n\n"
        "Add `scripts/drain_splitter_carry_forward_clusters.py` per "
        "the existing pattern in `packages/scraper-framework/src/`.\n"
    )
    paths = probe_module.extract_target_paths(body)
    assert "scripts/drain_splitter_carry_forward_clusters.py" in paths


def test_extract_target_paths_drops_search_context(probe_module):
    """Paths cited only on Verify: lines are NOT classified as target.

    Delegates to the shared classifier from
    ``_check_shipped_pr_extract_files.py`` — the same target-vs-search
    semantics. A path that only shows up inside a Verify clause's
    grep / pytest invocation is search-context, not target-context.
    """
    body = (
        "## Acceptance\n\n"
        "- [ ] Some change.\n"
        "  Verify: `grep -n widget scripts/foo.sh`\n"
    )
    paths = probe_module.extract_target_paths(body)
    # The path appears only inside a Verify shell invocation, so the
    # extractor's target-context list excludes it.
    assert "scripts/foo.sh" not in paths


def test_extract_target_paths_empty_body(probe_module):
    """Empty body yields empty set."""
    assert probe_module.extract_target_paths("") == set()


def test_extract_target_paths_canonical_pair(probe_module):
    """#4321 and #4355 both cite the same script path in narrative."""
    body_4321 = (
        "## Proposal\n\n"
        "Add a generic helper script "
        "`scripts/drain_splitter_carry_forward_clusters.py` (or wire the "
        "same logic into `scripts/reingest_from_s3.py` ...).\n"
    )
    body_4355 = (
        "## Proposal\n\n"
        "Create `scripts/drain_splitter_carry_forward_clusters.py` "
        "(one-off, scraper-framework venv) ...\n"
    )
    a = probe_module.extract_target_paths(body_4321)
    b = probe_module.extract_target_paths(body_4355)
    assert "scripts/drain_splitter_carry_forward_clusters.py" in a & b


# ─── Layer 3: score_candidate ─────────────────────────────────────────────


def test_score_candidate_title_only_clears(probe_module):
    """Title overlap clears, path overlap empty → channel=title."""
    out = probe_module.score_candidate(
        current_title="feat(dx): drain splitter helper",
        current_body="Some prose without paths.",
        candidate_title="tooling: drain splitter canonical helper",
        candidate_body="Different prose, no shared paths.",
        title_threshold=2,
        path_threshold=1,
    )
    assert out is not None
    channel, overlap = out
    assert channel == "title"
    assert "drain" in overlap
    assert "splitter" in overlap


def test_score_candidate_path_only_clears(probe_module):
    """Path overlap clears, title overlap empty → channel=path."""
    out = probe_module.score_candidate(
        current_title="entirely unrelated",
        current_body="Cite `scripts/widget_loader.py` here.",
        candidate_title="completely different topic",
        candidate_body="Also touches `scripts/widget_loader.py`.",
        title_threshold=2,
        path_threshold=1,
    )
    assert out is not None
    channel, overlap = out
    assert channel == "path"
    assert "scripts/widget_loader.py" in overlap


def test_score_candidate_both_channels_clear(probe_module):
    """Both channels clear → channel=both, overlap is union."""
    out = probe_module.score_candidate(
        current_title="feat(dx): drain splitter helper",
        current_body="Cite `scripts/foo.sh` here.",
        candidate_title="tooling: drain splitter canonical helper",
        candidate_body="Also touches `scripts/foo.sh`.",
        title_threshold=2,
        path_threshold=1,
    )
    assert out is not None
    channel, overlap = out
    assert channel == "both"
    assert "drain" in overlap
    assert "splitter" in overlap
    assert "scripts/foo.sh" in overlap


def test_score_candidate_below_thresholds_returns_none(probe_module):
    """Neither channel clears → None."""
    out = probe_module.score_candidate(
        current_title="feat(dx): widget refactor",
        current_body="Some prose.",
        candidate_title="fix(api): unrelated bug",
        candidate_body="Different prose.",
        title_threshold=2,
        path_threshold=1,
    )
    assert out is None


def test_score_candidate_one_title_token_below_default(probe_module):
    """One-token title overlap is below default threshold of 2 → None."""
    out = probe_module.score_candidate(
        current_title="feat(scrapers): widget loader fix",
        current_body="Some prose.",
        candidate_title="feat(api): widget unrelated change",
        candidate_body="Different prose, no paths.",
        title_threshold=2,
        path_threshold=1,
    )
    # Only ``widget`` overlaps (and ``feat`` is a stopword) — below
    # default threshold of 2.
    assert out is None


# ─── Layer 4: End-to-end probe ────────────────────────────────────────────


def _make_gh_mock(responses):
    """Build a subprocess.run mock that dispatches by gh argv prefix.

    ``responses`` is a dict mapping argv-tuple prefix to (returncode, stdout).
    """

    def _run(args, **_kwargs):
        argv = tuple(args[1:])
        for key, (rc, out) in responses.items():
            if argv[: len(key)] == key:
                return mock.Mock(returncode=rc, stdout=out, stderr="")
        return mock.Mock(returncode=1, stdout="", stderr="")

    return _run


def _canonical_4321_4355_responses(*, closing_pr: int | None = 4325):
    """Build the gh mock responses for the #4321 ↔ #4355 canonical case.

    ``closing_pr=None`` simulates AC scenario (d) — closed issue with
    no `Closes #N` keyword (empty closedByPullRequestsReferences).
    """
    body_4321 = (
        "## Proposal\n\n"
        "Add a generic helper script "
        "`scripts/drain_splitter_carry_forward_clusters.py`.\n"
    )
    refs_json: str
    if closing_pr is not None:
        refs_json = (
            f'{{"closedByPullRequestsReferences": '
            f'[{{"number": {closing_pr}, "state": "MERGED"}}], '
            f'"title": "feat(dx): generic splitter-carry-forward drain helper", '
            f'"body": {repr(body_4321).replace(chr(39), chr(34))}, '
            f'"closedAt": "2026-05-08T17:07:58Z"}}'
        )
    else:
        refs_json = (
            f'{{"closedByPullRequestsReferences": [], '
            f'"title": "feat(dx): generic splitter-carry-forward drain helper", '
            f'"body": {repr(body_4321).replace(chr(39), chr(34))}, '
            f'"closedAt": "2026-05-08T17:07:58Z"}}'
        )
    return {
        # gh issue list --search closed:from..to → returns #4321
        ("issue", "list"): (0, '[{"number": 4321}]'),
        # gh issue view 4321 → title + body + closedAt + closing PR
        ("issue", "view", "4321"): (0, refs_json),
    }


def test_probe_canonical_match_4321_to_4355(probe_module):
    """AC scenario (a): match within window — #4355 → #4321 (PR #4325).

    #4355's title shares ``drain``/``helper``; bodies share
    ``scripts/drain_splitter_carry_forward_clusters.py``. Probe must
    emit (4321, 4325, "both", [...]).
    """
    current_body = (
        "## Proposal\n\n"
        "Create `scripts/drain_splitter_carry_forward_clusters.py` "
        "(one-off, scraper-framework venv).\n"
    )
    responses = _canonical_4321_4355_responses(closing_pr=4325)
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            current_title=(
                "tooling: drain_splitter_carry_forward_clusters.py "
                "canonical drain helper"
            ),
            current_body=current_body,
            current_created_at=datetime(2026, 5, 8, 19, 49, 25, tzinfo=timezone.utc),
            current_issue=4355,
            repo="judgemind/judgemind",
        )
    assert hit is not None
    closed_issue, closing_pr, channel, overlap_list = hit
    assert closed_issue == 4321
    assert closing_pr == 4325
    # Both title and path overlap fired → channel=both.
    assert channel == "both"
    # Path overlap is the load-bearing identifier.
    assert "scripts/drain_splitter_carry_forward_clusters.py" in overlap_list


def test_probe_no_match_different_area(probe_module):
    """AC scenario (b): no match — closed issue is in a different area.

    Closed issue is about a frontend bug; current issue is about
    scraper backfill. No title-token overlap, no path overlap.
    """
    current_body = (
        "## Proposal\n\nRun `scripts/backfill_widget_metadata.py` against dev.\n"
    )
    responses = {
        ("issue", "list"): (0, '[{"number": 4400}]'),
        ("issue", "view", "4400"): (
            0,
            '{"title": "fix(web): tooltip alignment in legal-research search bar",'
            ' "body": "## Problem\\n\\nThe `Tooltip` component overlaps the'
            " `SearchBar` on narrow viewports — see `packages/web/src/"
            'components/Tooltip.tsx`.",'
            ' "closedAt": "2026-05-07T10:00:00Z",'
            ' "closedByPullRequestsReferences": [{"number": 4401, "state": "MERGED"}]}',
        ),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            current_title="feat(scrapers): backfill widget_metadata for SF county",
            current_body=current_body,
            current_created_at=datetime(2026, 5, 8, 19, 49, 25, tzinfo=timezone.utc),
            current_issue=4520,
            repo="judgemind/judgemind",
        )
    assert hit is None


def test_probe_threshold_near_miss(probe_module):
    """AC scenario (c): one shared title token (below default threshold).

    Closed issue shares ONE title token with the current issue and
    no paths. Default threshold of 2 → no match.
    """
    current_body = "## Proposal\n\nFix the widget bug.\n"
    responses = {
        ("issue", "list"): (0, '[{"number": 4400}]'),
        ("issue", "view", "4400"): (
            0,
            '{"title": "fix(api): widget rendering refactor",'
            ' "body": "## Problem\\n\\nNon-overlapping prose.",'
            ' "closedAt": "2026-05-07T10:00:00Z",'
            ' "closedByPullRequestsReferences": [{"number": 4401, "state": "MERGED"}]}',
        ),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            current_title="feat(scrapers): widget extraction",
            current_body=current_body,
            current_created_at=datetime(2026, 5, 8, 19, 49, 25, tzinfo=timezone.utc),
            current_issue=4520,
            repo="judgemind/judgemind",
        )
    assert hit is None


def test_probe_closed_with_no_closing_pr(probe_module):
    """AC scenario (d): closed PR with no `Closes #N` → closing_pr=None.

    Closed issue has empty ``closedByPullRequestsReferences`` (manual
    close, or PR didn't include `Closes #N`). The probe still emits
    a match — the agent can read the closed issue itself even
    without a PR reference.
    """
    current_body = (
        "## Proposal\n\nCreate `scripts/drain_splitter_carry_forward_clusters.py`.\n"
    )
    responses = _canonical_4321_4355_responses(closing_pr=None)
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            current_title=(
                "tooling: drain_splitter_carry_forward_clusters.py "
                "canonical drain helper"
            ),
            current_body=current_body,
            current_created_at=datetime(2026, 5, 8, 19, 49, 25, tzinfo=timezone.utc),
            current_issue=4355,
            repo="judgemind/judgemind",
        )
    assert hit is not None
    closed_issue, closing_pr, channel, overlap_list = hit
    assert closed_issue == 4321
    assert closing_pr is None
    assert channel == "both"


def test_probe_excludes_self_from_candidates(probe_module):
    """Current issue is dropped from the candidate list.

    The recently-closed search may surface the current issue itself
    if the issue was just closed and re-queried; ``list_recently_closed``
    must drop ``current_issue`` from the results.
    """
    responses = {
        # Mock returns the current issue as the only candidate.
        ("issue", "list"): (0, '[{"number": 4520}]'),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            current_title="anything",
            current_body="anything",
            current_created_at=datetime(2026, 5, 8, 19, 49, 25, tzinfo=timezone.utc),
            current_issue=4520,
            repo="judgemind/judgemind",
        )
    assert hit is None


def test_probe_no_candidates_in_window(probe_module):
    """Empty candidate list → None."""
    responses = {
        ("issue", "list"): (0, "[]"),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            current_title="anything",
            current_body="anything",
            current_created_at=datetime(2026, 5, 8, 19, 49, 25, tzinfo=timezone.utc),
            current_issue=4520,
            repo="judgemind/judgemind",
        )
    assert hit is None


def test_probe_search_api_error_exits_clean(probe_module):
    """gh issue list rc=1 → probe returns None (no crash)."""
    responses = {("issue", "list"): (1, "")}
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            current_title="anything",
            current_body="anything",
            current_created_at=datetime(2026, 5, 8, 19, 49, 25, tzinfo=timezone.utc),
            current_issue=4520,
            repo="judgemind/judgemind",
        )
    assert hit is None


def test_probe_malformed_search_json_exits_clean(probe_module):
    """gh issue list returns malformed JSON → probe returns None."""
    responses = {("issue", "list"): (0, "not-json {{")}
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            current_title="anything",
            current_body="anything",
            current_created_at=datetime(2026, 5, 8, 19, 49, 25, tzinfo=timezone.utc),
            current_issue=4520,
            repo="judgemind/judgemind",
        )
    assert hit is None


def test_probe_skips_candidate_with_view_failure(probe_module):
    """gh issue view rc=1 for a candidate → skip, continue to next."""
    body_4322 = "## Proposal\n\nFix `scripts/baz.py`.\n"
    body_4321 = "## Proposal\n\nFix `scripts/baz.py` (similar but different).\n"
    responses = {
        # Two candidates returned.
        ("issue", "list"): (0, '[{"number": 4322}, {"number": 4321}]'),
        # First fails.
        ("issue", "view", "4322"): (1, ""),
        # Second succeeds.
        ("issue", "view", "4321"): (
            0,
            '{"title": "fix: baz refactor",'
            f' "body": {repr(body_4321).replace(chr(39), chr(34))},'
            ' "closedAt": "2026-05-07T10:00:00Z",'
            ' "closedByPullRequestsReferences": [{"number": 4399, "state": "MERGED"}]}',
        ),
    }
    current_body = body_4322
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            current_title="fix: another baz refactor",
            current_body=current_body,
            current_created_at=datetime(2026, 5, 8, 19, 49, 25, tzinfo=timezone.utc),
            current_issue=4520,
            repo="judgemind/judgemind",
        )
    # First candidate fails, second matches via path overlap.
    assert hit is not None
    closed_issue, _pr, channel, overlap_list = hit
    assert closed_issue == 4321
    assert channel == "path"
    assert "scripts/baz.py" in overlap_list


# ─── Calibration sanity check ─────────────────────────────────────────────


def test_calibration_canonical_pair_clears(probe_module):
    """Calibration: the canonical #4321 ↔ #4355 pair clears the threshold.

    This is the load-bearing positive case — if the OR-of-channels rule
    can't catch this exact recurrence the script was filed for, it's
    not useful. Default thresholds (title=2, path=1) MUST fire on the
    canonical pair.
    """
    out = probe_module.score_candidate(
        current_title=(
            "tooling: drain_splitter_carry_forward_clusters.py canonical drain helper"
        ),
        current_body=(
            "Create `scripts/drain_splitter_carry_forward_clusters.py` "
            "(one-off, scraper-framework venv).\n"
        ),
        candidate_title=(
            "feat(dx): generic splitter-carry-forward drain helper to close "
            "audit thresholds post-deploy"
        ),
        candidate_body=(
            "Add a generic helper script "
            "`scripts/drain_splitter_carry_forward_clusters.py`.\n"
        ),
        title_threshold=2,
        path_threshold=1,
    )
    assert out is not None
    channel, overlap = out
    # Both channels should clear — the title shares ``drain``/``helper``
    # and the body cites the same script path.
    assert channel == "both"
    assert "drain" in overlap
    assert "helper" in overlap
    assert "scripts/drain_splitter_carry_forward_clusters.py" in overlap


def test_calibration_unrelated_recent_pair_does_not_clear(probe_module):
    """Calibration: unrelated issues from same window do NOT clear.

    Two issues in different areas (frontend vs scraper) with no shared
    path or title token must NOT clear, even if both were filed in the
    same week.
    """
    out = probe_module.score_candidate(
        current_title="feat(scrapers): backfill widget_metadata for SF county",
        current_body=("Run `scripts/backfill_widget_metadata.py` against dev.\n"),
        candidate_title="fix(web): tooltip alignment in legal-research search bar",
        candidate_body=(
            "The `Tooltip` component overlaps the `SearchBar` on narrow "
            "viewports — see `packages/web/src/components/Tooltip.tsx`.\n"
        ),
        title_threshold=2,
        path_threshold=1,
    )
    assert out is None
