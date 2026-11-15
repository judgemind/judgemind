#!/usr/bin/env bash
# check-no-unflagged-page-cap.sh — every page / chunk cap must be flagged (#4995).
#
# A hard-coded page limit that stops reading silently drops every ruling
# past it while the split still passes as complete.  It happened twice: the
# OCR fallback stopped at page 10 (#1332, fixed by #4986) and the multimodal
# extractor at page 50 (#4995).  A cap is fine when it either bounds a
# runaway document far above real calendars, or marks the result incomplete
# when hit (``PdfText.incomplete`` / ``PageCapExceededError`` /
# ``ExtractionIncompleteError``) so the worker refuses the split and a
# reingest retries the key.  Either way it must say so where it is defined
# and enforced.
#
# This check fails when a Python file under packages/*/src or scripts/
# (tests, scripts/archive and the dispatcher, which pages GitHub issue
# lists, excluded) has a page / chunk cap line without a
# ``page-cap:`` marker comment on the same line or the line directly above:
#
#     # page-cap: <how hitting the cap is flagged, or why it cannot drop rulings>
#
# Cap lines are:
#   - a cap name assigned a number: ``_OCR_MAX_PAGES = 100``,
#     ``max_pages: int = 50``, ``DEFAULT_MAX_PAGES = 10``, ``MAX_CHUNKS = 10``
#   - a comparison against a cap name: ``if i >= max_pages``,
#     ``len(chunks) < _MAX_CHUNKS``
#   - slicing a page list: ``pdf.pages[:10]``, ``doc[:n]``
#   - ``islice(`` / ``range(<number>`` on a line that mentions pages
#
# Usage:
#   scripts/check-no-unflagged-page-cap.sh          # scan packages/*/src and scripts/
#   scripts/check-no-unflagged-page-cap.sh DIR...   # scan the given directories
#
# Exit codes:
#   0 — every cap line is flagged.
#   1 — at least one cap line has no page-cap: marker.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

if [[ $# -gt 0 ]]; then
    SCAN_DIRS=("$@")
else
    SCAN_DIRS=("$REPO_ROOT/scripts")
    for src in "$REPO_ROOT"/packages/*/src; do
        [[ -d "$src" ]] && SCAN_DIRS+=("$src")
    done
fi

NAME='[A-Za-z0-9_]*(MAX_PAGES|max_pages|MAX_CHUNKS|max_chunks|PAGE_CAP|page_cap|PAGE_LIMIT|page_limit|_PAGES)'
# A cap name assigned a number (constant, default argument, keyword argument).
DEFINE="(^|[^A-Za-z0-9_.])(self\\.)?${NAME}[[:space:]]*(:[^=]*)?=[[:space:]]*[0-9]"
# A comparison against a cap.
COMPARE="(>=?|<=?|==)[[:space:]]*(self\\.)?${NAME}([^A-Za-z0-9_]|$)"
# Slicing a list of pages.
SLICE='(pages|doc)\)?\[[^]]*:'
# islice / range over a fixed count, on a line about pages.
COUNTED='islice\(|range\([[:space:]]*(min\([[:space:]]*)?[0-9]+[[:space:]]*[,)]'
MARKER='page-cap:[[:space:]]*[^[:space:]]'

# The dispatcher pages GitHub issue lists, not documents.  --include comes
# first: GNU grep includes a file no option matches unless the first
# include/exclude option is --include (BSD grep does not care).
exclude_args=(--include='*.py' --exclude='test_*' --exclude-dir=tests --exclude-dir=archive
    --exclude-dir=dispatcher --exclude-dir=dispatcher_v3)
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

matches=$(grep -rnE "$DEFINE|$COMPARE|$SLICE|$COUNTED" "${SCAN_DIRS[@]}" "${exclude_args[@]}" \
    2>/dev/null || true)

violations=0
if [[ -n "$matches" ]]; then
    while IFS= read -r line; do
        file_path="${line%%:*}"
        rest="${line#*:}"
        line_no="${rest%%:*}"
        content="${rest#*:}"

        # Comment lines may name caps as context.
        if [[ "$content" =~ ^[[:space:]]*# ]]; then
            continue
        fi
        # islice / range(N) count only on a line about pages.
        if ! grep -qE "$DEFINE|$COMPARE|$SLICE" <<< "$content"; then
            if ! grep -qiE 'page' <<< "$content"; then
                continue
            fi
        fi
        if grep -qE "$MARKER" <<< "$content"; then
            continue
        fi
        if [[ "$line_no" -gt 1 ]]; then
            previous=$(sed -n "$((line_no - 1))p" "$file_path")
            if grep -qE "$MARKER" <<< "$previous"; then
                continue
            fi
        fi

        if [[ $violations -eq 0 ]]; then
            echo "ERROR: page / chunk cap with no page-cap: marker:"
            echo ""
        fi
        echo "    $file_path:$line_no:$content"
        violations=$((violations + 1))
    done <<< "$matches"
fi

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations unflagged cap line(s)."
    echo "  A cap that stops reading silently drops every ruling past it while"
    echo "  the split passes as complete (#4986 OCR at page 10, #4995 multimodal"
    echo "  at page 50)."
    echo ""
    echo "  Fix: make hitting the cap visible, then mark the line.  Either"
    echo "  raise it to a bound real calendars never reach (with a cost note),"
    echo "  or, when it is hit, log a warning naming total_pages and mark the"
    echo "  result incomplete so the worker refuses the split:"
    echo ""
    echo "    from framework.llm_extractor import PageCapExceededError"
    echo "    if total_pages > max_pages:"
    echo "        logger.warning(\"...page_limit_reached\", max_pages=max_pages, total_pages=total_pages)"
    echo "        if strict:"
    echo "            raise PageCapExceededError(f\"PDF has {total_pages} pages\")"
    echo ""
    echo "  (OCR text: return PdfText(..., incomplete=\"page cap N of M pages\").)"
    echo "  Then put a marker on the cap line or the line directly above:"
    echo ""
    echo "    # page-cap: <how hitting the cap is flagged, or why it cannot drop rulings>"
    exit 1
fi

echo "All clean: every page / chunk cap carries a page-cap: marker."
exit 0
