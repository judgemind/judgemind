#!/usr/bin/env bash
# check-parse-document-reingest-safety.sh — Hygiene check enforcing the
# reingest-hazard docstring marker on Live-only ``parse_document``
# implementations under ``packages/scraper-framework/src/courts/``.
#
# Why this check exists
# ---------------------
# Audit #4046 (``docs/investigations/parse_document-reingest-safety-2026-05.md``)
# classified all 20 ``parse_document`` implementations against the
# reingest path.  Live-only implementations (``parse_document`` returns
# ``doc`` unchanged with no read of ``raw_content`` and no
# ``super().parse_document(...)`` delegation) are the structural shape
# that produced the #3986 production incident — CourtListener returned
# ``doc`` unchanged on the reingest path, and its JSON envelope was
# decoded as text and stored as ``ruling_text``, hitting the 50000-char
# truncation cap.
#
# This check is the structural complement to the runtime
# ``_TRUNCATION_SENTINEL_LENGTH`` validator: every Live-only
# ``parse_document`` MUST carry a docstring marker that explicitly names
# the reingest hazard.  Without this guard, a new scraper could land as
# Live-only with a misleading docstring (the same shape #3986 fixed)
# and the failure would only surface on the next reingest.
#
# Marker contract
# ---------------
# The Python scanner (``scripts/check_parse_document_reingest_safety.py``)
# accepts any of these substrings (case-insensitive) in the docstring
# of a Live-only ``parse_document``:
#
#   * "Reingest hazard"
#   * "no-op on the reingest path"
#   * "not reingest-safe"
#
# These match the three current Live-only sites
# (``cc_tentatives_portal.py``, ``sf_civil_tentatives.py``,
# ``oc_tentatives.py``).  See the audit doc for the full per-scraper
# classification.
#
# Issue
# -----
# #4141 (this check).  Audit: #4046.  Failure-shape origin: #3986.
#
# Usage
# -----
#   scripts/check-parse-document-reingest-safety.sh        # scan default courts/
#   scripts/check-parse-document-reingest-safety.sh PATH   # scan a specific dir
#                                                          # (used by tests)
#
# Exit codes
# ----------
#   0 — All Live-only parse_document implementations carry the marker.
#   1 — At least one Live-only parse_document is missing the marker.
#   2 — Internal error (Python scanner failed to run).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCANNER="$REPO_ROOT/scripts/check_parse_document_reingest_safety.py"

# ─── Resolve scan root ────────────────────────────────────────────────────
# With no argument: scan the production courts/ tree (the default).
# With an argument: scan that path (used by tests for fixture isolation).
if [[ $# -eq 0 ]]; then
    SCAN_ARGS=()
else
    SCAN_ARGS=(--root "$1")
fi

# ─── Run the scanner ──────────────────────────────────────────────────────
# The scanner emits one line per violation in the form:
#   <path>:<lineno>:<class.parse_document>
#
# It always exits 0; an empty stdout means "no violations".  We capture
# stdout and decide the wrapper's exit code from there — same split as
# check-no-redos-pattern.sh / check_no_redos_pattern.py.
if ! python_output="$(python3 "$SCANNER" ${SCAN_ARGS[@]+"${SCAN_ARGS[@]}"})"; then
    echo "ERROR: scanner failed to run (see stderr above)." >&2
    exit 2
fi

# ─── Report violations ────────────────────────────────────────────────────
if [[ -z "${python_output// /}" ]]; then
    exit 0
fi

violations=0
echo "ERROR: Live-only parse_document method(s) missing reingest-hazard marker."
echo ""
echo "  A Live-only parse_document is one that does not read"
echo "  doc.raw_content and does not delegate to another parse_document"
echo "  (super().parse_document(...) or parser.parse_document(...))."
echo "  On the reingest path (scripts/reingest_from_s3.py::_reparse_document)"
echo "  this clears judge_name/department/parties unconditionally — see"
echo "  audit #4046 for the full failure analysis (originally surfaced by"
echo "  #3986 in CourtListener)."
echo ""
echo "  Required: each Live-only parse_document docstring MUST contain at"
echo "  least one of these substrings (case-insensitive):"
echo ""
echo "    - Reingest hazard"
echo "    - no-op on the reingest path"
echo "    - not reingest-safe"
echo ""
echo "  Existing examples:"
echo "    packages/scraper-framework/src/courts/ca/cc_tentatives_portal.py"
echo "    packages/scraper-framework/src/courts/ca/sf_civil_tentatives.py"
echo "    packages/scraper-framework/src/courts/ca/oc_tentatives.py"
echo ""
echo "  Violating methods:"
echo ""
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    echo "    $entry"
    violations=$((violations + 1))
done <<< "$python_output"

if (( violations > 0 )); then
    echo ""
    echo "  Found $violations Live-only parse_document method(s) missing the marker."
    exit 1
fi

exit 0
