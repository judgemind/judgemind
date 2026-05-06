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
