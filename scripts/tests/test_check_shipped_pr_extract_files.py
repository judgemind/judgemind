# venv: none
"""Tests for ``scripts/_check_shipped_pr_extract_files.py``.

Loads the helper module via ``importlib.util.spec_from_file_location`` —
the filename starts with an underscore so it is not imported by the
normal ``import scripts._check_...`` path (``scripts/`` is not a package).

The helper extracts candidate file paths from issue title+body so the
``check-shipped-pr.sh`` outer wrapper can scan for shipped matches.
Issue #4219 hardened the extractor to drop *directory* paths (paths
ending in ``/``) — those are container references, not specific files,
and they previously produced false-positive overlaps against any PR that
touched a file under the directory.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "_check_shipped_pr_extract_files.py"


def _import_extract_module():
    """Load the extractor script as ``check_shipped_pr_extract_files``."""
    spec = importlib.util.spec_from_file_location(
        "check_shipped_pr_extract_files", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_shipped_pr_extract_files"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def extract_module():
    return _import_extract_module()


# ─── Directory-path exclusion (issue #4219) ───────────────────────────


def test_directory_paths_excluded(extract_module):
    """Paths ending in ``/`` are container references, not load-bearing
    file claims — they should never appear in the extracted candidate list.

    Regression for #4219. Empirically observed: issue body cites
    ``packages/web/src/`` or ``scripts/dispatcher/tests/`` as a directory
    scope, not as a specific file the issue is creating. Pre-fix, the
    extractor returned the directory token; the downstream overlap
    helper's directory-prefix matcher then registered ANY file the
    candidate PR touched under that directory as an overlap, producing
    false ``shipped:`` matches.
    """
    body = (
        "We need to audit `packages/web/src/components/ui/` for token "
        "collisions, plus check `packages/web/src/` more broadly."
    )
    assert extract_module.extract_files(body) == []


def test_directory_paths_excluded_dispatcher_tests(extract_module):
    """Concrete second false-positive observed during #4216 verification.

    Issue #4213 cites ``scripts/dispatcher/tests/`` (a directory) and the
    pre-fix extractor surfaced it. Post-fix, only specific files inside
    the directory are extracted — and #4213's body cites only the
    directory, so the candidate list is empty.
    """
    body = (
        "Migrate the 10 fetch_queue sites in scripts/dispatcher/tests/ "
        "to fetch_responses."
    )
    assert extract_module.extract_files(body) == []


def test_directory_paths_excluded_mixed_with_specific_file(extract_module):
    """If the body cites BOTH a directory and a specific file, drop only
    the directory and keep the specific file. The fix tightens the
    extractor without regressing legitimate file references.
    """
    body = (
        "Update scripts/check-shipped-pr.sh and audit "
        "scripts/dispatcher/tests/ for the same pattern."
    )
    out = extract_module.extract_files(body)
    assert "scripts/check-shipped-pr.sh" in out
    assert "scripts/dispatcher/tests/" not in out
    # Anything else returned must not be a directory reference.
    assert all(not p.endswith("/") for p in out)


def test_directory_paths_excluded_all_five_roots(extract_module):
    """Coverage across the regex's five conventional roots — scripts/,
    packages/, docs/, infra/, and ``.github/``. A body that cites only
    bare directories under any of these roots should yield an empty list.
    """
    body = (
        "Audit packages/web/src/, scripts/dispatcher/, docs/agent/, "
        "infra/terraform/, and .github/workflows/ for the same issue."
    )
    assert extract_module.extract_files(body) == []


# ─── Existing behavior remains intact ─────────────────────────────────


def test_specific_file_paths_still_extracted(extract_module):
    """Specific file references (with extensions or trailing
    punctuation) continue to extract correctly. This is the canonical
    positive-case — the extractor's reason for existing.
    """
    body = (
        "Bug in `scripts/check-shipped-pr.sh` (#4204): the regex over-"
        "matches. See `scripts/_check_shipped_pr_extract_files.py` for "
        "the entry point."
    )
    out = extract_module.extract_files(body)
    assert "scripts/check-shipped-pr.sh" in out
    assert "scripts/_check_shipped_pr_extract_files.py" in out


def test_empty_body_returns_empty_list(extract_module):
    assert extract_module.extract_files("") == []
    assert extract_module.extract_files(None) == []  # type: ignore[arg-type]


def test_dedupe_preserves_first_seen_order(extract_module):
    """Multiple references to the same path collapse to one entry, in
    first-seen order. The fix must not alter dedupe semantics.
    """
    body = (
        "scripts/foo.sh is broken; scripts/bar.sh too; but scripts/foo.sh is the worst."
    )
    out = extract_module.extract_files(body)
    assert out == ["scripts/foo.sh", "scripts/bar.sh"]


def test_glob_paths_excluded(extract_module):
    """Glob entries (``scripts/tests/*.sh``) were already filtered pre-
    fix — the regression test below locks that in. The fix's directory
    exclusion runs alongside the glob exclusion, not in place of it.
    """
    body = "Update scripts/tests/*.sh and scripts/check-foo.sh."
    out = extract_module.extract_files(body)
    assert "scripts/check-foo.sh" in out
    assert all("*" not in p for p in out)


def test_short_paths_excluded(extract_module):
    """Pre-fix, paths shorter than 8 chars were filtered to avoid bare-
    root false positives like ``docs/``. The fix preserves that filter
    AND adds the trailing-slash filter — both apply.
    """
    body = "See scripts/ and docs/ for more details."
    # All four are either too short or end in `/` — both filters drop them.
    assert extract_module.extract_files(body) == []


# ─── Search-context vs target-context classification (issue #4340) ────


def test_classify_files_separates_verify_line_paths(extract_module):
    """Paths that appear ONLY inside a ``Verify:`` line are classified as
    *search-context*, not *target-context*. The grep/pytest/aws verbs
    inside the Verify line treat any cited paths as search arguments,
    not as files the resolution must touch.

    Regression for #4340. Issue #4331's Verify line is::

        Verify: ``grep -n "..." packages/A.py packages/B.py scripts/C.py``

    All three paths are search arguments to grep — not files the issue
    resolution targets. PR #3552 happened to touch all three for
    unrelated reasons (LLM retry plumbing) → false-positive shipped
    match. Post-fix the classifier must tag those three as
    ``search-context`` so the threshold helper can require ≥1
    *target-context* overlap.
    """
    body = (
        "## Acceptance criteria\n"
        "\n"
        "- [ ] Splitter aliases registered.\n"
        '  Verify: `grep -n "alias" packages/scraper-framework/src/courts/ca/sc_tentatives.py '
        "packages/scraper-framework/src/ingestion/worker.py "
        "scripts/reingest_from_s3.py`\n"
    )
    target, search = extract_module.classify_files(body)
    assert target == []
    assert sorted(search) == sorted(
        [
            "packages/scraper-framework/src/courts/ca/sc_tentatives.py",
            "packages/scraper-framework/src/ingestion/worker.py",
            "scripts/reingest_from_s3.py",
        ]
    )


def test_classify_files_target_context_in_narrative(extract_module):
    """Paths in narrative prose (Problem / Proposal / non-Verify AC text)
    are *target-context* — they describe the load-bearing locations the
    issue intends to change.
    """
    body = (
        "## Proposal\n"
        "\n"
        "Update `scripts/check-shipped-pr.sh` to handle the verify-context case.\n"
        "\n"
        "## Acceptance criteria\n"
        "\n"
        "- [ ] `scripts/check-shipped-pr.sh` no longer flags PR #3552.\n"
        "  Verify: `bash scripts/tests/test_check_shipped_pr.sh` exits 0.\n"
    )
    target, search = extract_module.classify_files(body)
    # The Verify line cites scripts/tests/test_check_shipped_pr.sh.
    # Narrative cites scripts/check-shipped-pr.sh (twice — proposal + AC text).
    assert "scripts/check-shipped-pr.sh" in target
    assert "scripts/tests/test_check_shipped_pr.sh" in search
    # No path appears in BOTH — once classified as search-context (Verify
    # line), it stays there even if narrative also cites it. (See
    # docstring on classify_files for the precedence rule.)
    assert "scripts/check-shipped-pr.sh" not in search


def test_classify_files_fenced_block_treated_as_search_context(extract_module):
    """Paths inside fenced code blocks (``` ```) that contain shell
    invocations (grep / pytest / aws / curl) are *search-context*, not
    *target-context*. Issue bodies frequently paste shell-output blocks
    naming files that are search arguments, not change targets.
    """
    body = (
        "## Problem\n"
        "\n"
        "Found during /task pickup:\n"
        "\n"
        "```\n"
        "$ grep -n widget scripts/run-foo.sh packages/api/src/index.ts\n"
        "scripts/run-foo.sh:42: widget = make_widget()\n"
        "```\n"
        "\n"
        "## Proposal\n"
        "\n"
        "Patch `scripts/render-widget.sh` to handle the new shape.\n"
    )
    target, search = extract_module.classify_files(body)
    assert "scripts/render-widget.sh" in target
    assert "scripts/run-foo.sh" in search
    assert "packages/api/src/index.ts" in search
    # No leakage in the other direction.
    assert "scripts/render-widget.sh" not in search


def test_classify_files_pytest_line_search_context(extract_module):
    """``Verify: pytest <path>`` cites ``<path>`` as a test selector,
    not as a file the resolution edits. Tag as search-context.
    """
    body = (
        "## Acceptance criteria\n"
        "\n"
        "- [ ] New behavior tested.\n"
        "  Verify: `pytest packages/scraper-framework/tests/test_split_registry.py -v`\n"
    )
    target, search = extract_module.classify_files(body)
    assert target == []
    assert "packages/scraper-framework/tests/test_split_registry.py" in search


def test_classify_files_extract_files_back_compat(extract_module):
    """The legacy ``extract_files()`` API returns the union (search ∪
    target) so callers that don't care about classification keep
    working. Tests that lock in directory / glob / short-path filters
    still pass against ``extract_files()``.
    """
    body = (
        "Update `scripts/check-shipped-pr.sh`.\n"
        "Verify: `grep -n widget scripts/run-foo.sh`.\n"
    )
    out = extract_module.extract_files(body)
    # Both paths land in the union, regardless of context.
    assert sorted(out) == sorted(["scripts/check-shipped-pr.sh", "scripts/run-foo.sh"])


def test_classify_files_path_first_seen_in_verify_stays_search(extract_module):
    """Precedence rule: a path is target-context only if it appears at
    least once OUTSIDE a search-context line. A path cited only inside
    Verify lines / shell-invocation fenced blocks is search-context
    even if it appears multiple times.
    """
    body = (
        "Verify: `grep packages/api/src/index.ts`\n"
        "Verify: `pytest packages/api/src/index.ts`\n"
    )
    target, search = extract_module.classify_files(body)
    assert target == []
    assert search == ["packages/api/src/index.ts"]


def test_classify_files_path_in_both_promotes_to_target(extract_module):
    """If a path appears in BOTH narrative and a Verify line, it is
    target-context — the narrative reference is the load-bearing
    intent and the Verify line is just a re-run convenience."""
    body = (
        "Update `scripts/foo.sh` to handle the new format.\n"
        "Verify: `bash scripts/foo.sh`.\n"
    )
    target, search = extract_module.classify_files(body)
    assert target == ["scripts/foo.sh"]
    assert search == []
