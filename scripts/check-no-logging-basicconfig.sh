#!/usr/bin/env bash
# check-no-logging-basicconfig.sh — Forbid `logging.basicConfig(` in
# top-level `scripts/*.py` files.
#
# After #4368 (drain_splitter), #4373 (13 more scripts/*.py), and #4399
# (seed_judges_from_directory_snapshots), the canonical pattern for
# stdout/CloudWatch logger configuration in `scripts/*.py` is
#
#     from framework.logging import configure_structlog
#     configure_structlog(json=True, stdlib_bridge=True)
#
# `logging.basicConfig(level=..., format="%(asctime)s ...")` silently
# drops every `extra=` field passed to logger calls — see #4368 for the
# post-deploy verification incident on #4360 that surfaced the bug class.
#
# This guard catches new top-level `scripts/*.py` files that introduce
# the old pattern (or any future agent that scaffolds a new script and
# copies the anti-pattern from muscle memory).  Without it, the bug
# class re-emerges silently — a new `scripts/*.py` writes
# `logger.info(..., extra={"document_id": ...})`, the `extra=` field
# disappears in CloudWatch, and the next operator burns time
# rediscovering #4368.
#
# Allowlist
# ─────────
# Files that legitimately need `logging.basicConfig` opt out by adding
# a `# basic-config-allow: <issue-or-PR-ref>` comment marker anywhere
# in the file (typically immediately above the basicConfig call site).
# The marker must cite the issue or PR that justified the exception so
# future readers can audit whether the exemption is still warranted.
# The marker travels with the file so renaming or moving the script
# does not break the exemption.
#
# Today's only allowed exception is `scripts/telemetry_upload.py` — a
# tiny CLI shim whose only job is to upload one local file to S3.  It
# does not call `logger.<level>(..., extra={...})`; the WARNING-level
# stderr stream is sufficient for the operator-only failure paths.
# See `scripts/telemetry_upload.py` for the marker placement and #4400
# for the full rationale.
#
# Scope
# ─────
# Top-level `scripts/*.py` only — the same scope #4373 migrated.  Nested
# subdirectories under `scripts/` (spotcheck/, dispatcher/, dispatcher_v3/,
# archive/, eval/, tests/) are intentionally NOT scanned by this guard —
# they live under separate observability conventions and are excluded
# from the canonical-pattern enforcement.
#
# Usage:
#   scripts/check-no-logging-basicconfig.sh             # scan repo's scripts/
#   scripts/check-no-logging-basicconfig.sh [dir]       # scan a specific directory
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more top-level `scripts/*.py` files contain the forbidden pattern.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT/scripts}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Pattern ─────────────────────────────────────────────────────────────
# Match `logging.basicConfig(` at line start with optional leading whitespace.
# Anchoring at the start prevents matching the pattern when it appears
# inside a string literal or as part of a longer identifier.
PATTERN='^[[:space:]]*logging\.basicConfig\('

# ─── Allowlist marker ────────────────────────────────────────────────────
# Files containing this marker anywhere in their content opt out of the
# check.  The marker must cite the issue/PR justifying the exception.
ALLOW_MARKER='# basic-config-allow:'

# ─── Files that legitimately mention the pattern as context ──────────────
# The check script and its test must spell the pattern literally.
EXCLUDE_FILES=(
    "scripts/check-no-logging-basicconfig.sh"
    "scripts/tests/test_check_no_logging_basicconfig.sh"
)

# ─── Scan ────────────────────────────────────────────────────────────────
#
# Top-level `scripts/*.py` only — this guard intentionally does NOT
# recurse into subdirectories.  The canonical pattern enforcement is
# scoped to the same set of files #4373 migrated.

violations=0
violation_paths=()

# `find` with -maxdepth 1 keeps us at the top level.  We use -print0 to
# survive any pathological filename, and read -d '' to consume it.
while IFS= read -r -d '' file; do
    # Skip if not a regular file (broken symlinks, etc.).
    [[ -f "$file" ]] || continue

    # Skip excluded files (the check script + its test).
    rel="${file#$REPO_ROOT/}"
    skip=false
    for excl in "${EXCLUDE_FILES[@]}"; do
        if [[ "$rel" == "$excl" ]]; then
            skip=true
            break
        fi
    done
    if "$skip"; then
        continue
    fi

    # Skip if the file carries the allowlist marker anywhere.
    if grep -qF "$ALLOW_MARKER" "$file" 2>/dev/null; then
        continue
    fi

    # Look for the forbidden pattern.  -E for the anchored regex, -n for
    # line numbers, no -r (we already enumerated files).
    if matches=$(grep -nE "$PATTERN" "$file" 2>/dev/null); then
        while IFS= read -r line; do
            if [[ $violations -eq 0 ]]; then
                echo "ERROR: Found forbidden 'logging.basicConfig(' usage in scripts/*.py."
                echo ""
                echo "  Top-level scripts/*.py files must use the canonical structlog"
                echo "  pattern instead of stdlib logging.basicConfig — see #4368/#4373."
                echo ""
                echo "  The format-string-only logger silently drops every extra= field"
                echo "  passed to logger calls (extras disappear in CloudWatch Logs"
                echo "  Insights), which surfaced as the post-deploy verification"
                echo "  incident in #4368."
                echo ""
            fi
            echo "    $rel:$line"
            violations=$((violations + 1))
        done <<< "$matches"
        violation_paths+=("$rel")
    fi
done < <(find "$SCAN_DIR" -maxdepth 1 -type f -name '*.py' -print0)

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) across ${#violation_paths[@]} file(s)."
    echo ""
    echo "  Fix: replace the basicConfig block with the canonical structlog"
    echo "  pattern.  The one-line migration:"
    echo ""
    echo "    -import logging"
    echo "    +import logging"
    echo "    +"
    echo "    +from framework.logging import configure_structlog"
    echo "    +"
    echo "    -logging.basicConfig("
    echo "    -    level=logging.INFO,"
    echo "    -    format=\"%(asctime)s %(levelname)-8s %(message)s\","
    echo "    -)"
    echo "    +configure_structlog(json=True, stdlib_bridge=True)"
    echo "     logger = logging.getLogger(__name__)"
    echo ""
    echo "  Reference implementations:"
    echo "    - scripts/drain_splitter_carry_forward_clusters.py (post-#4368)"
    echo "    - scripts/audit_correctly_labeled_s3_orphans.py (post-#4373)"
    echo ""
    echo "  If your script genuinely cannot use configure_structlog (e.g.,"
    echo "  it must avoid the framework dependency for a tiny CLI shim — see"
    echo "  scripts/telemetry_upload.py), add a marker line citing the"
    echo "  justifying issue/PR:"
    echo ""
    echo "    # basic-config-allow: #<issue-or-PR>"
    echo "    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)"
    echo ""
    echo "  See #4400 for the full rationale."
    exit 1
fi

echo "All clean — no forbidden 'logging.basicConfig(' usage in top-level scripts/*.py."
exit 0
