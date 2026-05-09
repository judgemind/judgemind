# venv: none
"""Tests for ``scripts/_check_shipped_pr_lineage_probe.py``.

The lineage-probe helper (issue #4515) is the third channel in
check-shipped-pr.sh's pipeline (after the literal-Verify channel and
before the path-overlap channel). It catches the canonical
retrospective-duplicate shape: two issues filed from the same parent
retrospective describe the same lesson, one already merged.

These tests cover three layers:

  1. **Lineage parent extraction** — the ``extract_lineage_parents()``
     parser. Pulls ``Found by:.*retrospective on #N`` references out of
     an issue body, deduplicating and preserving first-mention order.

  2. **Backtick-token extraction** — the ``extract_backtick_tokens()``
     helper. Extracts ``\\`token\\``-wrapped identifiers of length ≥3,
     filtering stopwords. The set intersection of the current-issue
     tokens and the candidate-PR-body tokens is the load-bearing
     precision signal.

  3. **End-to-end probe** — the ``probe()`` orchestrator with mocked
     gh subprocess calls. Asserts the documented exit codes / output
     shapes for the canonical positive case (#4315 ↔ PR #4345 ↔
     sibling #4322), the precision case (sibling describes a different
     lesson with no token overlap), and the negative cases (no
     retrospective-on lineage, sibling has no merging PR, sibling's PR
     is unmerged or merged onto a feature branch).

Loads the helper module via ``importlib.util.spec_from_file_location``
because the filename starts with an underscore — the standard
``import scripts._check_...`` path doesn't apply (``scripts/`` is not
a package).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "_check_shipped_pr_lineage_probe.py"


def _import_probe_module():
    """Load the lineage-probe helper as ``check_shipped_pr_lineage_probe``."""
    spec = importlib.util.spec_from_file_location(
        "check_shipped_pr_lineage_probe", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_shipped_pr_lineage_probe"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def probe_module():
    return _import_probe_module()


# ─── Layer 1: Lineage parent extraction ───────────────────────────────────


def test_extract_canonical_form(probe_module):
    """``Found by: retrospective on #N`` (canonical) is captured."""
    body = "## Background\n\nFound by: retrospective on #4304.\n"
    assert probe_module.extract_lineage_parents(body) == [4304]


def test_extract_no_colon(probe_module):
    """``Found by retrospective on #N`` (no colon) is also captured."""
    body = "Found by retrospective on #4304\n"
    assert probe_module.extract_lineage_parents(body) == [4304]


def test_extract_lowercase(probe_module):
    """``found by: retrospective on #N`` (lowercase) is captured (regex i flag)."""
    body = "found by: retrospective on #4304\n"
    assert probe_module.extract_lineage_parents(body) == [4304]


def test_extract_multiple_parents(probe_module):
    """Multiple ``Found by:`` lines yield all parents in first-mention order."""
    body = (
        "## Background\n"
        "\n"
        "Found by: retrospective on #4304.\n"
        "Also found by: retrospective on #4250.\n"
    )
    assert probe_module.extract_lineage_parents(body) == [4304, 4250]


def test_extract_deduplicates(probe_module):
    """Same parent mentioned twice yields one entry."""
    body = (
        "Found by: retrospective on #4304.\n(Also Found by: retrospective on #4304.)\n"
    )
    assert probe_module.extract_lineage_parents(body) == [4304]


def test_extract_empty_when_no_match(probe_module):
    """Body without retrospective lineage yields empty list."""
    body = "## Description\n\nA bug fix.\n"
    assert probe_module.extract_lineage_parents(body) == []


def test_extract_ignores_unrelated_retrospective(probe_module):
    """``retrospective on`` without the ``Found by`` prefix is NOT a parent.

    The probe is conservative — only the canonical ``Found by: retrospective
    on #N`` shape counts. Free-form prose like "the retrospective on #4304
    showed..." is too weak a signal to base a duplicate-shipped match on.
    """
    body = "The retrospective on #4304 showed that we should rename foo.\n"
    assert probe_module.extract_lineage_parents(body) == []


# ─── Layer 2: Backtick-token extraction ───────────────────────────────────


def test_extract_simple_backticks(probe_module):
    """Single backtick-wrapped tokens are extracted."""
    body = "Update `_DATACLASS_SCOPE` and `*SplitRuling`.\n"
    assert probe_module.extract_backtick_tokens(body) == {
        "_DATACLASS_SCOPE",
        "*SplitRuling",
    }


def test_extract_filters_short_tokens(probe_module):
    """Tokens of length <3 are dropped."""
    body = "The `=` sign and `a` and `xyz`.\n"
    # `=` (1 char) and `a` (1 char) are below the length-3 floor; only
    # `xyz` (3 chars) survives.
    assert probe_module.extract_backtick_tokens(body) == {"xyz"}


def test_extract_filters_stopwords(probe_module):
    """Common stopwords (``main``, ``true``, etc.) are filtered."""
    body = "On `main`, set the value to `true` not `false`. Also `widget`.\n"
    assert probe_module.extract_backtick_tokens(body) == {"widget"}


def test_extract_case_sensitive(probe_module):
    """Case-sensitive — ``DATACLASS`` and ``dataclass`` are different tokens."""
    body = "Use `DATACLASS` for constants and `dataclass` for the decorator.\n"
    assert probe_module.extract_backtick_tokens(body) == {"DATACLASS", "dataclass"}


def test_extract_empty_body(probe_module):
    """Empty body yields empty set."""
    assert probe_module.extract_backtick_tokens("") == set()


def test_extract_preserves_internal_punctuation(probe_module):
    """Tokens with ``::``, ``-``, ``_``, ``/`` are kept as-is."""
    body = (
        "The path `scripts/foo.sh` and the symbol `module::Klass` and "
        "the kebab-case `my-flag` and the snake `my_var`.\n"
    )
    assert probe_module.extract_backtick_tokens(body) == {
        "scripts/foo.sh",
        "module::Klass",
        "my-flag",
        "my_var",
    }


def test_extract_drops_empty_backtick_spans(probe_module):
    """`` `` `` (empty backticks) are filtered."""
    body = "Empty: `` and not-empty: `widget`.\n"
    # The regex requires length-3 minimum, so empty spans never match.
    assert probe_module.extract_backtick_tokens(body) == {"widget"}


def test_extract_handles_multiple_per_line(probe_module):
    """Multiple backtick spans on one line each yield a token."""
    body = "Refs `alpha` and `beta` and `gamma`.\n"
    assert probe_module.extract_backtick_tokens(body) == {"alpha", "beta", "gamma"}


# ─── Layer 3: End-to-end probe ────────────────────────────────────────────


def _make_gh_mock(responses):
    """Build a subprocess.run mock that dispatches by gh argv.

    ``responses`` is a dict mapping argv-tuple-suffix to (returncode, stdout).
    The keys match against the SUFFIX of the gh argv, so the test can ignore
    the leading ``gh`` token.
    """

    def _run(args, **_kwargs):
        # args is a list starting with the gh binary. Drop it.
        argv = tuple(args[1:])
        for key, (rc, out) in responses.items():
            # Match prefix - the test response key matches the leading
            # tokens, ignoring later flags like --json or --limit values.
            if argv[: len(key)] == key:
                return mock.Mock(returncode=rc, stdout=out, stderr="")
        # Default: 1, empty (mimics gh exit 1 on a missing route).
        return mock.Mock(returncode=1, stdout="", stderr="")

    return _run


def test_probe_canonical_match_4315(probe_module):
    """End-to-end: #4315 ↔ PR #4345 ↔ sibling #4322 (canonical AC1).

    Issue #4315 has ``Found by: retrospective on #4304.`` and mentions
    ``_DATACLASS_SCOPE`` + ``*SplitRuling`` in backticks. Sibling #4322
    is closed by PR #4345, whose body also mentions ``_DATACLASS_SCOPE``
    in backticks. Probe must emit (4345, 4322, [..._DATACLASS_SCOPE...]).
    """
    issue_body = (
        "## Description\n"
        "\n"
        "When a new `*SplitRuling` dataclass is missing from `_DATACLASS_SCOPE`,\n"
        "the check should emit a paste-ready hint.\n"
        "\n"
        "## Background\n"
        "\n"
        "Found by: retrospective on #4304.\n"
    )
    pr_body = (
        "## Summary\n"
        "\n"
        "Make `_DATACLASS_SCOPE` self-diagnosing when a new `*SplitRuling`\n"
        "dataclass is missing.\n"
        "\n"
        "Closes #4322\n"
    )
    responses = {
        # gh search issues "...retrospective on #4304..." → returns sibling #4322
        ("search", "issues"): (
            0,
            '[{"number": 4322}]',
        ),
        # gh issue view 4322 --json closedByPullRequestsReferences → PR #4345
        ("issue", "view", "4322"): (
            0,
            '{"closedByPullRequestsReferences": [{"number": 4345, "state": "MERGED"}]}',
        ),
        # gh pr view 4345 --json body,mergedAt,baseRefName → eligible
        ("pr", "view", "4345"): (
            0,
            '{"body": '
            + repr(pr_body).replace("'", '"')
            + ', "mergedAt": "2026-05-08T18:58:05Z", "baseRefName": "main"}',
        ),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4315
        )
    assert hit is not None
    pr, sibling, identifiers = hit
    assert pr == 4345
    assert sibling == 4322
    # Both identifiers are present in both bodies → both in the overlap.
    assert "_DATACLASS_SCOPE" in identifiers
    assert "*SplitRuling" in identifiers


def test_probe_no_lineage_returns_none(probe_module):
    """Issue body without ``Found by: retrospective on #N`` exits 1.

    Issues without the retrospective-lineage signal must fall through to
    the path-overlap channel. The probe extracts zero parents and returns
    None unconditionally — no gh calls are made.
    """
    issue_body = "## Description\n\nA bug fix in `widget_loader`.\n"
    with mock.patch("subprocess.run") as m:
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4515
        )
        # No gh subprocess calls because there's nothing to look up.
        m.assert_not_called()
    assert hit is None


def test_probe_no_token_overlap_returns_none(probe_module):
    """Sibling describes a DIFFERENT lesson (no identifier overlap) → exit 1.

    AC2: precision case. The current issue has retrospective lineage AND
    a sibling has shipped, but the sibling's PR body does not share any
    backtick-wrapped identifiers with the current issue. The lineage
    channel must not fire — fall through to path-overlap.
    """
    issue_body = (
        "Update `_DATACLASS_SCOPE` to include the new entry.\n"
        "\n"
        "Found by: retrospective on #4304.\n"
    )
    pr_body = (
        "## Summary\n\n"
        "Tighten the `regex_validator` for trailing whitespace.\n\n"
        "Closes #4399\n"
    )
    responses = {
        ("search", "issues"): (0, '[{"number": 4399}]'),
        ("issue", "view", "4399"): (
            0,
            '{"closedByPullRequestsReferences": [{"number": 4400, "state": "MERGED"}]}',
        ),
        ("pr", "view", "4400"): (
            0,
            '{"body": '
            + repr(pr_body).replace("'", '"')
            + ', "mergedAt": "2026-05-08T18:58:05Z", "baseRefName": "main"}',
        ),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4515
        )
    assert hit is None


def test_probe_sibling_has_no_merging_pr(probe_module):
    """Sibling closed without a merging PR → skip, exit 1 if no other siblings.

    Sibling closed manually (``--reason completed``) has empty
    ``closedByPullRequestsReferences``. The probe must skip the sibling
    and exit 1 when there are no other candidates.
    """
    issue_body = "Update `_DATACLASS_SCOPE`.\n\nFound by: retrospective on #4304.\n"
    responses = {
        ("search", "issues"): (0, '[{"number": 4500}]'),
        ("issue", "view", "4500"): (
            0,
            '{"closedByPullRequestsReferences": []}',
        ),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4515
        )
    assert hit is None


def test_probe_sibling_pr_unmerged(probe_module):
    """Sibling's PR is unmerged (mergedAt null) → skip, exit 1."""
    issue_body = "Update `_DATACLASS_SCOPE`.\n\nFound by: retrospective on #4304.\n"
    pr_body = "Body mentions `_DATACLASS_SCOPE`.\n\nCloses #4322"
    responses = {
        ("search", "issues"): (0, '[{"number": 4322}]'),
        ("issue", "view", "4322"): (
            0,
            '{"closedByPullRequestsReferences": [{"number": 4345, "state": "MERGED"}]}',
        ),
        ("pr", "view", "4345"): (
            0,
            '{"body": '
            + repr(pr_body).replace("'", '"')
            + ', "mergedAt": null, "baseRefName": "main"}',
        ),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4515
        )
    assert hit is None


def test_probe_sibling_pr_feature_branch(probe_module):
    """Sibling's PR merged onto a feature branch (not main) → skip, exit 1."""
    issue_body = "Update `_DATACLASS_SCOPE`.\n\nFound by: retrospective on #4304.\n"
    pr_body = "Body mentions `_DATACLASS_SCOPE`.\n\nCloses #4322"
    responses = {
        ("search", "issues"): (0, '[{"number": 4322}]'),
        ("issue", "view", "4322"): (
            0,
            '{"closedByPullRequestsReferences": [{"number": 4345, "state": "MERGED"}]}',
        ),
        ("pr", "view", "4345"): (
            0,
            '{"body": '
            + repr(pr_body).replace("'", '"')
            + ', "mergedAt": "2026-05-08T18:58:05Z", "baseRefName": "feature/wip"}',
        ),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4515
        )
    assert hit is None


def test_probe_excludes_self_from_siblings(probe_module):
    """Current-issue number is excluded from the sibling list.

    GitHub's search may return the current issue itself in the results
    when the issue body's ``Found by:`` line is what triggered the
    search match. The probe must drop self-matches — an issue's
    duplicate cannot be itself.
    """
    issue_body = "Update `_DATACLASS_SCOPE`.\n\nFound by: retrospective on #4304.\n"
    # The search returns ONLY the current issue — no siblings to vet.
    responses = {
        ("search", "issues"): (0, '[{"number": 4515}]'),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4515
        )
    assert hit is None


def test_probe_no_backtick_tokens_in_body(probe_module):
    """Issue body has retrospective lineage but no backtick tokens → exit 1.

    An issue with zero backtick-wrapped identifiers cannot clear the
    single-token overlap threshold no matter what the sibling PR shipped.
    The probe returns None without making gh calls.
    """
    issue_body = (
        "Update the dataclass scope to include the new entry.\n\n"
        "Found by: retrospective on #4304.\n"
    )
    with mock.patch("subprocess.run") as m:
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4515
        )
        # No gh calls — empty current-tokens short-circuits before search.
        m.assert_not_called()
    assert hit is None


def test_probe_search_api_error_exits_clean(probe_module):
    """gh search exits 1 → probe exits 1 (no crash)."""
    issue_body = "Update `_DATACLASS_SCOPE`.\n\nFound by: retrospective on #4304.\n"
    responses = {
        # gh search issues returns rc=1
        ("search", "issues"): (1, ""),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4515
        )
    assert hit is None


def test_probe_malformed_search_json_exits_clean(probe_module):
    """gh search returns malformed JSON → probe exits 1 (no crash)."""
    issue_body = "Update `_DATACLASS_SCOPE`.\n\nFound by: retrospective on #4304.\n"
    responses = {
        ("search", "issues"): (0, "not-json {{"),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4515
        )
    assert hit is None


def test_probe_first_match_wins_when_multiple_siblings(probe_module):
    """When multiple siblings have shipped, the first match is emitted.

    Search returns siblings in most-recently-updated order; the probe
    walks them in that order and emits the first identifier-overlap hit.
    """
    issue_body = "Update `_DATACLASS_SCOPE`.\n\nFound by: retrospective on #4304.\n"
    pr_body_first = "Updates `_DATACLASS_SCOPE` self-diagnosis.\n\nCloses #4322"
    responses = {
        # Two siblings — both eventually clear overlap, but the first
        # candidate (4322) is checked first and short-circuits.
        ("search", "issues"): (
            0,
            '[{"number": 4322}, {"number": 4399}]',
        ),
        ("issue", "view", "4322"): (
            0,
            '{"closedByPullRequestsReferences": [{"number": 4345, "state": "MERGED"}]}',
        ),
        ("pr", "view", "4345"): (
            0,
            '{"body": '
            + repr(pr_body_first).replace("'", '"')
            + ', "mergedAt": "2026-05-08T18:58:05Z", "baseRefName": "main"}',
        ),
    }
    with mock.patch("subprocess.run", side_effect=_make_gh_mock(responses)):
        hit = probe_module.probe(
            issue_body, repo="judgemind/judgemind", current_issue=4515
        )
    assert hit is not None
    pr, sibling, _ = hit
    assert pr == 4345
    assert sibling == 4322
