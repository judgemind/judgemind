# venv: none
"""Tests for ``scripts/_check_shipped_pr_closes_other.py``.

The helper is the closes-other-issue filter for ``check-shipped-pr.sh``
(issue #4327). It reads ``gh pr view --json body`` JSON on stdin and the
current issue number from ``CHECK_SHIPPED_ISSUE_NUM`` env var, and exits
0 ("filter this candidate out") only when EVERY closing-keyword
reference in the PR body points at an issue OTHER than the one being
checked.

The canonical placeholder PR (empty body, no closing keywords) must
NOT be filtered out — that is the exact zombie-issue shape the script
was built to catch (#2831 ↔ PR #3229).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "_check_shipped_pr_closes_other.py"


def _import_helper_module():
    """Load the helper script as ``check_shipped_pr_closes_other``."""
    spec = importlib.util.spec_from_file_location(
        "check_shipped_pr_closes_other", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_shipped_pr_closes_other"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def helper():
    return _import_helper_module()


# ─── extract_closed_issue_numbers ─────────────────────────────────────


def test_extract_basic_closes_keyword(helper):
    """`Closes #N` is the canonical form."""
    assert helper.extract_closed_issue_numbers("Closes #4317") == {4317}


def test_extract_all_nine_verbs_case_insensitive(helper):
    """All 9 GitHub closing keywords (close/closes/closed/fix/fixes/
    fixed/resolve/resolves/resolved) are recognized case-insensitively.
    """
    body = (
        "close #1\n"
        "Closes #2\n"
        "CLOSED #3\n"
        "fix #4\n"
        "Fixes #5\n"
        "FIXED #6\n"
        "resolve #7\n"
        "Resolves #8\n"
        "RESOLVED #9\n"
    )
    assert helper.extract_closed_issue_numbers(body) == {1, 2, 3, 4, 5, 6, 7, 8, 9}


def test_extract_owner_repo_prefix(helper):
    """`Closes owner/repo#N` is the cross-repo form GitHub recognizes.

    Empirically rare in this repo but the helper supports it for
    completeness — the GitHub auto-close API treats this form
    identically to the bare ``#N`` form.
    """
    body = "Closes judgemind/judgemind#4317"
    assert helper.extract_closed_issue_numbers(body) == {4317}


def test_extract_full_url_form(helper):
    """`Closes https://github.com/owner/repo/issues/N` is the long form
    GitHub also accepts.
    """
    body = "Closes https://github.com/judgemind/judgemind/issues/4317"
    assert helper.extract_closed_issue_numbers(body) == {4317}


def test_extract_with_colon(helper):
    """`Closes: #N` (with colon) is sometimes used; GitHub accepts it."""
    body = "Closes: #4317"
    assert helper.extract_closed_issue_numbers(body) == {4317}


def test_extract_multiple_keywords_in_one_body(helper):
    """A body with multiple closing keywords returns all referenced
    issues. The PR-#3426 case from #4317's reproduction has only one
    (`Closes #3424`) but multi-issue PRs do exist.
    """
    body = "## Summary\n\nLots of work.\n\nCloses #100\nFixes #200\nResolves #300\n"
    assert helper.extract_closed_issue_numbers(body) == {100, 200, 300}


def test_extract_empty_body(helper):
    """Empty body returns empty set — the canonical placeholder-PR
    shape that #2831 ↔ #3229 motivates the check from.
    """
    assert helper.extract_closed_issue_numbers("") == set()
    assert helper.extract_closed_issue_numbers(None) == set()  # type: ignore[arg-type]


def test_extract_freeform_prose_no_match(helper):
    """A body with prose that mentions issues but doesn't use a closing
    keyword returns empty set. ``See #1234`` and ``related to #5678``
    do not auto-close on GitHub, so they shouldn't trigger the filter.
    """
    body = "See #1234 for context. This builds on #5678. Discussion in #999."
    assert helper.extract_closed_issue_numbers(body) == set()


def test_extract_no_word_boundary_breakage(helper):
    """The `\\b` word boundary must allow `Closes` at start of line and
    after whitespace, but not match inside a longer word.
    """
    # Match: at start, after whitespace, after newline.
    assert helper.extract_closed_issue_numbers("Closes #1") == {1}
    assert helper.extract_closed_issue_numbers(" Closes #2") == {2}
    assert helper.extract_closed_issue_numbers("\nCloses #3") == {3}
    # Don't match: substring of a longer identifier. ``preclose`` /
    # ``unfixed`` are unusual but should not falsely trigger the filter.
    assert helper.extract_closed_issue_numbers("preclose #99") == set()
    assert helper.extract_closed_issue_numbers("unfixed #99") == set()


# ─── main() — the exit-code contract the bash wrapper depends on ──────


def _run_main(helper, monkeypatch, body: str, current_issue: str | int) -> int:
    """Drive the helper's main() with synthetic stdin + env."""
    import io
    import json

    monkeypatch.setenv("CHECK_SHIPPED_ISSUE_NUM", str(current_issue))
    stdin_payload = json.dumps({"body": body})
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_payload))
    return helper.main()


def test_main_filter_out_when_pr_closes_other_issue(helper, monkeypatch):
    """The motivating case for #4327. PR body says `Closes #3424` but
    we're checking issue #4317 — the helper exits 0 (filter out).
    """
    assert _run_main(helper, monkeypatch, "Closes #3424", 4317) == 0


def test_main_keep_when_pr_closes_same_issue(helper, monkeypatch):
    """The legitimate happy-path PR. PR body says `Closes #4317` and
    we're checking issue #4317 — keep the candidate (exit 1).
    """
    assert _run_main(helper, monkeypatch, "Closes #4317", 4317) == 1


def test_main_keep_when_pr_closes_same_and_other(helper, monkeypatch):
    """A PR that closes BOTH the current issue and another issue is
    legitimate (the current issue's intent shipped) — keep the candidate.
    """
    body = "Closes #4317\nFixes #100"
    assert _run_main(helper, monkeypatch, body, 4317) == 1


def test_main_keep_when_empty_body_no_keywords(helper, monkeypatch):
    """The canonical placeholder PR (#2831 ↔ #3229) — empty body, no
    closing keywords. Keep the candidate so the file-overlap check can
    still match the zombie shape.
    """
    assert _run_main(helper, monkeypatch, "", 4317) == 1


def test_main_keep_when_freeform_prose(helper, monkeypatch):
    """Body with prose but no closing keywords (just `See #N` /
    `related to #N`) — keep the candidate. Only explicit closing
    keywords filter the candidate out.
    """
    body = "See #100 for related context. Builds on #200."
    assert _run_main(helper, monkeypatch, body, 4317) == 1


def test_main_filter_with_lowercase_fixes(helper, monkeypatch):
    """Lowercase verbs are recognized — the regex is case-insensitive.

    Real-world bodies use both `Closes #N` and `fixes #N` interchangeably.
    """
    assert _run_main(helper, monkeypatch, "fixes #888", 4317) == 0


def test_main_fail_open_on_malformed_json(helper, monkeypatch):
    """Malformed PR JSON on stdin — fail open (exit 1, keep candidate).

    The wrapper's overall flow already drops candidates whose `gh pr
    view` call errored upstream of this helper, so a malformed JSON
    here is unusual; failing open lets the file-overlap check apply
    rather than silently dropping the candidate.
    """
    import io

    monkeypatch.setenv("CHECK_SHIPPED_ISSUE_NUM", "4317")
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    assert helper.main() == 1


def test_main_fail_open_on_missing_issue_num(helper, monkeypatch):
    """If `CHECK_SHIPPED_ISSUE_NUM` is unset/blank, the helper cannot
    decide — fail open (exit 1, keep candidate). Caller falls back to
    file-overlap check.
    """
    import io
    import json

    monkeypatch.delenv("CHECK_SHIPPED_ISSUE_NUM", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"body": "Closes #1"})))
    assert helper.main() == 1
