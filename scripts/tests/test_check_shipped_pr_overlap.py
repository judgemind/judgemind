# venv: none
"""Tests for ``scripts/_check_shipped_pr_overlap.py``.

The overlap helper is the threshold gate for ``check-shipped-pr.sh``. It
reads the candidate PR's JSON on stdin plus a handful of env-var-passed
context (candidate paths, target subset, issue createdAt, audit-class
flag) and decides whether the PR's file overlap clears the high-
confidence threshold for "this issue's work has shipped".

The tests cover:

  - Pre-#4223 baseline behavior (audit-class env unset / empty / "0"
    preserves the old ≥1 added OR ≥2 total threshold).
  - Audit-class tightening (#4223): a non-empty CHECK_SHIPPED_AUDIT_CLASS
    requires ≥2 target-context overlaps AND every overlap is ADDED;
    single-file added overlaps and any-modified overlaps are dropped.
  - Canonical pre-#4223 cases still work (placeholder PR added a file the
    issue's AC asks for is still detected when the issue is NOT
    audit-class).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "_check_shipped_pr_overlap.py"


def _import_helper_module():
    """Load the overlap helper as ``check_shipped_pr_overlap``."""
    spec = importlib.util.spec_from_file_location(
        "check_shipped_pr_overlap", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_shipped_pr_overlap"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def helper():
    return _import_helper_module()


def _run_main(
    helper: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pr_json: dict,
    *,
    candidate_files: str = "",
    target_files: str | None = None,
    issue_created_at: str = "",
    audit_class: str = "",
) -> tuple[int, str]:
    """Drive ``helper.main`` with a mocked stdin and env, return (exit_code, stdout).

    Each call sets ALL four env vars explicitly — the helper reads them
    via ``os.environ.get(...)`` and the stricter audit-class branch only
    fires when ``CHECK_SHIPPED_AUDIT_CLASS`` is set, so passing
    ``audit_class=""`` exercises the pre-#4223 behavior.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pr_json)))
    monkeypatch.setenv("CHECK_SHIPPED_CANDIDATE_FILES", candidate_files)
    if target_files is None:
        monkeypatch.delenv("CHECK_SHIPPED_TARGET_FILES", raising=False)
    else:
        monkeypatch.setenv("CHECK_SHIPPED_TARGET_FILES", target_files)
    monkeypatch.setenv("CHECK_SHIPPED_ISSUE_CREATED_AT", issue_created_at)
    monkeypatch.setenv("CHECK_SHIPPED_AUDIT_CLASS", audit_class)
    rc = helper.main()
    captured = capsys.readouterr()
    return rc, captured.out


def _pr_files(*entries: tuple[str, int, int, str | None]) -> list[dict]:
    """Build a `files` list from (path, additions, deletions, change_type) tuples.

    Use ``None`` for change_type to omit it (exercises the deletions==0
    fallback heuristic from #4340).
    """
    out: list[dict] = []
    for path, additions, deletions, change_type in entries:
        entry: dict = {"path": path, "additions": additions, "deletions": deletions}
        if change_type is not None:
            entry["changeType"] = change_type
        out.append(entry)
    return out


# ─── Pre-#4223 baseline (audit-class unset preserves original threshold) ───


def test_baseline_single_added_overlap_clears_threshold(helper, monkeypatch, capsys):
    """Pre-#4223: 1 ADDED target-context overlap clears the threshold.

    Audit-class env is empty — preserves the canonical zombie-detection
    path. A placeholder PR added the file the issue cites; the helper
    emits a count + overlap line.
    """
    pr_json = {
        "mergedAt": "2026-04-24T00:00:00Z",
        "baseRefName": "main",
        "files": _pr_files(("scripts/foo.sh", 100, 0, "ADDED")),
    }
    rc, out = _run_main(
        helper,
        monkeypatch,
        capsys,
        pr_json,
        candidate_files="scripts/foo.sh",
        target_files="scripts/foo.sh",
    )
    assert rc == 0
    assert out.startswith("1\t"), f"expected count=1 line, got: {out!r}"


def test_baseline_two_modified_overlap_clears_threshold(helper, monkeypatch, capsys):
    """Pre-#4223: 2 MODIFIED target-context overlaps clear the threshold."""
    pr_json = {
        "mergedAt": "2026-04-24T00:00:00Z",
        "baseRefName": "main",
        "files": _pr_files(
            ("scripts/foo.sh", 5, 5, "MODIFIED"),
            ("packages/web/bar.ts", 8, 3, "MODIFIED"),
        ),
    }
    rc, out = _run_main(
        helper,
        monkeypatch,
        capsys,
        pr_json,
        candidate_files="scripts/foo.sh,packages/web/bar.ts",
        target_files="scripts/foo.sh,packages/web/bar.ts",
    )
    assert rc == 0
    assert out.startswith("2\t"), f"expected count=2 line, got: {out!r}"


def test_baseline_single_modified_overlap_below_threshold(helper, monkeypatch, capsys):
    """Pre-#4223: a single MODIFIED overlap is below the threshold.

    Verifies the OR-branch — one overlap that's NOT added is not enough.
    """
    pr_json = {
        "mergedAt": "2026-04-24T00:00:00Z",
        "baseRefName": "main",
        "files": _pr_files(("scripts/foo.sh", 5, 5, "MODIFIED")),
    }
    rc, out = _run_main(
        helper,
        monkeypatch,
        capsys,
        pr_json,
        candidate_files="scripts/foo.sh",
        target_files="scripts/foo.sh",
    )
    assert rc == 0
    assert out == "", f"expected empty output (below threshold), got: {out!r}"


# ─── Audit-class tightening (#4223 — primary acceptance criterion) ──────


def test_audit_class_single_added_overlap_dropped(helper, monkeypatch, capsys):
    """#4223: an audit-class issue with a single ADDED overlap is dropped.

    This is the canonical FP shape from #4223's worked example #4208 —
    issue cites one file in narrative prose, an unrelated prior PR
    added that file. Pre-#4223 (without #4353's date guard reaching) the
    pre-existing threshold (≥1 added) would fire. Post-#4223 with audit-
    class set, ≥2 target-context overlaps is required → drops.
    """
    pr_json = {
        "mergedAt": "2026-04-24T00:00:00Z",
        "baseRefName": "main",
        "files": _pr_files(("packages/web/tailwind.config.ts", 200, 0, "ADDED")),
    }
    rc, out = _run_main(
        helper,
        monkeypatch,
        capsys,
        pr_json,
        candidate_files="packages/web/tailwind.config.ts",
        target_files="packages/web/tailwind.config.ts",
        audit_class="audit",
    )
    assert rc == 0
    assert out == "", (
        f"expected empty output (audit-class single-file dropped), got: {out!r}"
    )


def test_audit_class_two_added_overlaps_clears_threshold(helper, monkeypatch, capsys):
    """#4223: an audit-class issue with 2 ADDED overlaps clears the (tightened) threshold.

    Sanity check — the tightening must NOT block legitimate audit
    matches. When a PR genuinely created BOTH files the audit names,
    that's strong enough evidence of shipped work even for an audit
    issue.
    """
    pr_json = {
        "mergedAt": "2026-04-24T00:00:00Z",
        "baseRefName": "main",
        "files": _pr_files(
            ("scripts/foo.sh", 100, 0, "ADDED"),
            ("scripts/bar.sh", 50, 0, "ADDED"),
        ),
    }
    rc, out = _run_main(
        helper,
        monkeypatch,
        capsys,
        pr_json,
        candidate_files="scripts/foo.sh,scripts/bar.sh",
        target_files="scripts/foo.sh,scripts/bar.sh",
        audit_class="audit",
    )
    assert rc == 0
    assert out.startswith("2\t"), f"expected count=2 line, got: {out!r}"


def test_audit_class_mixed_added_modified(helper, monkeypatch, capsys):
    """#4501: an audit-class issue with mixed ADDED+MODIFIED overlaps clears the threshold.

    The #4501 escape hatch on the #4223 audit-class tightening. Pre-#4501,
    the tightened rule required EVERY target-context overlap to be ADDED,
    which over-penalized audit issues that prescribed BOTH (a) a
    modification to an existing file AND (b) creation of a new file.
    PR #3319 ↔ #3310 is exactly this shape — `main.tf` modified +
    `iam-agent-phase-b-smoke.sh` added. The refined rule (≥1 target-
    context overlap is ADDED, ≥2 total) re-opens this legitimate match.

    Mirrors the #3310 ↔ #3319 fixture shape: one MODIFIED file
    (`infra/terraform/.../main.tf`) and one ADDED file
    (`scripts/iam-agent-phase-b-smoke.sh`).
    """
    pr_json = {
        "mergedAt": "2026-04-24T00:00:00Z",
        "baseRefName": "main",
        "files": _pr_files(
            ("infra/terraform/modules/iam-agent/main.tf", 12, 4, "MODIFIED"),
            ("scripts/iam-agent-phase-b-smoke.sh", 80, 0, "ADDED"),
        ),
    }
    rc, out = _run_main(
        helper,
        monkeypatch,
        capsys,
        pr_json,
        candidate_files=(
            "infra/terraform/modules/iam-agent/main.tf,"
            "scripts/iam-agent-phase-b-smoke.sh"
        ),
        target_files=(
            "infra/terraform/modules/iam-agent/main.tf,"
            "scripts/iam-agent-phase-b-smoke.sh"
        ),
        audit_class="audit",
    )
    assert rc == 0
    assert out.startswith("2\t"), (
        f"expected count=2 line (audit-class mixed clears threshold), got: {out!r}"
    )


def test_audit_class_two_modified_overlaps_dropped(helper, monkeypatch, capsys):
    """#4223 (refined #4501): an audit-class issue with all-MODIFIED overlaps is dropped.

    The most common FP shape for audit issues that cite multiple files
    in a directory — a prior refactor PR touched both. Pre-#4223 the
    ≥2 total branch fires; post-#4223 with audit-class, ≥1 ADDED is
    required (#4501 refinement of the original "all-ADDED" rule) → with
    no ADDED overlaps in the PR's diff, the match still drops.
    """
    pr_json = {
        "mergedAt": "2026-04-24T00:00:00Z",
        "baseRefName": "main",
        "files": _pr_files(
            ("scripts/foo.sh", 5, 5, "MODIFIED"),
            ("packages/web/bar.ts", 8, 3, "MODIFIED"),
        ),
    }
    rc, out = _run_main(
        helper,
        monkeypatch,
        capsys,
        pr_json,
        candidate_files="scripts/foo.sh,packages/web/bar.ts",
        target_files="scripts/foo.sh,packages/web/bar.ts",
        audit_class="audit",
    )
    assert rc == 0
    assert out == "", (
        f"expected empty output (audit-class all-modified dropped), got: {out!r}"
    )


# ─── Non-audit issues retain pre-#4223 behavior on those same shapes ────


def test_non_audit_two_modified_overlaps_clears_threshold(helper, monkeypatch, capsys):
    """Non-audit issues retain pre-#4223 behavior — 2 MODIFIED clears.

    Mirror of `test_audit_class_two_modified_overlaps_dropped` but with
    audit_class="" — confirms the tightening fires ONLY when the env
    var is set.
    """
    pr_json = {
        "mergedAt": "2026-04-24T00:00:00Z",
        "baseRefName": "main",
        "files": _pr_files(
            ("scripts/foo.sh", 5, 5, "MODIFIED"),
            ("packages/web/bar.ts", 8, 3, "MODIFIED"),
        ),
    }
    rc, out = _run_main(
        helper,
        monkeypatch,
        capsys,
        pr_json,
        candidate_files="scripts/foo.sh,packages/web/bar.ts",
        target_files="scripts/foo.sh,packages/web/bar.ts",
        audit_class="",
    )
    assert rc == 0
    assert out.startswith("2\t"), f"expected count=2 line, got: {out!r}"


def test_audit_class_zero_value_disables_tightening(helper, monkeypatch, capsys):
    """`CHECK_SHIPPED_AUDIT_CLASS=0` is treated as non-audit (defense-in-depth).

    The classifier emits empty stdout for non-audit, but a defensive
    "0" value also disables tightening — preserves pre-#4223 behavior
    for any future caller that passes "0" explicitly.
    """
    pr_json = {
        "mergedAt": "2026-04-24T00:00:00Z",
        "baseRefName": "main",
        "files": _pr_files(("scripts/foo.sh", 100, 0, "ADDED")),
    }
    rc, out = _run_main(
        helper,
        monkeypatch,
        capsys,
        pr_json,
        candidate_files="scripts/foo.sh",
        target_files="scripts/foo.sh",
        audit_class="0",
    )
    assert rc == 0
    assert out.startswith("1\t"), (
        f"expected count=1 line (0 disables tightening), got: {out!r}"
    )


# ─── Date-ordering guard (#4353) still fires regardless of audit class ──


def test_audit_class_does_not_override_date_guard(helper, monkeypatch, capsys):
    """#4353's date guard takes precedence — PR-merged-before-issue-filed drops first.

    Even with audit_class set and 2+ ADDED overlaps (which would clear
    the audit-class threshold), a PR that merged before the issue was
    filed cannot have shipped the issue's work and is dropped by the
    date guard. Order-of-evaluation safety check.
    """
    pr_json = {
        # Merged 30 days BEFORE the issue's createdAt below.
        "mergedAt": "2026-04-08T00:00:00Z",
        "baseRefName": "main",
        "files": _pr_files(
            ("scripts/foo.sh", 100, 0, "ADDED"),
            ("scripts/bar.sh", 50, 0, "ADDED"),
        ),
    }
    rc, out = _run_main(
        helper,
        monkeypatch,
        capsys,
        pr_json,
        candidate_files="scripts/foo.sh,scripts/bar.sh",
        target_files="scripts/foo.sh,scripts/bar.sh",
        issue_created_at="2026-05-08T19:41:29Z",
        audit_class="audit",
    )
    assert rc == 0
    assert out == "", f"expected empty output (date guard dropped), got: {out!r}"
