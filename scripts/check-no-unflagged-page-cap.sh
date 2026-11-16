#!/usr/bin/env bash
# scans: packages/*/src/ scripts/
# (#5088: paths this guard reads; check-guard-path-filters.sh checks its CI job runs when they change.)
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
#     ``max_pages: int = 50``, ``DEFAULT_MAX_PAGES = 10``, ``MAX_CHUNKS = 10``,
#     ``max_pdf_pages = 50``, ``MAX_PAGE_COUNT = 40``, ``OCR_PAGE_BUDGET = 10``,
#     ``MAX_PAGES = int(os.environ.get("MAX_PAGES", "50"))``.  A cap name is
#     any identifier, in any case, that ends in a page word after a cap word
#     (max / limit / cap / budget ... page[s]) or in a cap word after a page
#     word (page ... max / limit / cap / budget / count) (#5089).
#   - a comparison against a cap name: ``if i >= max_pages``,
#     ``len(chunks) < _MAX_CHUNKS``
#   - slicing a page list: ``pdf.pages[:10]``, ``doc[:n]``
#   - ``islice(`` / ``range(<number>`` on a line that mentions pages
#   - ``if <expr> >= <number>: break`` inside a ``for`` loop over pages
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

# Cap names (#5089: any case, any word order).  The identifier must END in
# the page word or the cap word, so max_pdf_pages, MAX_PAGE_COUNT,
# OCR_PAGE_BUDGET and page_limit are caps, while _SCANNED_PAGE_MAX_TEXT_CHARS
# (a per-page threshold) and max_page_parse_retries are not.
ID='[A-Za-z0-9_]'
CAP_WORD='([Mm][Aa][Xx]|[Ll][Ii][Mm][Ii][Tt]|[Cc][Aa][Pp]|[Bb][Uu][Dd][Gg][Ee][Tt])'
PAGE_WORD='[Pp][Aa][Gg][Ee][Ss]?'
COUNT_WORD='[Cc][Oo][Uu][Nn][Tt][Ss]?'
CAP_NAMES="MAX_CHUNKS|max_chunks|_PAGES|${CAP_WORD}${ID}*${PAGE_WORD}|${PAGE_WORD}${ID}*${CAP_WORD}[Ss]?"
# A comparison bound: only cap words (``i < page_count`` is a loop bound).
NAME="${ID}*(${CAP_NAMES})"
# A definition: a ``*_PAGE_COUNT`` assigned a number is a cap too.
DEF_NAME="${ID}*(${CAP_NAMES}|${PAGE_WORD}${ID}*${COUNT_WORD})"
# A non-zero number (``page_count: int = 0`` is a counter), or a number read
# through int()/float(), e.g. ``int(os.environ.get("MAX_PAGES", "50"))``.
CAP_VALUE='(0*[1-9]|(int|float)\(.*[0-9])'
# A cap name assigned a number (constant, default argument, keyword argument).
DEFINE="(^|[^A-Za-z0-9_.])(self\\.)?${DEF_NAME}[[:space:]]*(:[^=]*)?=[[:space:]]*${CAP_VALUE}"
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

# A literal-count early exit inside a loop over pages (#5089):
#     for page in pdf.pages:
#         if i >= 50: break
# names no cap, so the patterns above cannot see it.  Print file:line:text
# for an ``if <expr> >= / > / == <non-zero number>:`` followed by ``break``
# (same line or next code line) inside a ``for`` whose line mentions pages.
LOOP_BREAK_AWK='
function indent(s) { match(s, /^[ \t]*/); return RLENGTH }
FNR == 1 { depth = 0; pending = 0 }
/^[ \t]*($|#)/ { next }
{
    ind = indent($0)
    if (pending) {
        if (ind > pind && $0 ~ /^[ \t]*break[ \t]*(#.*)?$/) print FILENAME ":" pline ":" ptext
        pending = 0
    }
    while (depth > 0 && ind <= loop_ind[depth]) depth--
    if (depth > 0 && $0 ~ /^[ \t]*if[ \t].*(>=|>|==)[ \t]*0*[1-9][0-9]*[ \t]*:/) {
        if ($0 ~ /:[ \t]*break[ \t]*(#.*)?$/) print FILENAME ":" FNR ":" $0
        else { pending = 1; pline = FNR; ptext = $0; pind = ind }
    }
    if ($0 ~ /^[ \t]*(async[ \t]+)?for[ \t].*[Pp][Aa][Gg][Ee].*:[ \t]*(#.*)?$/) loop_ind[++depth] = ind
}'
py_files=$(grep -rlE 'for[[:space:]].*[Pp][Aa][Gg][Ee]' "${SCAN_DIRS[@]}" "${exclude_args[@]}" \
    2>/dev/null || true)
loop_breaks=""
if [[ -n "$py_files" ]]; then
    while IFS= read -r f; do
        hits=$(awk "$LOOP_BREAK_AWK" "$f")
        [[ -n "$hits" ]] && loop_breaks+="$hits"$'\n'
    done <<< "$py_files"
fi

violations=0
seen=""
# Report $1 (file:line:content) unless it is a comment or carries a marker.
# $2 = "about-pages" when an islice / range(N) hit must be on a page line.
check_hit() {
    local line="$1" mode="${2:-}" file_path rest line_no content previous
    file_path="${line%%:*}"
    rest="${line#*:}"
    line_no="${rest%%:*}"
    content="${rest#*:}"

    # A line hit by both scans is reported once.
    case "$seen" in
        *"|$file_path:$line_no|"*) return 0 ;;
    esac
    seen+="|$file_path:$line_no|"
    # Comment lines may name caps as context.
    if [[ "$content" =~ ^[[:space:]]*# ]]; then
        return 0
    fi
    # islice / range(N) count only on a line about pages.
    if [[ "$mode" == "about-pages" ]] && ! grep -qE "$DEFINE|$COMPARE|$SLICE" <<< "$content"; then
        if ! grep -qiE 'page' <<< "$content"; then
            return 0
        fi
    fi
    if grep -qE "$MARKER" <<< "$content"; then
        return 0
    fi
    if [[ "$line_no" -gt 1 ]]; then
        previous=$(sed -n "$((line_no - 1))p" "$file_path")
        if grep -qE "$MARKER" <<< "$previous"; then
            return 0
        fi
    fi

    if [[ $violations -eq 0 ]]; then
        echo "ERROR: page / chunk cap with no page-cap: marker:"
        echo ""
    fi
    echo "    $file_path:$line_no:$content"
    violations=$((violations + 1))
}

if [[ -n "$matches" ]]; then
    while IFS= read -r line; do
        check_hit "$line" about-pages
    done <<< "$matches"
fi
if [[ -n "$loop_breaks" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        check_hit "$line"
    done <<< "$loop_breaks"
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
