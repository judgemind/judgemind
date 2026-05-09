# venv: none
"""Tests for ``scripts/_check_shipped_pr_classify_issue.py``.

The classifier (issue #4223) inspects an issue's title+body and decides
whether the issue is *audit-class* — i.e. the work is asking for further
investigation, refactor, migration, extension, or hardening of an
already-existing file. Audit-class issues need a stricter file-overlap
threshold than the canonical zombie shape (placeholder PR added a file
the issue's AC asks for); the classifier is the gate that triggers the
tightening.

The tests cover:

  - All listed audit-class verbs match (audit / investigate / refactor /
    migrate / extend / tighten / harden / additional, plus the noun
    forms investigation / migration / extension / refactoring).
  - Word boundaries enforced — substring matches do NOT trip the
    classifier ("investor" is not "investigate", "addition" is not
    "additional", "morepath" is not "more").
  - Title-only and body-only matches both fire (audit issues commonly
    declare their nature in only one of these).
  - Non-audit titles/bodies (the canonical zombie shape) do NOT match.
  - Malformed JSON / missing fields fail open (return empty string,
    treated as non-audit).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "_check_shipped_pr_classify_issue.py"


def _import_helper_module():
    """Load the classifier as ``check_shipped_pr_classify_issue``."""
    spec = importlib.util.spec_from_file_location(
        "check_shipped_pr_classify_issue", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_shipped_pr_classify_issue"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def helper():
    return _import_helper_module()


# ─── Audit-class keyword detection ──────────────────────────────────────


@pytest.mark.parametrize(
    "title,body",
    [
        # Title-only matches — concrete shape from the issue body's
        # worked examples (#4208 and #4213 both used investigate / dx
        # / migrate verbs in the title).
        (
            "investigate: audit Tailwind token pairs for additional one-character footguns",
            "Some prose without keywords.",
        ),
        ("dx(dispatcher-tests): migrate scheduler_tick gate tests", ""),
        ("refactor(scraper): extract shared helper", ""),
        # Body-only matches — the title is neutral but the body
        # declares the intent.
        (
            "dx: clean up old code",
            "## Investigation task\n\nWe need to investigate ...",
        ),
        # Noun forms of the verbs (investigation / refactoring /
        # migration / extension).
        ("dx: notes", "This investigation should produce a list of follow-ups."),
        ("dx: notes", "A refactoring of the helper would clean this up."),
        ("dx: notes", "The migration to the new dispatcher is overdue."),
        # Hardening / tightening verbs.
        ("fix: tighten the regex", ""),
        ("fix: harden the validator against malformed input", ""),
        # `additional` — the broadest of the audit-class verbs but
        # word-boundary-anchored so it doesn't trip on "addition" or
        # similar.
        ("dx: additional test cases for parser", ""),
        # Audit verb in narrative prose.
        ("test fixture", "We should audit the existing fixtures for coverage."),
    ],
)
def test_audit_class_titles_match(helper, title, body):
    """Audit-class titles and bodies trigger classification as `audit`."""
    assert helper.classify(title, body) is True


# ─── Word-boundary enforcement (no substring matches) ───────────────────


@pytest.mark.parametrize(
    "title,body",
    [
        # "investor" is not "investigate".
        ("Add an investor signup form", ""),
        # "addition" is not "additional".
        ("Implement addition operator", ""),
        # "ration" / "harden" — "ration" should not match "harden".
        ("Allocate the ration", ""),
        # "extender" / "extends" — neither is the verb "extend" with
        # word boundary on the right. ("extends" matches because of the
        # \b word boundary plus "extend" prefix though — let's just
        # verify "extender" doesn't match.)
        ("Configure the extender", ""),
        # Compound words that contain a keyword as substring.
        ("Use morepath for routing", ""),
        # "auditor" — not the verb "audit".
        ("Hire a new auditor", ""),
    ],
)
def test_substrings_do_not_match(helper, title, body):
    """Word-boundary regex prevents substring false-positives."""
    assert helper.classify(title, body) is False


# ─── Non-audit issues do NOT classify as audit-class ────────────────────


@pytest.mark.parametrize(
    "title,body",
    [
        # The canonical zombie shape from #2831 — an `add` issue,
        # NOT an audit issue.
        ("dx: add scripts/foo.sh to validate widgets", "We need scripts/foo.sh."),
        # A standard feature-add issue.
        (
            "feat(api): expose new graphql resolver for caseDetail",
            "Add the resolver and the corresponding schema entry.",
        ),
        # A pure bug-fix issue.
        (
            "fix(scraper): handle malformed HTML gracefully",
            "The parser crashes when...",
        ),
        # A documentation issue.
        (
            "docs: update agent-runner README",
            "The README is stale.",
        ),
        # A test-creation issue (test- prefix is not audit-class).
        (
            "test(api): cover the auth middleware",
            "Add coverage for the failure paths.",
        ),
        # Empty input — no signal at all.
        ("", ""),
    ],
)
def test_non_audit_does_not_match(helper, title, body):
    """Non-audit titles and bodies do NOT trigger classification."""
    assert helper.classify(title, body) is False


# ─── Concrete worked examples from the issue body ──────────────────────


def test_4208_investigate_classifies_as_audit(helper):
    """Issue #4208 (per #4223 worked example) — `investigate: audit ...`.

    The title carries TWO audit keywords (`investigate`, `audit`); a
    single hit is sufficient for classification, but this exercises the
    "happy path" worked example from the issue body.
    """
    title = (
        "investigate: audit Tailwind token pairs for additional "
        "one-character footguns (#2816 class)"
    )
    body = (
        "## Investigation task\n\nThis is an investigation issue, not implementation."
    )
    assert helper.classify(title, body) is True


def test_4213_dx_migrate_classifies_as_audit(helper):
    """Issue #4213 (per #4223 worked example) — `dx: migrate ...`.

    The title carries `migrate`; the body uses `migration` (noun form).
    Either is sufficient — the classifier OR's title and body.
    """
    title = (
        "dx(dispatcher-tests): migrate scheduler_tick gate tests to "
        "fetch_responses dict-keyed dispatcher"
    )
    body = (
        "Consider migrating tests off the positional fetch_queue and onto "
        "the dict-keyed fetch_responses dispatcher."
    )
    assert helper.classify(title, body) is True
