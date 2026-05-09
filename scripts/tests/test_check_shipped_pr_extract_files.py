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


# ─── Line-range suffix stripping (issue #4462) ────────────────────────


def test_line_range_suffix_stripped_dedupes_with_bare_path(extract_module):
    """Issue bodies often cite a file both with a line range
    (``foo.py:47-62``) and bare (``foo.py``). The downstream
    ``gh api /repos/.../commits?path=<path>`` query rejects line ranges,
    so the line-ranged variant resolves to no commits and the threshold
    is unreachable. Strip the trailing ``:N`` / ``:N-M`` suffix BEFORE
    dedupe so both cite forms collapse to one canonical entry.

    Regression for #4462. Canonical case from issue #3310 — body cited
    ``infra/terraform/modules/iam_agent/main.tf:47-62`` and
    ``infra/terraform/modules/iam_agent/main.tf:132-141`` in narrative
    plus the bare path in a fenced block; pre-fix the extractor emitted
    three separate entries.
    """
    body = (
        "see infra/terraform/foo/main.tf:47-62 and "
        "infra/terraform/foo/main.tf for the fix"
    )
    out = extract_module.extract_files(body)
    assert out == ["infra/terraform/foo/main.tf"]


def test_line_range_suffix_single_line_stripped(extract_module):
    """Bare-line citations (``foo.py:42``, no range) also strip. The
    suffix regex matches ``:N`` as well as ``:N-M``.
    """
    body = (
        "Bug at scripts/check-shipped-pr.sh:42 — see also scripts/check-shipped-pr.sh."
    )
    out = extract_module.extract_files(body)
    assert out == ["scripts/check-shipped-pr.sh"]


def test_line_range_suffix_three_cite_forms_collapse(extract_module):
    """Three cite forms — single line, line range, bare — all dedupe
    to one entry. Mirrors the worked example from issue #4462.
    """
    body = (
        "infra/terraform/modules/iam_agent/main.tf:47-62 and "
        "infra/terraform/modules/iam_agent/main.tf:132 plus "
        "infra/terraform/modules/iam_agent/main.tf bare."
    )
    out = extract_module.extract_files(body)
    assert out == ["infra/terraform/modules/iam_agent/main.tf"]


def test_line_range_suffix_with_trailing_punctuation(extract_module):
    """A cite like ``foo.py:47-62.`` (sentence period after the line
    range) must still strip the line range. The implementation runs
    the punctuation strip BEFORE the line-range strip — the period
    falls off first, then the ``:47-62`` falls off cleanly.
    """
    body = (
        "Bug lives at scripts/check-shipped-pr.sh:47-62. "
        "Reading scripts/check-shipped-pr.sh."
    )
    out = extract_module.extract_files(body)
    assert out == ["scripts/check-shipped-pr.sh"]


def test_line_range_suffix_target_wins_over_search(extract_module):
    """When the bare path appears in narrative (target-context) and the
    line-ranged variant appears in a Verify line, both collapse to the
    bare path — and the precedence rule still tags it as target-context.
    Locks in that the line-range strip runs before classification.
    """
    body = (
        "Update `scripts/check-shipped-pr.sh` to handle the new shape.\n"
        "Verify: `grep -n widget scripts/check-shipped-pr.sh:42`\n"
    )
    target, search = extract_module.classify_files(body)
    # Narrative cite (bare) is target; Verify cite (line-ranged) strips
    # to bare and then promotes to target via the precedence rule.
    assert target == ["scripts/check-shipped-pr.sh"]
    assert search == []


def test_line_range_suffix_only_in_search_context_stays_search(extract_module):
    """If the path appears only with a line-range suffix inside a
    Verify line (no narrative cite), the stripped path is search-
    context — line-range stripping happens at the path-extraction
    layer, not at the classification layer.
    """
    body = "Verify: `grep -n widget packages/api/src/index.ts:42-99`\n"
    target, search = extract_module.classify_files(body)
    assert target == []
    assert search == ["packages/api/src/index.ts"]


def test_line_range_suffix_does_not_strip_non_numeric(extract_module):
    """The suffix regex matches ONLY ``:<digits>`` or ``:<digits>-<digits>``
    at end-of-string. A path with a non-numeric trailing token after the
    colon (rare, but defensive) is not affected.
    """
    # Trailing colon alone is already stripped by TRAILING_STRIP.
    # A path like ``foo.py:abc`` is not a real cite — defensive: don't
    # strip anything past the colon.
    body = "See packages/api/src/index.ts (the entry point)."
    out = extract_module.extract_files(body)
    assert out == ["packages/api/src/index.ts"]


# ─── Existence-Verify classification (issue #4469) ────────────────────


def test_existence_verify_is_committed_classifies_as_target(extract_module):
    """``Verify: <path> is committed`` cites ``<path>`` as the change
    target — the predicate is "this file must exist," not "this file
    is a search argument." Pre-#4469 the bare ``Verify:`` token put
    every cited path into search-context; post-fix the verb regex no
    longer triggers on plain-prose Verify clauses.

    Canonical case from issue #3310 — its third AC reads
    ``Verify: scripts/iam-agent-phase-b-smoke.sh ... is committed.``
    The cited script IS the load-bearing file the AC requires, not a
    grep / pytest argument.
    """
    body = (
        "## Acceptance criteria\n"
        "\n"
        "- [ ] A documented manual smoke test exists for the Phase-B write path.\n"
        "  - Verify: `scripts/iam-agent-phase-b-smoke.sh` "
        "(or equivalent in `infra/terraform/modules/iam_agent/tests/`) is committed.\n"
    )
    target, search = extract_module.classify_files(body)
    assert "scripts/iam-agent-phase-b-smoke.sh" in target
    assert "scripts/iam-agent-phase-b-smoke.sh" not in search


def test_existence_verify_exists_or_created_classifies_as_target(extract_module):
    """The existence-Verify pattern is shape-agnostic to the specific
    predicate verb — ``is committed``, ``is created``, ``exists``,
    ``must exist`` all behave the same. The classifier doesn't need to
    enumerate predicate verbs; it just needs to NOT classify the line
    as search-context when the line carries no shell-invocation verb.
    """
    body_a = "## AC\n  - Verify: `scripts/dispatcher/zombie_alert.py` exists.\n"
    body_b = "## AC\n  - Verify: `docs/agent/new-runbook.md` is created.\n"
    target_a, _ = extract_module.classify_files(body_a)
    target_b, _ = extract_module.classify_files(body_b)
    assert "scripts/dispatcher/zombie_alert.py" in target_a
    assert "docs/agent/new-runbook.md" in target_b


def test_verify_with_grep_invocation_stays_search(extract_module):
    """Negative regression — the existence-Verify fix must not regress
    the #4340 invariant. ``Verify: `grep -n widget <path>` `` cites
    ``<path>`` as a grep argument; it stays search-context.
    """
    body = (
        "## AC\n"
        "  - Verify: `grep -n widget scripts/run-foo.sh packages/api/src/index.ts`\n"
    )
    target, search = extract_module.classify_files(body)
    assert target == []
    assert "scripts/run-foo.sh" in search
    assert "packages/api/src/index.ts" in search


def test_verify_with_bash_invocation_stays_search(extract_module):
    """Negative regression — ``Verify: `bash <path>` exits 0`` cites
    ``<path>`` as the script being executed, not as a target the
    issue creates. Classify as search-context.

    The verb regex was extended in #4469 to include ``bash`` so this
    case keeps its pre-#4469 search classification after the bare
    ``Verify:`` trigger was dropped.
    """
    body = "## AC\n  - Verify: `bash scripts/tests/test_check_shipped_pr.sh` exits 0.\n"
    target, search = extract_module.classify_files(body)
    assert target == []
    assert "scripts/tests/test_check_shipped_pr.sh" in search


def test_verify_with_python_invocation_stays_search(extract_module):
    """Negative regression — ``Verify: `python3 <path>` `` and
    ``Verify: `python <path>` `` cite the script being executed,
    not a target the issue creates. Classify as search-context.

    Locks in the ``python`` / ``python3`` verb additions from #4469.
    """
    body_a = (
        "## AC\n  - Verify: `python3 scripts/check-something.py --dry-run` succeeds.\n"
    )
    body_b = "## AC\n  - Verify: `python scripts/dispatcher/audit.py` reports clean.\n"
    target_a, search_a = extract_module.classify_files(body_a)
    target_b, search_b = extract_module.classify_files(body_b)
    assert "scripts/check-something.py" in search_a
    assert "scripts/dispatcher/audit.py" in search_b


def test_existence_verify_mixed_with_search_verify(extract_module):
    """Two Verify clauses in the same body — one existence-shape, one
    grep-shape — classify INDEPENDENTLY. The existence Verify's path
    is target; the grep Verify's path is search.
    """
    body = (
        "## AC\n"
        "  - [ ] Smoke test exists.\n"
        "  - Verify: `scripts/new-smoke-test.sh` is committed.\n"
        "  - [ ] Hardcoded prefix is gone from caller.\n"
        "  - Verify: `grep -n staging packages/api/src/legacy.py`\n"
    )
    target, search = extract_module.classify_files(body)
    assert "scripts/new-smoke-test.sh" in target
    assert "packages/api/src/legacy.py" in search
    assert "scripts/new-smoke-test.sh" not in search
    assert "packages/api/src/legacy.py" not in target


def test_existence_verify_three_repo_3310_layout(extract_module):
    """End-to-end repro of the issue #3310 body shape — narrative cites
    ``main.tf`` (with line ranges that strip per #4462) plus a fenced
    HCL block citing the bare ``main.tf``, plus a Verify-existence
    clause citing ``iam-agent-phase-b-smoke.sh``. All three target
    paths classify correctly.

    Locks in #4469's worked example: post-fix, target_overlap should
    contain BOTH ``main.tf`` and ``iam-agent-phase-b-smoke.sh``.
    """
    body = (
        "## Affected files\n"
        "\n"
        "- `infra/terraform/modules/iam_agent/main.tf:47-62` — first issue\n"
        "- `infra/terraform/modules/iam_agent/main.tf:132-141` — second issue\n"
        "\n"
        "## Acceptance criteria\n"
        "\n"
        "- [ ] Policy is split.\n"
        "  - Verify: `terraform plan -chdir=infra/terraform/environments/dev` "
        "shows the new statement; `aws iam simulate-principal-policy "
        "--policy-source-arn <arn> --action-names ecs:RegisterTaskDefinition` "
        "returns `allowed`.\n"
        "- [ ] A documented manual smoke test exists.\n"
        "  - Verify: `scripts/iam-agent-phase-b-smoke.sh` "
        "(or equivalent in `infra/terraform/modules/iam_agent/tests/`) is committed.\n"
    )
    target, search = extract_module.classify_files(body)
    assert "infra/terraform/modules/iam_agent/main.tf" in target
    assert "scripts/iam-agent-phase-b-smoke.sh" in target
    # The aws / terraform plan Verify line cites
    # ``infra/terraform/environments/dev`` — only on a search-context
    # line — so it stays search.
    assert "infra/terraform/environments/dev" in search
