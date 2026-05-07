#!/usr/bin/env bash
# test_check_scraper_image_shipped.sh — Tests for check-scraper-image-shipped.sh
#
# Builds synthetic (repo, Dockerfile) pairs in a temp directory and
# verifies the checker's exit code matches expectations. Covers the
# four behaviors the issue (#4294) calls out explicitly:
#
#   (a) Current main passes (smoke test against the real repo).
#   (b) A reference to /app/scripts/<X>.py with a matching COPY in the
#       scraper Dockerfile passes.
#   (c) A reference to /app/scripts/<X>.py without a matching COPY
#       fails with stderr naming X.py.
#   (d) A reference to /app/scripts/<X>.sh without a matching COPY
#       fails with stderr naming X.sh.
#
# Plus extras:
#   - Subdirectory references (/app/scripts/dispatcher/X.sh) are out of
#     scope.
#   - References inside other Dockerfiles (Dockerfile.dispatcher, etc.)
#     are excluded — those images manage their own COPYs.
#   - References in docs/ are excluded.
#   - References in tmp/ are excluded.
#   - The check script's own header is excluded (no self-match).
#   - Multiple missing scripts in a single run are all reported.
#
# Usage:
#   scripts/tests/test_check_scraper_image_shipped.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-scraper-image-shipped.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo.
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Helper: clear tmpdir between tests so unrelated fixtures do not leak.
# The "${TMPDIR_TEST:?}" guard ensures the variable is non-empty before
# the rm — without it shellcheck (SC2115) flags the rm as a foot-gun
# because an unset TMPDIR_TEST would expand to `/*` and wipe the disk.
reset_tmpdir() {
    rm -rf "${TMPDIR_TEST:?}"/*
    rm -rf "${TMPDIR_TEST:?}"/.[!.]* 2>/dev/null || true
}

# Helper: write content to a path, creating parent dirs as needed.
write_file() {
    local path="$1"
    local content="$2"
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" > "$path"
}

# Run the check against the synthetic repo at $TMPDIR_TEST.
# The synthetic repo always has its scraper Dockerfile at
# packages/scraper-framework/Dockerfile and the check is invoked with
# --repo-root pointing at TMPDIR_TEST.
#
# We pass --self-path pointing to a path that does not exist inside the
# synthetic repo so the self-exclusion grep never matches anything in
# the fixtures (the real check script lives outside TMPDIR_TEST). Same
# for --repo-root: the real script gets invoked but scans only the
# synthetic tree.
#
# shellcheck disable=SC2120  # "$@" passthrough is intentional for future
# tests that want to add a flag-shape — current callers happen to pass none.
run_check_on_tmp() {
    "$CHECK_SCRIPT" \
        --repo-root "$TMPDIR_TEST" \
        --self-path "scripts/check-scraper-image-shipped.sh" \
        "$@"
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    local output
    output=$(run_check_on_tmp 2>&1) && status=0 || status=$?
    if [[ $status -eq 0 ]]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got exit $status)"
        echo "--- output ---"
        echo "$output"
        echo "--------------"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_fails() {
    local desc="$1"
    local expected_token="${2:-}"
    TESTS=$((TESTS + 1))
    local output
    output=$(run_check_on_tmp 2>&1) && status=0 || status=$?
    if [[ $status -eq 0 ]]; then
        echo "FAIL: $desc (expected failure, got success)"
        echo "--- output ---"
        echo "$output"
        echo "--------------"
        FAILURES=$((FAILURES + 1))
        return
    fi
    if [[ -n "$expected_token" ]] && ! echo "$output" | grep -qF "$expected_token"; then
        echo "FAIL: $desc (exit $status was non-zero but stderr did not mention '$expected_token')"
        echo "--- output ---"
        echo "$output"
        echo "--------------"
        FAILURES=$((FAILURES + 1))
        return
    fi
    echo "PASS: $desc"
}

# Scaffold helper: create a synthetic scraper Dockerfile under TMPDIR_TEST.
# Pass extra COPY targets as varargs (e.g. "rebuild_db.py" "reingest_from_s3.py").
synth_scraper_dockerfile() {
    local out="$TMPDIR_TEST/packages/scraper-framework/Dockerfile"
    mkdir -p "$(dirname "$out")"
    {
        echo 'FROM python:3.12-slim'
        echo 'WORKDIR /app'
        for name in "$@"; do
            echo "COPY scripts/$name /app/scripts/$name"
        done
        echo 'ENTRYPOINT ["python", "-m"]'
    } > "$out"
}

# ─── Test 1: AC #1 — check script exists and is executable ─────────────
TESTS=$((TESTS + 1))
if [[ -x "$CHECK_SCRIPT" ]]; then
    echo "PASS: $CHECK_SCRIPT exists and is executable"
else
    echo "FAIL: $CHECK_SCRIPT exists and is executable"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 2: AC #2 — current main passes ───────────────────────────────
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: Current main passes (real repo, default args)"
else
    echo "FAIL: Current main passes (real repo, default args)"
    "$CHECK_SCRIPT" 2>&1 | head -20
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 3: AC #3 — synthetic missing script fails with clear stderr ──
# This is the AC's exact scenario: a tmp/fake_caller.sh containing
# `python /app/scripts/nonexistent.py` causes the check to fail with
# stderr naming nonexistent.py.
#
# Note: AC says tmp/fake_caller.sh, but the real check excludes tmp/
# (worktree-local scratch). For the test we put the fake caller under
# scripts/ instead, which is the same idea — a non-Dockerfile, non-docs,
# non-tmp file referencing a script that the Dockerfile does not COPY.
reset_tmpdir
synth_scraper_dockerfile  # no scripts COPY'd
write_file "$TMPDIR_TEST/scripts/fake_caller.sh" \
'#!/usr/bin/env bash
# Synthetic caller used only by the test harness.
python /app/scripts/nonexistent.py --foo'
assert_fails \
    "Reference to missing /app/scripts/nonexistent.py is detected" \
    "nonexistent.py"

# ─── Test 4: Reference with matching COPY passes ───────────────────────
reset_tmpdir
synth_scraper_dockerfile "rebuild_db.py"
write_file "$TMPDIR_TEST/scripts/some_caller.sh" \
'#!/usr/bin/env bash
python /app/scripts/rebuild_db.py --county la'
assert_passes "Reference to /app/scripts/rebuild_db.py with matching COPY passes"

# ─── Test 5: Reference to .sh script with matching COPY passes ─────────
reset_tmpdir
synth_scraper_dockerfile "notify-telegram.sh"
write_file "$TMPDIR_TEST/scripts/some_caller.sh" \
'#!/usr/bin/env bash
/app/scripts/notify-telegram.sh "alert text"'
assert_passes "Reference to /app/scripts/notify-telegram.sh with matching COPY passes"

# ─── Test 6: Missing .sh script is detected ────────────────────────────
reset_tmpdir
synth_scraper_dockerfile  # no scripts COPY'd
write_file "$TMPDIR_TEST/scripts/some_caller.sh" \
'#!/usr/bin/env bash
/app/scripts/missing-helper.sh "alert text"'
assert_fails "Reference to missing /app/scripts/missing-helper.sh is detected" \
    "missing-helper.sh"

# ─── Test 7: Subdirectory references are out of scope ──────────────────
# /app/scripts/dispatcher/agent-runner-entrypoint.sh is in a different
# image's COPY hierarchy (Dockerfile.dispatcher-agent-runner). The
# check's regex does not match subdir references — only single-segment
# basenames after /app/scripts/.
reset_tmpdir
synth_scraper_dockerfile  # no scripts COPY'd
write_file "$TMPDIR_TEST/scripts/some_caller.sh" \
'#!/usr/bin/env bash
PHASE_TRANSITIONS_DIR="${PHASE_TRANSITIONS_DIR:-/app/scripts/dispatcher}"
exec /app/scripts/dispatcher/agent-runner-entrypoint.sh'
assert_passes "Subdirectory references (/app/scripts/dispatcher/...) are out of scope"

# ─── Test 8: References inside other Dockerfile* are excluded ──────────
# Dockerfile.dispatcher-* manage their own COPY rules. The check only
# enforces against packages/scraper-framework/Dockerfile, so references
# inside any Dockerfile* are excluded.
reset_tmpdir
synth_scraper_dockerfile  # no scripts COPY'd
write_file "$TMPDIR_TEST/Dockerfile.dispatcher-agent-runner" \
'FROM python:3.12-slim
COPY scripts/check-issue-author.sh /app/scripts/check-issue-author.sh
RUN chmod +x /app/scripts/check-issue-author.sh
ENTRYPOINT ["/app/scripts/dispatcher/agent-runner-entrypoint.sh"]'
assert_passes "References inside Dockerfile.dispatcher-agent-runner are excluded"

# ─── Test 9: References under docs/ are excluded ───────────────────────
# Documentation is descriptive, not invocation-site.
reset_tmpdir
synth_scraper_dockerfile  # no scripts COPY'd
write_file "$TMPDIR_TEST/docs/agent/infrastructure-reference.md" \
'# Some doc

The script is at `/app/scripts/example_only.py` inside the image.'
assert_passes "References under docs/ are excluded"

# ─── Test 10: References under tmp/ are excluded ───────────────────────
# Worktree-local scratch never ships and should never trip the check.
reset_tmpdir
synth_scraper_dockerfile  # no scripts COPY'd
write_file "$TMPDIR_TEST/tmp/scratch.sh" \
'#!/usr/bin/env bash
python /app/scripts/scratch_caller.py'
assert_passes "References under tmp/ are excluded"

# ─── Test 11: Multiple missing scripts are all reported ────────────────
reset_tmpdir
synth_scraper_dockerfile "rebuild_db.py"
write_file "$TMPDIR_TEST/scripts/caller_a.sh" 'python /app/scripts/missing_a.py'
write_file "$TMPDIR_TEST/scripts/caller_b.sh" 'python /app/scripts/missing_b.py'
write_file "$TMPDIR_TEST/scripts/caller_c.sh" 'python /app/scripts/rebuild_db.py'

TESTS=$((TESTS + 1))
output=$(run_check_on_tmp 2>&1) && status=0 || status=$?
if [[ $status -eq 0 ]]; then
    echo "FAIL: Multiple missing scripts are all reported (expected failure, got success)"
    FAILURES=$((FAILURES + 1))
elif echo "$output" | grep -qF "missing_a.py" && echo "$output" | grep -qF "missing_b.py"; then
    if echo "$output" | grep -qF "rebuild_db.py" && ! echo "$output" | grep -qE "rebuild_db\.py.*\(referenced at"; then
        # rebuild_db.py SHOULD appear in the recovery hint suggestion list ONLY
        # if listed as missing — but it's NOT missing here. Check it's not
        # listed as missing.
        echo "PASS: Multiple missing scripts are all reported"
    elif echo "$output" | grep -qE "rebuild_db\.py.*\(referenced at"; then
        echo "FAIL: rebuild_db.py wrongly reported as missing"
        echo "--- output ---"
        echo "$output"
        echo "--------------"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: Multiple missing scripts are all reported"
    fi
else
    echo "FAIL: Multiple missing scripts are all reported (output did not mention both missing names)"
    echo "--- output ---"
    echo "$output"
    echo "--------------"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 12: Same script referenced from multiple files is deduped ────
# The error message names the script once, and the file:line points to
# the FIRST reference site (deterministic ordering by grep -r).
reset_tmpdir
synth_scraper_dockerfile  # no scripts COPY'd
write_file "$TMPDIR_TEST/scripts/caller_a.sh" 'python /app/scripts/dup.py'
write_file "$TMPDIR_TEST/scripts/caller_b.sh" 'python /app/scripts/dup.py'

TESTS=$((TESTS + 1))
output=$(run_check_on_tmp 2>&1) && status=0 || status=$?
dup_count=$(echo "$output" | grep -cE "^\s+- dup\.py " || true)
if [[ $status -ne 0 && "$dup_count" == "1" ]]; then
    echo "PASS: Same script referenced from multiple files is deduped"
else
    echo "FAIL: Same script referenced from multiple files is deduped (status=$status, dup_count=$dup_count)"
    echo "--- output ---"
    echo "$output"
    echo "--------------"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 13: Word-boundary check rejects /app/scripts/X.python ────────
# A reference like /app/scripts/foo.python should NOT match (it's a
# bogus extension, not .py followed by a non-word character).
reset_tmpdir
synth_scraper_dockerfile  # no scripts COPY'd
write_file "$TMPDIR_TEST/scripts/some_caller.sh" \
'#!/usr/bin/env bash
# This string contains /app/scripts/foo.python which is NOT a real
# python invocation — the regex must not match it.
echo "/app/scripts/foo.python is not a real reference"'
assert_passes "Word-boundary check rejects /app/scripts/X.python (not .py)"

# ─── Test 14: COPY directive with different destination does not satisfy ─
# The check requires exact "COPY scripts/X /app/scripts/X" — a COPY to
# a different path does not satisfy the contract that the script is
# reachable at /app/scripts/X.
reset_tmpdir
mkdir -p "$TMPDIR_TEST/packages/scraper-framework"
{
    echo 'FROM python:3.12-slim'
    echo 'WORKDIR /app'
    echo 'COPY scripts/wrong_dest.py /app/elsewhere/wrong_dest.py'  # wrong dest
} > "$TMPDIR_TEST/packages/scraper-framework/Dockerfile"
write_file "$TMPDIR_TEST/scripts/some_caller.sh" \
'python /app/scripts/wrong_dest.py'
assert_fails \
    "COPY to a different destination does not satisfy /app/scripts/X.py" \
    "wrong_dest.py"

# ─── Test 15: Self-match guard — check's own header references /app/scripts ─
# The check script's header text mentions /app/scripts/<X>.py several
# times in prose. Running the check on the real repo (Test 2) already
# covers this — if the self-exclusion broke, Test 2 would fail. This
# test makes the assertion explicit.
TESTS=$((TESTS + 1))
header_hits=$(grep -cE '/app/scripts/[A-Za-z_][A-Za-z0-9_-]*\.(py|sh)' "$CHECK_SCRIPT" || true)
if [[ "$header_hits" -gt 0 ]] && "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: Check's own header contains /app/scripts/<X>.{py,sh} prose ($header_hits hits) but does not self-trip"
else
    echo "FAIL: Self-match guard (header_hits=$header_hits, status=$?)"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 15b: References under .claude/ are excluded (#4300) ──────────
# The dispatcher test suite creates synthetic agent worktrees under
# .claude/worktrees/agent-<MagicMock id=...>/scripts/... when running
# the scripts-tests pytest shard in CI. Those leftover fixtures may
# contain test heredocs with literal `python /app/scripts/<X>.py`
# strings, which would otherwise trip the check at the next CI step.
# Reproduce the shape of that fixture and assert the check ignores it.
reset_tmpdir
synth_scraper_dockerfile  # no scripts COPY'd
write_file "$TMPDIR_TEST/.claude/worktrees/agent-fake/scripts/tests/test_check_scraper_image_shipped.sh" \
'#!/usr/bin/env bash
# Synthetic leftover from dispatcher tests — must NOT trip the check.
python /app/scripts/nonexistent.py --foo'
assert_passes "References under .claude/ are excluded (#4300 regression)"

# ─── Test 16: No self-match on ci.yml step name ────────────────────────
# Per CLAUDE.md §Hygiene-check CI steps, every new string-forbidding
# guard must not have a CI step name that itself contains a forbidden
# pattern. Our pattern is /app/scripts/<X>.{py,sh} — no CI step name
# should literally contain that prose, since the check would trip on
# its own step name during the workflow run.
TESTS=$((TESTS + 1))
ci_yml="$REPO_ROOT/.github/workflows/ci.yml"
if [[ -f "$ci_yml" ]]; then
    if grep -qE '/app/scripts/[A-Za-z_][A-Za-z0-9_-]*\.(py|sh)' "$ci_yml"; then
        echo "FAIL: ci.yml contains /app/scripts/<X>.{py,sh} prose — guard would self-match"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: No self-match on ci.yml — no /app/scripts/<X>.{py,sh} prose in workflow"
    fi
else
    echo "PASS: No self-match on ci.yml step name (ci.yml missing — skipped)"
fi

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
