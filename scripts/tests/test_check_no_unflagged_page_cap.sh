#!/usr/bin/env bash
# test_check_no_unflagged_page_cap.sh — Tests for check-no-unflagged-page-cap.sh (#4995)
#
# Usage:
#   scripts/tests/test_check_no_unflagged_page_cap.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-unflagged-page-cap.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    mkdir -p "$(dirname "$TMPDIR_TEST/$name")"
    printf '%s\n' "$content" > "$TMPDIR_TEST/$name"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "${TMPDIR_TEST:?}"/*
}

# The two caps that dropped rulings, as they were written.
create_test_file "llm_extractor.py" '    def extract_from_pdf(self, pdf_bytes, *, max_pages: int = 50):'
assert_fails "the old multimodal default max_pages: int = 50 is detected"
reset_tmpdir

create_test_file "llm_extract.py" '_OCR_MAX_PAGES = 10'
assert_fails "the old OCR cap constant is detected"
reset_tmpdir

create_test_file "render.py" 'for i, page in enumerate(doc):
    if i >= max_pages:
        break'
assert_fails "an early break at a cap is detected"
reset_tmpdir

create_test_file "chunks.py" 'while len(chunks) < _MAX_CHUNKS:'
assert_fails "a chunk cap comparison is detected"
reset_tmpdir

create_test_file "slice.py" 'for page in pdf.pages[:10]:'
assert_fails "slicing a page list is detected"
reset_tmpdir

create_test_file "count.py" 'for n in range(20):  # first pages only
    text += doc[n].get_text()'
assert_fails "range(N) on a line about pages is detected"
reset_tmpdir

create_test_file "kwarg.py" 'text = ocr_pdf_document(raw, max_pages=5)'
assert_fails "a keyword argument cap is detected"
reset_tmpdir

create_test_file "attr.py" '        self._max_pages = 50'
assert_fails "an attribute assigned a cap is detected"
reset_tmpdir

for op in '>' '<=' '=='; do
    create_test_file "compare.py" "if page_no $op max_pages:"
    assert_fails "a $op comparison against a cap is detected"
    reset_tmpdir
done

# ── #5089: cap names in other spellings and word orders ───────────────────
for cap in 'max_pdf_pages = 50' 'MAX_PAGE_COUNT = 40' 'OCR_PAGE_BUDGET = 10' \
    'max_ocr_pages: int = 25' 'PAGE_LIMIT = 30' 'MaxPages = 20' \
    'MAX_PAGES = int(os.environ.get("JM_MAX_PAGES", "50"))'; do
    create_test_file "spelling.py" "$cap"
    assert_fails "the cap [$cap] is detected"
    reset_tmpdir
done

create_test_file "budget_cmp.py" 'if n >= ocr_page_budget:'
assert_fails "a comparison against a page budget is detected"
reset_tmpdir

create_test_file "loop_break.py" 'for i, page in enumerate(pdf.pages):
    if i >= 50:
        break
    texts.append(page.extract_text())'
assert_fails "a literal-count break inside a loop over pages is detected"
reset_tmpdir

create_test_file "loop_break_inline.py" 'for i, page in enumerate(doc):
    if i >= 50: break'
assert_fails "a one-line literal-count break inside a loop over pages is detected"
reset_tmpdir

create_test_file "loop_break_flagged.py" 'for i, page in enumerate(pdf.pages):
    # page-cap: a runaway bound far above real calendars.
    if i >= 5000:
        break'
assert_passes "a marked literal-count break passes"
reset_tmpdir

create_test_file "loop_break_other.py" 'for row in rows:
    if retries >= 3:
        break
for i, page in enumerate(pdf.pages):
    text = page.extract_text()
if found > 1:
    break'
assert_passes "a literal-count break outside a loop over pages passes"
reset_tmpdir

create_test_file "not_caps.py" '_SCANNED_PAGE_MAX_TEXT_CHARS = 200
_MAX_PAGE_OUTPUT_TOKENS = 65536
self._max_page_parse_retries = 1
page_count: int = 0
while i < page_count:
    i += 1'
assert_passes "per-page thresholds, retry counts and page counters are not caps"
reset_tmpdir

create_test_file "flagged.py" '# page-cap: past it the result is PdfText.incomplete (#4986).
_OCR_MAX_PAGES = 100'
assert_passes "a cap with a page-cap marker on the line above passes"
reset_tmpdir

create_test_file "inline.py" 'if i >= max_pages:  # page-cap: caller raises PageCapExceededError'
assert_passes "a cap with an inline page-cap marker passes"
reset_tmpdir

create_test_file "empty_marker.py" '# page-cap:
_OCR_MAX_PAGES = 100'
assert_fails "a marker with no reason does not count"
reset_tmpdir

create_test_file "far_marker.py" '# page-cap: flagged
# some other comment
_OCR_MAX_PAGES = 100'
assert_fails "a marker two lines above does not count"
reset_tmpdir

create_test_file "retries.py" 'for attempt in range(3):
    call()
for n in range(1 + self._max_page_parse_retries):
    pass'
assert_passes "range(N) on lines not about pages is ignored"
reset_tmpdir

create_test_file "comment.py" '# Never write max_pages: int = 50 again.'
assert_passes "a comment line naming a cap passes"
reset_tmpdir

create_test_file "none_default.py" 'def extract(pdf, *, max_pages: int | None = None):'
assert_passes "a cap defaulting to None (resolved elsewhere) passes"
reset_tmpdir

create_test_file "tests/test_render.py" '_OCR_MAX_PAGES = 10'
assert_passes "files under tests/ are not scanned"
reset_tmpdir

create_test_file "notes.md" '_OCR_MAX_PAGES = 10'
assert_passes "non-Python files are not scanned"
reset_tmpdir

# The real repository must be clean.
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: the repository has no unflagged page cap"
else
    echo "FAIL: the repository has an unflagged page cap"
    FAILURES=$((FAILURES + 1))
fi

# The failure output carries a Fix block.
create_test_file "llm_extract.py" '_OCR_MAX_PAGES = 10'
TESTS=$((TESTS + 1))
out=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if grep -q '^  Fix:' <<< "$out" && grep -q 'PageCapExceededError' <<< "$out" \
    && grep -q '# page-cap: <' <<< "$out"; then
    echo "PASS: failure output has a Fix block naming the mechanism and the marker"
else
    echo "FAIL: failure output lacks the Fix block"
    FAILURES=$((FAILURES + 1))
fi

echo ""
echo "$((TESTS - FAILURES))/$TESTS passed"
[[ $FAILURES -eq 0 ]]
