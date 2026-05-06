#!/usr/bin/env bash
# check-split-ruling-fields-propagated.sh — CI hygiene guard that verifies
# every ``*SplitRuling`` dataclass field is propagated through the
# ingestion worker's split-event dispatcher AND the reingest path's
# ``_full_reparse_document`` (issue #4298).
#
# Why this check exists
# ---------------------
# ``LASplitRuling`` had no ``judge_name`` field for years (#4282) because
# the worker's ``_try_la_html_split`` dispatcher carried a stale comment
# ("LASplitRuling has no judge_name field — preserve whatever the
# scraper provided") and there was no static check to flag the gap.
# Filed as #4298 to add that check.
#
# What this script does
# ---------------------
# Delegates to ``scripts/check_split_ruling_fields_propagated.py`` which:
#
#   1. AST-parses every ``*SplitRuling`` dataclass under
#      ``packages/scraper-framework/src/courts/`` to extract its field set.
#   2. AST-parses each ``_try_<county>_split`` function in
#      ``packages/scraper-framework/src/ingestion/worker.py`` and the LA-
#      specific multi-ruling path in
#      ``scripts/reingest_from_s3.py::_full_reparse_document`` to extract
#      the keys assigned to ``split_event`` / ``extracted`` dicts and the
#      ``ruling.<field>`` attribute accesses on the loop variable.
#   3. For each ``*SplitRuling`` field NOT already known-excluded
#      (``ruling_index`` is internal) or known-gap-whitelisted (e.g.
#      ``parties`` reingest gap, tracked in #4300), flags missed
#      pass-through cases.
#
# Usage
# -----
#   scripts/check-split-ruling-fields-propagated.sh
#
# The script accepts no arguments — paths default to repo standards.
# For unit-testing the underlying scanner against synthesized inputs,
# call the Python script directly with ``--scraper-framework`` /
# ``--worker`` / ``--reingest`` overrides.
#
# Exit codes
# ----------
#   0 — All ``*SplitRuling`` fields are propagated, modulo the documented
#       exclusion + known-gap lists.
#   1 — At least one propagation gap was detected — see stdout for the
#       specific ``DataclassName.field`` pair and which function it is
#       missing from.
#
# Issue: #4298.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── Run the AST scanner ────────────────────────────────────────────────
# Capture stdout (violations) and stderr (whitelisted notices + summary)
# separately so the report below stays tidy on the failure path.
violations_file="$(mktemp)"
trap 'rm -f "$violations_file"' EXIT

if python3 "$REPO_ROOT/scripts/check_split_ruling_fields_propagated.py" \
    > "$violations_file"; then
    # No blocking violations — the scanner exited 0.  Whitelisted
    # notices, if any, were printed to stderr already.
    exit 0
fi

# ─── Report violations ───────────────────────────────────────────────────
echo "ERROR: *SplitRuling dataclass field(s) are not propagated through" \
     "the worker / reingest split paths."
echo ""
echo "  This means a per-county SplitRuling carries a field that the"
echo "  worker's split-event dispatcher (or the reingest path's"
echo "  _full_reparse_document) silently drops.  See issue #4298 for the"
echo "  motivation; #4282 is the latent bug class this check prevents."
echo ""
echo "  Fix options:"
echo "    1. Propagate the field through the named function — add it"
echo "       to the split_event / extracted dict literal."
echo "    2. If the field is intentionally not part of the split-event"
echo "       payload, add it to _INTERNAL_FIELDS in the Python helper"
echo "       (rare — usually only loop counters belong here)."
echo "    3. If the gap is tracked in a follow-up issue, add it to"
echo "       _KNOWN_PROPAGATION_GAPS with the issue number."
echo ""
echo "  Violating dataclass.field pairs:"
echo ""

while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    echo "    $line"
done < "$violations_file"

exit 1
