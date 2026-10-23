#!/usr/bin/env bash
# test_check_split_ruling_fields_propagated.sh — tests for the SplitRuling
# field-propagation hygiene guard (issue #4298).
#
# Synthesizes a tiny ``packages/scraper-framework/src/`` tree (courts +
# ``ingestion/worker.py``) under a temp dir, then exercises the underlying
# Python scanner against both pass and fail cases.  The worker is the only
# write path (#4845), so there is no reingest file to synthesize.
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

# ─── Synthesize a minimal scraper-framework layout ───────────────────────
# Each test sets up a tree under $TMPDIR_TEST/src and invokes the Python
# scanner with overrides.

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
        printf '        **event_data,\n'
        for f in "${fields[@]}"; do
            printf '        "%s": None,\n' "$f"
        done
        printf '    }\n'
        printf '    return True\n'
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
        --quiet-whitelisted \
        > "$last_stdout" \
        2> "$last_stderr"
    local rc=$?
    set -e
    return $rc
}

run_check_combined() {
    # run_check_combined — like run_check but merges stdout + stderr into
    # $last_combined (Fix blocks + the summary go to stderr) and stores
    # the exit code in $rc.  Passes --worker explicitly.
    last_combined=$(mktemp)
    set +e
    python3 "$PY_SCRIPT" \
        --scraper-framework "$TMPDIR_TEST/src" \
        --worker "$TMPDIR_TEST/src/ingestion/worker.py" \
        --quiet-whitelisted \
        > "$last_combined" 2>&1
    rc=$?
    set -e
}

# ─── Test 1: All fields propagated → exit 0 ──────────────────────────────
write_dataclass la_tentatives LASplitRuling ruling_index case_number ruling_text \
    judge_name department
write_worker _try_la_html_split case_number ruling_text judge_name department
TESTS=$((TESTS + 1))
if run_check; then
    echo "PASS: Test 1 — all fields propagated (exit 0)"
else
    echo "FAIL: Test 1 — expected exit 0 ($(cat "$last_stdout") | $(cat "$last_stderr"))"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 2: Worker drops a field → exit 1, names that field + worker ────
# Hand-built dataclass-vs-worker mismatch (AC2 of #4298).
write_dataclass la_tentatives LASplitRuling ruling_index case_number ruling_text \
    judge_name department
# Worker missing judge_name
write_worker _try_la_html_split case_number ruling_text department
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

# ─── Test 3: Registered dataclass with no worker_fn is not checked ───────
# ``CCSplitRuling`` is registered in _DATACLASS_SCOPE with no worker_fn
# (CC has no worker dispatcher).  Its fields must not be flagged, and it
# must not trip the unknown-dataclass contract.
write_dataclass cc_tentatives CCSplitRuling ruling_index case_number ruling_text \
    judge_name
write_worker _try_la_html_split case_number
TESTS=$((TESTS + 1))
if run_check; then
    echo "PASS: Test 3 — scoped dataclass with no worker_fn is skipped (exit 0)"
else
    echo "FAIL: Test 3 — CCSplitRuling (no worker_fn) should not be checked"
    echo "  stdout: $(cat "$last_stdout")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 4: ruling_index (internal) is excluded ─────────────────────────
# ruling_index is never in the worker's split_event and must not flag.
write_dataclass la_tentatives LASplitRuling ruling_index case_number
write_worker _try_la_html_split case_number
TESTS=$((TESTS + 1))
if run_check; then
    echo "PASS: Test 4 — ruling_index is excluded from propagation check"
else
    echo "FAIL: Test 4 — ruling_index should be in _INTERNAL_FIELDS exclusion"
    echo "  stdout: $(cat "$last_stdout")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 5: Scoped worker_fn missing from worker.py → exit 1 ────────────
# LASplitRuling's scope names _try_la_html_split; if worker.py has no
# such function the scope table is stale and the check must block.
write_dataclass la_tentatives LASplitRuling ruling_index case_number
write_worker _try_other_split case_number
TESTS=$((TESTS + 1))
if run_check; then
    echo "FAIL: Test 5 — expected exit 1 (worker_fn not found), got 0"
    FAILURES=$((FAILURES + 1))
elif grep -q "LASplitRuling.<function not found>" "$last_stdout" \
    && grep -q "_try_la_html_split" "$last_stdout"; then
    echo "PASS: Test 5 — missing worker function is a blocking violation"
else
    echo "FAIL: Test 5 — output did not name <function not found> + _try_la_html_split"
    echo "  stdout: $(cat "$last_stdout")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 6: The removed --reingest flag is rejected ─────────────────────
# #4845 removed the reingest half of the check.  A stale caller passing
# --reingest must fail loudly (argparse exit 2), not be silently ignored.
write_dataclass la_tentatives LASplitRuling ruling_index case_number
write_worker _try_la_html_split case_number
TESTS=$((TESTS + 1))
set +e
python3 "$PY_SCRIPT" \
    --scraper-framework "$TMPDIR_TEST/src" \
    --reingest "$TMPDIR_TEST/reingest.py" \
    > /dev/null 2>&1
rc=$?
set -e
if [[ $rc -eq 2 ]]; then
    echo "PASS: Test 6 — --reingest is no longer accepted (exit 2)"
else
    echo "FAIL: Test 6 — expected argparse exit 2 for --reingest, got $rc"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 7: Adding a wholly new dataclass requires scope registration ───
# A new ``FooSplitRuling`` not in _DATACLASS_SCOPE must trigger a
# blocking violation so contributors can't silently skip the check.
write_dataclass foo_tentatives FooSplitRuling ruling_index case_number
write_worker _try_la_html_split case_number ruling_text
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
# The scanner must extract the field names from the slots tuple — proven
# both ways: full propagation passes, and a dropped slot field blocks.
courts_dir="$TMPDIR_TEST/src/courts/ca"
mkdir -p "$courts_dir"
printf '%s\n' \
    'class SplitRuling:' \
    '    __slots__ = (' \
    '        "ruling_index",' \
    '        "case_number",' \
    '        "ruling_text",' \
    '        "department",' \
    '    )' \
    '    def __init__(self, ruling_index, case_number, ruling_text, department=None):' \
    '        self.ruling_index = ruling_index' \
    '        self.case_number = case_number' \
    '        self.ruling_text = ruling_text' \
    '        self.department = department' \
    > "$courts_dir/fresno_tentatives.py"
write_worker _try_fresno_pdf_split case_number ruling_text department
TESTS=$((TESTS + 1))
if run_check; then
    write_worker _try_fresno_pdf_split case_number ruling_text
    if run_check; then
        echo "FAIL: Test 8 — dropped __slots__ field (department) was not flagged"
        FAILURES=$((FAILURES + 1))
    elif grep -q "SplitRuling.department" "$last_stdout" \
        && grep -q "_try_fresno_pdf_split" "$last_stdout"; then
        echo "PASS: Test 8 — __slots__-style SplitRuling is recognized + checked"
    else
        echo "FAIL: Test 8 — dropped slot field not named in output"
        echo "  stdout: $(cat "$last_stdout")"
        FAILURES=$((FAILURES + 1))
    fi
else
    echo "FAIL: Test 8 — __slots__ class fields not extracted correctly"
    echo "  stdout: $(cat "$last_stdout")"
    echo "  stderr: $(cat "$last_stderr")"
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 9: Wrapper passes on the live codebase ─────────────────────────
# The live tree must be fully propagated (modulo _KNOWN_PROPAGATION_GAPS)
# so CI stays green.
TESTS=$((TESTS + 1))
if "$WRAPPER_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: Test 9 — wrapper passes on the live codebase"
else
    echo "FAIL: Test 9 — wrapper script failed on the live codebase"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 10: Failure output names dataclass.field + function + summary ──
write_dataclass la_tentatives LASplitRuling ruling_index case_number ruling_text \
    judge_name
write_worker _try_la_html_split case_number ruling_text
TESTS=$((TESTS + 1))
run_check_combined
if [[ $rc -eq 1 ]] \
    && grep -q "LASplitRuling.judge_name" "$last_combined" \
    && grep -q "_try_la_html_split" "$last_combined" \
    && grep -q "propagation gap" "$last_combined"; then
    echo "PASS: Test 10 — failure output names dataclass.field + function + summary"
else
    echo "FAIL: Test 10 — failure output missing expected strings"
    echo "  rc: $rc"
    echo "  combined: $(cat "$last_combined")"
    FAILURES=$((FAILURES + 1))
fi
rm -f "$last_combined"
reset_tmpdir

# ─── Test 11: Live-codebase contract — every ``*SplitRuling`` is scoped ──
# Redundant with Test 9, but names a scope-table omission explicitly.
TESTS=$((TESTS + 1))
live_scan_output=$(mktemp)
set +e
python3 "$PY_SCRIPT" > "$live_scan_output" 2>&1
set -e
if grep -q "_DATACLASS_SCOPE" "$live_scan_output"; then
    echo "FAIL: Test 11 — a *SplitRuling exists that is not registered in _DATACLASS_SCOPE"
    cat "$live_scan_output"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: Test 11 — every live *SplitRuling is registered in _DATACLASS_SCOPE"
fi
rm -f "$live_scan_output"

# ─── Test 12: Fix block emitted for missing _DATACLASS_SCOPE entry ───────
# When a new ``*SplitRuling`` is missing from _DATACLASS_SCOPE, the check's
# error output must include a copy-pasteable Fix block under a ``Fix:``
# heading (issue #4322).  The patch literal carries only ``worker_fn`` —
# the obsolete ``reingest`` key (#4845) must not reappear.
write_dataclass xyz_tentatives XYZSplitRuling ruling_index case_number
write_worker _try_la_html_split case_number ruling_text
TESTS=$((TESTS + 1))
run_check_combined
if [[ $rc -eq 1 ]] \
    && grep -q "^Fix:" "$last_combined" \
    && grep -q '"XYZSplitRuling"' "$last_combined" \
    && grep -q '"worker_fn":' "$last_combined" \
    && ! grep -q '"reingest"' "$last_combined"; then
    echo "PASS: Test 12 — Fix block emitted with literal scope-entry patch"
else
    echo "FAIL: Test 12 — output missing Fix block / patch literal (or still names reingest)"
    echo "  rc: $rc"
    echo "  combined: $(cat "$last_combined")"
    FAILURES=$((FAILURES + 1))
fi
rm -f "$last_combined"
reset_tmpdir

# ─── Test 13: Fix block disambiguates SplitRuling by module stem ─────────
# A plain ``SplitRuling`` class (Fresno/Riverside/SF/SC convention) gets
# its scope key suggested as ``SplitRuling@<module-stem>`` so the operator
# can paste it directly into the disambiguated table.  Issue #4322.
courts_dir="$TMPDIR_TEST/src/courts/ca"
mkdir -p "$courts_dir"
printf '%s\n' \
    'class SplitRuling:' \
    '    __slots__ = (' \
    '        "ruling_index",' \
    '        "case_number",' \
    '        "ruling_text",' \
    '    )' \
    '    def __init__(self, ruling_index, case_number, ruling_text):' \
    '        self.ruling_index = ruling_index' \
    '        self.case_number = case_number' \
    '        self.ruling_text = ruling_text' \
    > "$courts_dir/abc_tentatives.py"
write_worker _try_la_html_split case_number ruling_text
TESTS=$((TESTS + 1))
run_check_combined
if [[ $rc -eq 1 ]] \
    && grep -q '"SplitRuling@abc_tentatives"' "$last_combined"; then
    echo "PASS: Test 13 — Fix block uses SplitRuling@<stem> for disambiguated key"
else
    echo "FAIL: Test 13 — Fix block did not name SplitRuling@abc_tentatives"
    echo "  rc: $rc"
    echo "  combined: $(cat "$last_combined")"
    FAILURES=$((FAILURES + 1))
fi
rm -f "$last_combined"
reset_tmpdir

# ─── Test 14: Fix block uses real worker fn name when one exists ─────────
# When ``_try_<county>_<format>_split`` exists in worker.py, the Fix
# block's ``"worker_fn"`` field uses the real name (no placeholder /
# adjust-comment).  Issue #4322 AC #2.
write_dataclass xyz_tentatives XYZSplitRuling ruling_index case_number
write_worker _try_xyz_pdf_split case_number
TESTS=$((TESTS + 1))
run_check_combined
if [[ $rc -eq 1 ]] \
    && grep -q '"_try_xyz_pdf_split"' "$last_combined" \
    && ! grep -q "adjust if the worker hook" "$last_combined"; then
    echo "PASS: Test 14 — Fix block uses real worker fn name (no placeholder comment)"
else
    echo "FAIL: Test 14 — Fix block did not pick up real _try_xyz_pdf_split"
    echo "  rc: $rc"
    echo "  combined: $(cat "$last_combined")"
    FAILURES=$((FAILURES + 1))
fi
rm -f "$last_combined"
reset_tmpdir

# ─── Test 15: Fix block falls back to placeholder when no worker fn ──────
# When no ``_try_<county>_*_split`` function exists in worker.py, the Fix
# block uses the conventional ``_try_<county>_pdf_split`` placeholder and
# includes the "adjust if the worker hook has a different name" comment.
# Issue #4322 AC #2 (negative).
write_dataclass xyz_tentatives XYZSplitRuling ruling_index case_number
write_worker _try_la_html_split case_number ruling_text
TESTS=$((TESTS + 1))
run_check_combined
if [[ $rc -eq 1 ]] \
    && grep -q '"_try_xyz_pdf_split"' "$last_combined" \
    && grep -q "adjust if the worker hook" "$last_combined"; then
    echo "PASS: Test 15 — Fix block uses placeholder + adjust-comment when no worker fn matches"
else
    echo "FAIL: Test 15 — Fix block missing placeholder _try_xyz_pdf_split + adjust-comment"
    echo "  rc: $rc"
    echo "  combined: $(cat "$last_combined")"
    FAILURES=$((FAILURES + 1))
fi
rm -f "$last_combined"
reset_tmpdir

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
