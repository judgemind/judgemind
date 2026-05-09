#!/usr/bin/env bash
# check-no-duplicate-framework-helpers.sh — Forbid scripts/*.py from
# re-defining names exported by ``framework.s3_keys``.
#
# Background — why duplicates accrue
# ──────────────────────────────────
# PR #4447 extracted the flat-hash S3 key helpers from the duplicated
# copies in ``cleanup_mislabeled_s3_2661.py`` and
# ``repoint_mislabeled_documents_4439.py`` into the shared module
# ``packages/scraper-framework/src/framework/s3_keys.py``. ECS oneshot
# scripts can import them via the standard ``from framework.s3_keys
# import ...`` path because the helpers are bundled into the
# ingestion-worker / scraper-framework Docker image.
#
# Days later PR #4453 shipped ``scripts/create_missing_twins_4446.py``
# that re-duplicated the helpers. The agent had read the *pre-#4447*
# docstring of ``repoint_mislabeled_documents_4439.py`` (which still
# carried the legacy NOTE about the duplication being deliberate) and
# faithfully copied the duplication. Issue #4455 tracks the migration
# of ``create_missing_twins_4446.py``; this guard prevents the next
# agent who clones one of those scripts from inheriting the same
# duplication.
#
# What this guard scans
# ─────────────────────
# Every ``*.py`` file under ``scripts/`` EXCEPT files inside
# ``scripts/archive/`` and ``scripts/tests/``. The exclusions are
# scope-driven:
#
#   - ``scripts/archive/`` houses deprecated one-offs kept for
#     posterity. The pre-extraction migration (``migrate_s3_keys.py``)
#     lives there and is intentionally kept independent.
#   - ``scripts/tests/`` legitimately imports from the framework but
#     also defines names locally as fixtures / inside
#     ``mock_sys_modules`` blocks — out of scope for this guard.
#
# A script is flagged when it defines a top-level ``def <name>`` or
# top-level ``<NAME> = ...`` (or annotated ``<NAME>: T = ...``) whose
# name appears in the public API of
# ``packages/scraper-framework/src/framework/s3_keys.py``.
#
# Public API is determined by ``__all__`` if present; otherwise every
# top-level non-underscore ``def`` / ``class`` / module-scope
# assignment defined IN the module (imports are never counted).
#
# Allowlist
# ─────────
# Files that legitimately need to re-define a framework helper opt out
# by adding a ``# allow-duplicate-framework-helpers: <issue-or-PR-ref>``
# comment marker anywhere in the file. The marker must cite the issue
# or PR that justified the exception so future readers can audit
# whether the exemption is still warranted. The marker travels with the
# file so renaming or moving the script does not break the exemption.
#
# Today's only pre-existing duplicator is
# ``scripts/create_missing_twins_4446.py``. Its temporary marker cites
# #4455, the issue that tracks the migration to importing from
# ``framework.s3_keys``. When #4455 lands, the import migration removes
# the duplicates AND the marker — the guard then runs strictly on a
# clean tree.
#
# Issue #4456. Cross-reference: #4447 (helper extraction), #4453 (the
# regressor PR), #4455 (the migration that closes the loop).
#
# Usage
# ─────
#
#   scripts/check-no-duplicate-framework-helpers.sh
#       # scan scripts/**/*.py from the repo's scripts/ directory
#   scripts/check-no-duplicate-framework-helpers.sh [scan-dir]
#       # scan a specific directory tree (used by tests)
#
# Exit codes
# ──────────
#
#   0 — No violations found.
#   1 — At least one scripts/*.py defines a name exported by
#       framework.s3_keys without the allowlist marker.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ─── Determine scan target ───────────────────────────────────────────
# With no argument: scan the repo's scripts/ directory.
# With an argument: scan that directory tree (test fixtures pass a
# tempdir; the shape inside must mirror the production layout — i.e.
# the scripts/ directory must contain the source files plus an
# ``archive/`` and ``tests/`` subtree if the test wants to exercise
# the exclusion logic).
SCAN_DIR="${1:-$REPO_ROOT/scripts}"
FRAMEWORK_PATH="$REPO_ROOT/packages/scraper-framework/src/framework/s3_keys.py"

# Tests can override the framework path so a synthetic public API can
# be exercised against synthetic scripts.
if [[ -n "${FRAMEWORK_S3_KEYS_PATH:-}" ]]; then
    FRAMEWORK_PATH="$FRAMEWORK_S3_KEYS_PATH"
fi

if [[ ! -f "$FRAMEWORK_PATH" ]]; then
    # If the framework module is missing, the guard cannot make a
    # decision. Exit 0 — the missing-module case will surface via
    # other CI guards (and most likely the build itself).
    exit 0
fi

# ─── Collect scan targets ────────────────────────────────────────────
# Find all .py files under SCAN_DIR, EXCLUDING:
#   - $SCAN_DIR/archive/**
#   - $SCAN_DIR/tests/**
#
# We use -prune so the excluded subdirectories are skipped before find
# descends into them, which is both faster and safer (avoids visiting
# vendored archive copies that legitimately re-define helpers).

py_files=()
while IFS= read -r -d '' found; do
    py_files+=("$found")
done < <(
    find "$SCAN_DIR" \
        \( -path "$SCAN_DIR/archive" -o -path "$SCAN_DIR/tests" \) -prune \
        -o -type f -name '*.py' -print0
)

# Empty file list → no violations possible.
if [[ ${#py_files[@]} -eq 0 ]]; then
    exit 0
fi

# ─── Run the AST scanner ─────────────────────────────────────────────
# The Python scanner emits one line per violation in the form:
#   <path>:<lineno>:<name>
python_output="$(python3 "$REPO_ROOT/scripts/check_no_duplicate_framework_helpers.py" \
    "$FRAMEWORK_PATH" "${py_files[@]}")"

# ─── Report violations ───────────────────────────────────────────────
if [[ -z "${python_output// /}" ]]; then
    exit 0
fi

violations=0
echo "ERROR: Found scripts/*.py files re-defining names exported by framework.s3_keys."
echo ""
echo "  PR #4447 extracted the flat-hash S3 key helpers (parse_flat_hash_key,"
echo "  is_mislabel, build_twin_key, head_object_metadata_hash, KEY_PATTERN)"
echo "  to the shared module packages/scraper-framework/src/framework/s3_keys.py."
echo "  ECS oneshot scripts run via scripts/ecs-run-task.sh CAN import from"
echo "  framework.s3_keys because the helpers are bundled into the"
echo "  scraper-framework Docker image — the same path that already serves"
echo "  the ``from framework.logging import configure_structlog`` import that"
echo "  every post-#4368 script uses."
echo ""
echo "  Re-defining these names locally regresses the maintainability win"
echo "  #4447 delivered: a future bug fix in the framework module would"
echo "  fix one site but leave the duplicate stale, producing the silent"
echo "  drift class behind #4453 ↔ #4455."
echo ""
echo "  Violating definition(s):"
echo ""
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    echo "    $entry"
    violations=$((violations + 1))
done <<< "$python_output"

if (( violations > 0 )); then
    echo ""
    echo "  Found $violations duplicated definition(s)."
    echo ""
    echo "  Fix: replace the local definitions with an import from"
    echo "  framework.s3_keys. The canonical migration:"
    echo ""
    echo "    -KEY_PATTERN = re.compile(r\"^(?P<state>[a-z]{2})/...\")"
    echo "    -"
    echo "    -def parse_flat_hash_key(key: str) -> dict[str, str] | None:"
    echo "    -    ..."
    echo "    -"
    echo "    -def is_mislabel(filename_hash: str, metadata_hash: str | None) -> bool:"
    echo "    -    ..."
    echo "    -"
    echo "    -def build_twin_key(mislabel_key: str, metadata_hash: str) -> str | None:"
    echo "    -    ..."
    echo "    -"
    echo "    -def head_object_metadata_hash(s3_client, bucket, key) -> str | None:"
    echo "    -    ..."
    echo "    +from framework.s3_keys import ("
    echo "    +    KEY_PATTERN,"
    echo "    +    build_twin_key,"
    echo "    +    head_object_metadata_hash,"
    echo "    +    is_mislabel,"
    echo "    +    parse_flat_hash_key,"
    echo "    +)"
    echo ""
    echo "  Reference implementations:"
    echo "    - scripts/cleanup_mislabeled_s3_2661.py (post-#4447)"
    echo "    - scripts/repoint_mislabeled_documents_4439.py (post-#4447)"
    echo ""
    echo "  If your script genuinely cannot import from framework.s3_keys"
    echo "  (e.g., it lives in scripts/archive/ and is preserved verbatim"
    echo "  for posterity), add a marker line citing the justifying issue:"
    echo ""
    echo "    # allow-duplicate-framework-helpers: #<issue-or-PR>"
    echo ""
    echo "  See #4456 for the full rationale and #4455 for the canonical"
    echo "  migration shape used to close out the only pre-existing"
    echo "  duplicator (scripts/create_missing_twins_4446.py)."
    exit 1
fi

exit 0
