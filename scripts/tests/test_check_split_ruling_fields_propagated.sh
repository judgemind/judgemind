#!/usr/bin/env bash
# test_check_split_ruling_fields_propagated.sh — tests for the SplitRuling
# field-propagation hygiene guard (issue #4298).
#
# Synthesizes a tiny ``packages/scraper-framework/src/`` tree and a
# matching ``scripts/reingest_from_s3.py`` under a temp dir, then exercises
# the underlying Python scanner against both pass and fail cases.
#
# Usage
# -----
#   scripts/tests/test_check_split_ruling_fields_propagated.sh
#
# Exit codes
# ----------
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY_SCRIPT="$REPO_ROOT/scripts/check_split_ruling_fields_propagated.py"
WRAPPER_SCRIPT="$REPO_ROOT/scripts/check-split-ruling-fields-propagated.sh"

FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Synthesize a minimal scraper-framework + reingest layout ────────────
# Each test sets up a tree under $TMPDIR_TEST/{src,reingest.py} and
# invokes the Python scanner with overrides.

write_dataclass() {
    # write_dataclass <module-name> <class-name> <field1> <field2> ...
    local module="$1" class_name="$2"
    shift 2
    local fields=("$@")
    local courts_dir="$TMPDIR_TEST/src/courts/ca"
    mkdir -p "$courts_dir"
    local path="$courts_dir/$module.py"
    {
        printf 'from dataclasses import dataclass, field\n\n'
        printf '@dataclass\n'
        printf 'class %s:\n' "$class_name"
        for f in "${fields[@]}"; do
            printf '    %s: str | None = None\n' "$f"
        done
    } > "$path"
}

write_worker() {
    # write_worker <function-name> <field1> <field2> ...
    local fn_name="$1"
    shift
    local fields=("$@")
    local worker_dir="$TMPDIR_TEST/src/ingestion"
    mkdir -p "$worker_dir"
    local path="$worker_dir/worker.py"
    {
        printf 'def %s(event_data, document_id, ruling_text, dispatch):\n' "$fn_name"
        printf '    sr = None\n'
        printf '    split_event: dict = {\n'
        for f in "${fields[@]}"; do
            printf '        "%s": None,\n' "$f"
        done
        printf '    }\n'
        printf '    return True\n'
    } > "$path"
}

write_reingest() {
    # write_reingest <field1> <field2> ...
    local fields=("$@")
    local path="$TMPDIR_TEST/reingest.py"
    {
        printf 'def _full_reparse_document(raw_content, scraper_id, doc_meta):\n'
        printf '    results = []\n'
        printf '    for ruling in []:\n'
        printf '        extracted: dict = {\n'
        for f in "${fields[@]}"; do
            printf '            "%s": None,\n' "$f"
        done
        printf '        }\n'
        printf '        results.append(extracted)\n'
        printf '    return results\n'
    } > "$path"
}

run_check() {
    # run_check — invokes the Python scanner with the synthesized paths.
    # Returns its exit code.  Stdout + stderr are captured into globals
    # so individual asserts can grep them.
    last_stdout=$(mktemp)
    last_stderr=$(mktemp)
    set +e
    python3 "$PY_SCRIPT" \
        --scraper-framework "$TMPDIR_TEST/src" \
        --reingest "$TMPDIR_TEST/reingest.py" \
        --quiet-whitelisted \
        > "$last_stdout" \
        2> "$last_stderr"
    local rc=$?
    set -e
    return $rc
}

# ─── Test 1: All fields propagated → exit 0 ──────────────────────────────
write_dataclass la_tentatives LASplitRuling ruling_index case_number ruling_text \
    judge_name department
write_worker _try_la_html_split case_number ruling_text judge_name department
write_reingest case_number ruling_text judge_name department
TESTS=$((TESTS + 1))
if run_check; then
    echo "PASS: Test 1 — all fields propagated (exit 0)"
else
    echo "FAIL: Test 1 — expected exit 0, got $? ($(cat "$last_stdout") | $(cat "$last_stderr"))"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 2: Worker drops a field → exit 1, names that field + worker ────
# Hand-built dataclass-vs-worker mismatch (AC2 of #4298).
write_dataclass la_tentatives LASplitRuling ruling_index case_number ruling_text \
    judge_name department
# Worker missing judge_name
write_worker _try_la_html_split case_number ruling_text department
write_reingest case_number ruling_text judge_name department
TESTS=$((TESTS + 1))
if run_check; then
    echo "FAIL: Test 2 — expected exit 1 (worker drops judge_name), got 0"
    FAILURES=$((FAILURES + 1))
elif grep -q "LASplitRuling.judge_name" "$last_stdout" \
    && grep -q "_try_la_html_split" "$last_stdout"; then
    echo "PASS: Test 2 — worker mismatch correctly named (judge_name + _try_la_html_split)"
else
    echo "FAIL: Test 2 — output did not name LASplitRuling.judge_name AND _try_la_html_split"
    echo "  stdout: $(cat "$last_stdout")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 3: Reingest drops a field → exit 1 names that field + fn ───────
write_dataclass la_tentatives LASplitRuling ruling_index case_number ruling_text \
    judge_name department
write_worker _try_la_html_split case_number ruling_text judge_name department
# Reingest missing department
write_reingest case_number ruling_text judge_name
TESTS=$((TESTS + 1))
if run_check; then
    echo "FAIL: Test 3 — expected exit 1 (reingest drops department), got 0"
    FAILURES=$((FAILURES + 1))
elif grep -q "LASplitRuling.department" "$last_stdout" \
    && grep -q "_full_reparse_document" "$last_stdout"; then
    echo "PASS: Test 3 — reingest mismatch correctly named (department + _full_reparse_document)"
else
    echo "FAIL: Test 3 — output did not name LASplitRuling.department AND _full_reparse_document"
    echo "  stdout: $(cat "$last_stdout")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 4: ruling_index (internal) is excluded ─────────────────────────
# A dataclass with ONLY ruling_index — no payload fields — must not flag
# anything if neither worker nor reingest mentions it.
write_dataclass la_tentatives LASplitRuling ruling_index case_number
write_worker _try_la_html_split case_number
write_reingest case_number
TESTS=$((TESTS + 1))
if run_check; then
    echo "PASS: Test 4 — ruling_index is excluded from propagation check"
else
    echo "FAIL: Test 4 — ruling_index should be in _INTERNAL_FIELDS exclusion"
    echo "  stdout: $(cat "$last_stdout")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 5: Reingest accepts ruling.<field> attribute access ────────────
# Even if a field is not in the extracted dict literal, ``ruling.<field>``
# attribute access on the loop variable counts as propagation.
write_dataclass la_tentatives LASplitRuling ruling_index case_number ruling_text \
    judge_name
write_worker _try_la_html_split case_number ruling_text judge_name
# Reingest extracts only case_number + ruling_text in its dict, but uses
# ruling.judge_name elsewhere.
cat > "$TMPDIR_TEST/reingest.py" <<'PYEOF'
def _full_reparse_document(raw_content, scraper_id, doc_meta):
    results = []
    for ruling in []:
        # Direct ruling.<field> access counts as propagation.
        judge = ruling.judge_name
        extracted: dict = {
            "case_number": None,
            "ruling_text": None,
            "judge_name": judge,
        }
        results.append(extracted)
    return results
PYEOF
TESTS=$((TESTS + 1))
if run_check; then
    echo "PASS: Test 5 — ruling.<field> attribute access counts as propagation"
else
    echo "FAIL: Test 5 — direct ruling.judge_name access should satisfy the check"
    echo "  stdout: $(cat "$last_stdout")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 6: Reingest accepts getattr(ruling, "<field>", ...) access ─────
write_dataclass la_tentatives LASplitRuling ruling_index case_number ruling_text \
    department
write_worker _try_la_html_split case_number ruling_text department
cat > "$TMPDIR_TEST/reingest.py" <<'PYEOF'
def _full_reparse_document(raw_content, scraper_id, doc_meta):
    results = []
    for ruling in []:
        # getattr(ruling, "<field>", default) counts as propagation.
        dept = getattr(ruling, "department", None)
        extracted: dict = {
            "case_number": None,
            "ruling_text": None,
            "department": dept,
        }
        results.append(extracted)
    return results
PYEOF
TESTS=$((TESTS + 1))
if run_check; then
    echo "PASS: Test 6 — getattr(ruling, ...) counts as propagation"
else
    echo "FAIL: Test 6 — getattr(ruling, 'department', None) should satisfy the check"
    echo "  stdout: $(cat "$last_stdout")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 7: Adding a wholly new dataclass requires scope registration ───
# A new ``FooSplitRuling`` not in _DATACLASS_SCOPE must trigger a
# blocking violation so contributors can't silently skip the check.
write_dataclass foo_tentatives FooSplitRuling ruling_index case_number
write_worker _try_la_html_split case_number ruling_text
write_reingest case_number ruling_text
TESTS=$((TESTS + 1))
if run_check; then
    echo "FAIL: Test 7 — new unscoped dataclass should block (got exit 0)"
    FAILURES=$((FAILURES + 1))
elif grep -q "FooSplitRuling" "$last_stdout" \
    && grep -q "_DATACLASS_SCOPE" "$last_stdout"; then
    echo "PASS: Test 7 — new unscoped dataclass triggers _DATACLASS_SCOPE violation"
else
    echo "FAIL: Test 7 — output did not name FooSplitRuling + _DATACLASS_SCOPE"
    echo "  stdout: $(cat "$last_stdout")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 8: __slots__-style classes are recognized ──────────────────────
# Fresno + Riverside SplitRuling use ``__slots__`` instead of @dataclass.
# The scanner must extract the field names from the slots tuple.
courts_dir="$TMPDIR_TEST/src/courts/ca"
mkdir -p "$courts_dir"
cat > "$courts_dir/fresno_tentatives.py" <<'PYEOF'
class SplitRuling:
    __slots__ = (
        "ruling_index",
        "case_number",
        "ruling_text",
        "department",
    )
    def __init__(self, ruling_index, case_number, ruling_text, department=None):
        self.ruling_index = ruling_index
        self.case_number = case_number
        self.ruling_text = ruling_text
        self.department = department
PYEOF
write_worker _try_fresno_pdf_split case_number ruling_text department
write_reingest case_number ruling_text department
TESTS=$((TESTS + 1))
if run_check; then
    echo "PASS: Test 8 — __slots__-style SplitRuling is recognized + checked"
else
    echo "FAIL: Test 8 — __slots__ class fields not extracted correctly"
    echo "  stdout: $(cat "$last_stdout")"
    echo "  stderr: $(cat "$last_stderr")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 9: Whitelisted gap doesn't trigger blocking violation ──────────
# ``LASplitRuling.ruling_text_html`` is in the production codebase's
# _KNOWN_PROPAGATION_GAPS for the reingest target.  When the live
# scanner runs against the live tree, exit code is 0.  Verifying the
# whitelist mechanism in synthesized form requires reproducing the
# whitelist semantics — easier to assert via the wrapper script's exit
# code on the live tree.
TESTS=$((TESTS + 1))
if "$WRAPPER_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: Test 9 — whitelisted live-tree gaps don't block CI"
else
    echo "FAIL: Test 9 — wrapper script failed on the live codebase"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 10: Wrapper script prints fix guidance on failure ──────────────
# Synthesize a minimal failing tree (worker drops a field), invoke the
# wrapper, confirm its stdout includes the fix-options block.
write_dataclass la_tentatives LASplitRuling ruling_index case_number ruling_text \
    judge_name
write_worker _try_la_html_split case_number ruling_text
write_reingest case_number ruling_text judge_name
# The wrapper script doesn't accept overrides — call it indirectly via
# environment so the underlying Python scanner uses the synthesized
# paths.  Easiest path: call python directly and verify exit + content,
# since wrapper-script branding is the same.
TESTS=$((TESTS + 1))
last_stdout_combined=$(mktemp)
set +e
python3 "$PY_SCRIPT" \
    --scraper-framework "$TMPDIR_TEST/src" \
    --reingest "$TMPDIR_TEST/reingest.py" \
    --quiet-whitelisted \
    > "$last_stdout_combined" \
    2>&1
rc=$?
set -e
if [[ $rc -eq 1 ]] \
    && grep -q "LASplitRuling.judge_name" "$last_stdout_combined" \
    && grep -q "_try_la_html_split" "$last_stdout_combined" \
    && grep -q "propagation gap" "$last_stdout_combined"; then
    echo "PASS: Test 10 — failure output names dataclass.field + function + summary"
else
    echo "FAIL: Test 10 — failure output missing expected strings"
    echo "  rc: $rc"
    echo "  stdout: $(cat "$last_stdout_combined")"
    FAILURES=$((FAILURES + 1))
fi
rm -f "$last_stdout_combined"
reset_tmpdir

# ─── Test 11: Live-codebase contract — every ``*SplitRuling`` is scoped ──
# Defensive: if a contributor adds a new ``*SplitRuling`` to the live
# tree, the wrapper script (Test 9) is the canonical check.  Test 11
# adds a redundancy — it asserts that the Python scanner's
# _DATACLASS_SCOPE table covers every dataclass discovered under
# packages/scraper-framework/src/courts/.  This catches the case where
# the dataclass is added but the scope table edit was forgotten —
# Test 9 catches it via the live wrapper, but Test 11 names the gap
# explicitly with a clearer error.
TESTS=$((TESTS + 1))
live_scan_output=$(mktemp)
set +e
python3 "$PY_SCRIPT" > "$live_scan_output" 2>&1
live_rc=$?
set -e
# Either rc=0 (everything scoped + propagated, or only whitelisted gaps),
# or rc=1 with no _DATACLASS_SCOPE violations.  The latter case means
# everything is scoped but at least one propagation gap exists that is
# not whitelisted — which Test 9 (`assert_passes`) already covers.
if grep -q "_DATACLASS_SCOPE" "$live_scan_output"; then
    echo "FAIL: Test 11 — a *SplitRuling exists that is not registered in _DATACLASS_SCOPE"
    cat "$live_scan_output"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: Test 11 — every live *SplitRuling is registered in _DATACLASS_SCOPE"
fi
rm -f "$live_scan_output"

# ─── Self-match guard — N/A ──────────────────────────────────────────────
# The peer ``check-no-*.sh`` self-match helper is for guards that grep
# arbitrary text and might match their own ci.yml step name.  This
# guard's scan target is fixed (``packages/scraper-framework/src/courts/``
# Python files only) and uses Python AST parsing, not pattern matching.
# A ci.yml step name cannot trip it.

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
