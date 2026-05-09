#!/usr/bin/env bash
# test_check_shipped_pr.sh — Tests for scripts/check-shipped-pr.sh.
#
# Mocks the gh CLI on PATH and asserts the documented exit codes:
#   0 — high-confidence shipped match (≥1 added overlap or ≥2 total overlap)
#   1 — no high-confidence match
#   2 — error (missing argument, gh CLI unavailable, malformed input)
#
# Usage:
#   scripts/tests/test_check_shipped_pr.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/check-shipped-pr.sh"
FAILURES=0
TESTS=0

# ─── Helpers ──────────────────────────────────────────────────────────────

# Cleanup of temp directories + PATH restore via the shared helper
# (see #4343).
. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"
ORIG_PATH_SAVE=""
restore_path() {
    if [[ -n "$ORIG_PATH_SAVE" ]]; then
        export PATH="$ORIG_PATH_SAVE"
    fi
}
register_cleanup_hook restore_path

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# Reset PATH/env for each scenario (mock_gh writes a fresh gh script).
ORIG_PATH_SAVE="$PATH"
MOCK_BIN_DIR=$(mktemp -d)
register_temp_dir "$MOCK_BIN_DIR"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

# Path to the mock gh script that test cases overwrite.
MOCK_GH="$MOCK_BIN_DIR/gh"

# ─── Precondition: wrapper exists and is executable ───────────────────────

if [[ ! -x "$WRAPPER" ]]; then
    echo "FAIL: $WRAPPER is not executable (or does not exist)" >&2
    exit 1
fi

# ─── Test 1: exit 2 when called with no argument ──────────────────────────

exit_code=0
"$WRAPPER" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 with no argument"
else
    fail "exits 2 with no argument" "expected exit 2, got $exit_code"
fi

# ─── Test 2: exit 2 with non-numeric argument ─────────────────────────────

exit_code=0
"$WRAPPER" not-a-number > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 with non-numeric argument"
else
    fail "exits 2 with non-numeric argument" "expected exit 2, got $exit_code"
fi

# ─── Test 3: high-confidence shipped match (added overlap = 1) ────────────

# Issue body cites scripts/foo.sh (one path); merged PR #3229 added that
# exact file. Expect exit 0 + "shipped:" line on stdout.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
# Top-level dispatcher: mimic gh subcommands the wrapper calls.
case "${1:-}" in
    issue)
        # gh issue view <N> --repo ... --json body,title
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh to validate widgets.\n\nVerify: scripts/foo.sh exits 0.", "title": "dx: add scripts/foo.sh"}
JSON
            exit 0
        fi
        ;;
    api)
        # gh api /repos/<repo>/commits?path=<file>... --jq .[].commit.message
        # Return one commit headline that names PR 3229.
        echo "Closes the widget loop (#3229)"
        exit 0
        ;;
    pr)
        # gh pr view <N> --repo ... --json number,title,mergedAt,baseRefName,files
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0}], "mergedAt": "2026-04-24T15:39:47Z", "number": 3229, "title": "WIP: ralph output"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "exits 0 on high-confidence shipped match (added overlap)"
else
    fail "exits 0 on high-confidence shipped match (added overlap)" "expected exit 0, got $exit_code; output: $output"
fi

if [[ "$output" == *"shipped:"* && "$output" == *"3229"* ]]; then
    pass "prints 'shipped:' line with PR number on match"
else
    fail "prints 'shipped:' line with PR number on match" "got: $output"
fi

if [[ "$output" == *'"shipped_pr": 3229'* ]]; then
    pass "JSON summary names the shipped PR"
else
    fail "JSON summary names the shipped PR" "got: $output"
fi

if [[ "$output" == *'"added_files"'* && "$output" == *'"scripts/foo.sh"'* ]]; then
    pass "JSON summary includes added_files with scripts/foo.sh"
else
    fail "JSON summary includes added_files with scripts/foo.sh" "got: $output"
fi

# ─── Test 4: PR mentions issue but file overlap is empty → exit 1 ─────────

# Issue body cites scripts/foo.sh; PR touches a totally different file.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh to validate widgets.", "title": "dx: add scripts/foo.sh"}
JSON
            exit 0
        fi
        ;;
    api)
        # No commits touched scripts/foo.sh — return empty.
        exit 0
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "exits 1 when no commits touch the candidate files"
else
    fail "exits 1 when no commits touch the candidate files" "expected exit 1, got $exit_code; output: $output"
fi

if [[ "$output" == *"not-shipped:"* ]]; then
    pass "prints 'not-shipped:' line on miss"
else
    fail "prints 'not-shipped:' line on miss" "got: $output"
fi

# ─── Test 5: 1-file MODIFIED overlap below threshold → exit 1 ─────────────

# Issue body cites scripts/foo.sh; a closed PR exists that *modified* it
# (deletions > 0). Single modified-file overlap is below the high-
# confidence threshold (≥1 added OR ≥2 total).
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need to extend scripts/foo.sh.", "title": "dx: extend scripts/foo.sh"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "refactor scripts/foo.sh (#1234)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/foo.sh", "additions": 5, "deletions": 5}], "mergedAt": "2026-04-01T00:00:00Z", "number": 1234, "title": "refactor scripts/foo.sh"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "exits 1 when the only overlap is a single MODIFIED file"
else
    fail "exits 1 when the only overlap is a single MODIFIED file" "expected exit 1, got $exit_code; output: $output"
fi

# ─── Test 6: 2-file MODIFIED overlap above threshold → exit 0 ─────────────

# Issue body cites two files; a closed PR modified both. ≥2 total overlap
# — even without an `added` overlap — clears the threshold (the second
# branch of the OR).
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "Update scripts/foo.sh and packages/web/bar.ts together.", "title": "dx: update foo and bar"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "refactor foo+bar (#5678)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/foo.sh", "additions": 10, "deletions": 5}, {"path": "packages/web/bar.ts", "additions": 8, "deletions": 3}], "mergedAt": "2026-04-15T00:00:00Z", "number": 5678, "title": "refactor foo+bar"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
    pass "exits 0 when 2 files overlap (modified, threshold cleared via total≥2)"
else
    fail "exits 0 when 2 files overlap" "expected exit 0, got $exit_code; output: $output"
fi

# ─── Test 7: PR not merged (mergedAt null) → exit 1 ───────────────────────

cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh.", "title": "dx: add foo"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "wip (#9999)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0}], "mergedAt": null, "number": 9999, "title": "wip"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
"$WRAPPER" 2831 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "exits 1 when candidate PR was closed without merging (mergedAt null)"
else
    fail "exits 1 when candidate PR was closed without merging" "expected exit 1, got $exit_code"
fi

# ─── Test 8: PR merged onto a feature branch (baseRef != main) → exit 1 ───

cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh.", "title": "dx: add foo"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "feature work (#777)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "feature/wip", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0}], "mergedAt": "2026-04-30T00:00:00Z", "number": 777, "title": "feature work"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
"$WRAPPER" 2831 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "exits 1 when candidate PR merged onto a feature branch"
else
    fail "exits 1 when candidate PR merged onto a feature branch" "expected exit 1, got $exit_code"
fi

# ─── Test 9: issue body has no candidate file paths → exit 1 ──────────────

cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "Pure prose with no file references at all.", "title": "Just thinking out loud"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 ]]; then
    pass "exits 1 when issue body has no candidate file paths"
else
    fail "exits 1 when issue body has no candidate file paths" "expected exit 1, got $exit_code; output: $output"
fi

if [[ "$output" == *"not-shipped:"* && "$output" == *"no candidate file paths"* ]]; then
    pass "prints 'no candidate file paths' miss reason"
else
    fail "prints 'no candidate file paths' miss reason" "got: $output"
fi

# ─── Test 10: gh issue view fails → exit 2 ────────────────────────────────

cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
# Always fail.
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
"$WRAPPER" 2831 > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 when gh issue view fails"
else
    fail "exits 2 when gh issue view fails" "expected exit 2, got $exit_code"
fi

# ─── Test 11: gh CLI not installed → exit 2 ───────────────────────────────

# Remove the mock and restrict PATH so the wrapper cannot find gh.
rm -f "$MOCK_GH"

if command -v gh > /dev/null 2>&1 && PATH="/bin:/usr/bin" command -v gh > /dev/null 2>&1; then
    echo "SKIP: gh is on /bin or /usr/bin; cannot simulate 'gh not installed'"
else
    exit_code=0
    PATH="/bin:/usr/bin" "$WRAPPER" 2831 > /dev/null 2>&1 || exit_code=$?
    if [[ "$exit_code" -eq 2 ]]; then
        pass "exits 2 when gh CLI is not installed"
    else
        fail "exits 2 when gh CLI is not installed" "expected exit 2, got $exit_code"
    fi
fi

# Restore PATH
export PATH="$ORIG_PATH_SAVE"

# ─── Test 12: '#' prefix is stripped from the issue argument ──────────────

# Use the high-confidence-match mock from Test 3 with a leading '#'.
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh.", "title": "dx: add scripts/foo.sh"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "Closes the widget loop (#3229)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0}], "mergedAt": "2026-04-24T15:39:47Z", "number": 3229, "title": "WIP: ralph output"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" "#2831" 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"shipped:"* ]]; then
    pass "strips leading '#' from issue argument"
else
    fail "strips leading '#' from issue argument" "exit=$exit_code output=$output"
fi

# ─── Test 13: multi-`(#N)` headline picks up the squash-merge PR (#4214) ──

# Regression for #4214. Commit headline contains TWO `(#N)` tokens — the
# first is a closed-by issue reference baked into the conventional-
# commits subject, the second is the auto-appended squash-merge PR.
# Pre-fix, bash captured only the first token (`2837`) and the candidate
# was silently skipped because `gh pr view 2837 --json files` errored
# (2837 was an issue, not a PR). Post-fix, BOTH tokens are tried; the
# downstream vetting loop drops the issue (`mergedAt: null`) and keeps
# the PR (`mergedAt: <timestamp>`), so the script returns shipped.
#
# Mock behavior: `gh pr view 2837` returns `mergedAt: null` (simulating
# the issue number being passed to `gh pr view` and returning a null
# mergedAt — which is the same eligibility-fail path the overlap helper
# already takes on closed-without-merge PRs). `gh pr view 3170` returns
# the merged PR with the candidate file in its `files` list. The
# downstream loop must reach 3170; pre-fix it could not.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "Bug in .github/workflows/vercel-deploy-status.yml — squash-merge false-fails.", "title": "fix(ci): vercel-deploy-status false-fails on squash-merge"}
JSON
            exit 0
        fi
        ;;
    api)
        # Single commit headline carrying TWO `(#N)` tokens — the
        # closed-by issue (#2837) and the squash-merge PR (#3170).
        echo "fix(ci): vercel-deploy-status no longer false-fails on squash-merge (#2837) (#3170)"
        exit 0
        ;;
    pr)
        # gh pr view <N> --repo ... — vary the response by PR number.
        if [[ "${2:-}" == "view" ]]; then
            case "${3:-}" in
                2837)
                    # The closed-by ISSUE — `gh pr view <issue-num>`
                    # returns a null mergedAt because issues are not
                    # mergeable. The overlap helper drops it.
                    cat << 'JSON'
{"baseRefName": "main", "files": [], "mergedAt": null, "number": 2837, "title": "[issue not PR]"}
JSON
                    exit 0
                    ;;
                3170)
                    # The actual squash-merge PR — eligible. Mocked
                    # with deletions=0 so the candidate registers as an
                    # `added` overlap and clears the high-confidence
                    # threshold (≥1 added OR ≥2 total) on a single
                    # candidate path.
                    cat << 'JSON'
{"baseRefName": "main", "files": [{"path": ".github/workflows/vercel-deploy-status.yml", "additions": 50, "deletions": 0}], "mergedAt": "2026-04-15T00:00:00Z", "number": 3170, "title": "fix(ci): vercel-deploy-status no longer false-fails on squash-merge"}
JSON
                    exit 0
                    ;;
            esac
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2979 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"shipped:"* && "$output" == *"3170"* ]]; then
    pass "extracts EVERY (#N) token from multi-token headline (regression #4214)"
else
    fail "extracts EVERY (#N) token from multi-token headline (regression #4214)" "exit=$exit_code output=$output"
fi

# ─── Test 14: PR body has `Closes #N` for a DIFFERENT issue → exit 1 ─────

# Regression for #4327. Issue body cites scripts/foo.sh; a closed PR
# touched that file but its body says `Closes #999` — i.e. it shipped a
# DIFFERENT issue's work. The closes-other-issue filter must drop this
# candidate even though file overlap clears the high-confidence
# threshold (1 added overlap). Pre-fix: shipped: PR #1234.
# Post-fix: not-shipped (the only candidate was filtered out).
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh to validate widgets.", "title": "dx: extend scripts/foo.sh"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "Closes the unrelated bug (#1234)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "body": "## Summary\n\nFixed unrelated bug.\n\nCloses #999\n", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0}], "mergedAt": "2026-04-15T00:00:00Z", "number": 1234, "title": "fix: unrelated bug (#999)"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"not-shipped:"* ]]; then
    pass "exits 1 when candidate PR's body Closes a different issue (regression #4327)"
else
    fail "exits 1 when candidate PR's body Closes a different issue (regression #4327)" "exit=$exit_code output=$output"
fi

# ─── Test 15: PR body has `Fixes #N` for a DIFFERENT issue → exit 1 ──────

# Same shape as Test 14 but uses `Fixes` (lowercase, different verb).
# All 9 GitHub closing-keyword verbs (close/closes/closed/fix/fixes/
# fixed/resolve/resolves/resolved) are recognized case-insensitively.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh.", "title": "dx: foo"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "fix unrelated (#5555)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "body": "fixes #888", "files": [{"path": "scripts/foo.sh", "additions": 50, "deletions": 0}], "mergedAt": "2026-04-15T00:00:00Z", "number": 5555, "title": "fix: unrelated"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"not-shipped:"* ]]; then
    pass "exits 1 when candidate PR's body Fixes a different issue (lowercase, regression #4327)"
else
    fail "exits 1 when candidate PR's body Fixes a different issue (lowercase)" "exit=$exit_code output=$output"
fi

# ─── Test 16: PR body has `Closes #N` for the SAME issue → exit 0 ────────

# A PR whose body says `Closes #<this-issue>` is the legitimate happy-
# path PR — it explicitly closes the issue we're checking. Keep it as
# a candidate (exit 0). The duplicate-PR check is the right gate for
# the "same issue, different open PR" case, not this one.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh.", "title": "dx: foo"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "feat: foo (#7777)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "body": "## Summary\n\nAdded foo.\n\nCloses #2831", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0}], "mergedAt": "2026-04-15T00:00:00Z", "number": 7777, "title": "feat: foo"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"shipped:"* && "$output" == *"7777"* ]]; then
    pass "exits 0 when candidate PR's body Closes the SAME issue we're checking (regression #4327)"
else
    fail "exits 0 when candidate PR's body Closes the SAME issue we're checking" "exit=$exit_code output=$output"
fi

# ─── Test 17: canonical placeholder PR (empty body) still ships → exit 0 ─

# The canonical case from #2831 ↔ PR #3229: PR title is `WIP: ralph
# output` (placeholder), body is empty (no `Closes #N` keyword fired
# the auto-close). The closes-other-issue filter must NOT drop this —
# an empty body has no closing-keyword references at all, so the
# filter exits 1 (keep) and the candidate proceeds to file-overlap
# check. This guards against accidentally over-filtering the very
# zombie shape the script was built to catch.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh.", "title": "dx: foo"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "WIP: ralph output (#3229)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "body": "", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0}], "mergedAt": "2026-04-24T00:00:00Z", "number": 3229, "title": "WIP: ralph output"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"shipped:"* && "$output" == *"3229"* ]]; then
    pass "canonical placeholder PR (empty body) still reports shipped (regression #4327, AC2)"
else
    fail "canonical placeholder PR (empty body) still reports shipped (regression #4327, AC2)" "exit=$exit_code output=$output"
fi

# ─── Test 18: PR merged BEFORE the issue was filed → exit 1 (#4353) ──────

# Regression for #4353. Issue body cites scripts/foo.sh; a closed PR
# *added* that file but `mergedAt` is 30 days before the issue's
# `createdAt`. A PR that merged before the issue existed cannot have
# shipped the issue's work — by definition. Pre-fix, file overlap on
# the heavily-edited path was enough to clear the high-confidence
# threshold and emit a `shipped:` line. Post-fix, the date-ordering
# guard skips the candidate and the script reports not-shipped.
#
# Concrete case: issue #4353 (created 2026-05-08) hit a false positive
# against PR #3268 (merged 2026-04-25) on `docs/agent/code-standards.md`
# — flagged because the 1-file added overlap was the only signal the
# script considered.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh to validate widgets.", "title": "dx: add scripts/foo.sh", "createdAt": "2026-05-08T19:41:29Z"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "Closes the widget loop (#3268)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0}], "mergedAt": "2026-04-08T15:39:47Z", "number": 3268, "title": "WIP: ralph output"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4353 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"not-shipped:"* ]]; then
    pass "exits 1 when candidate PR merged before the issue was filed (regression #4353)"
else
    fail "exits 1 when candidate PR merged before the issue was filed (regression #4353)" "exit=$exit_code output=$output"
fi

# ─── Test 19: missing createdAt → guard fails open, fall back to overlap ─

# When the issue JSON lacks `createdAt` (e.g. an older mock or a gh CLI
# version that doesn't expose the field for some reason), the date guard
# must be a no-op so existing behavior is preserved. Same shape as
# Test 18 but with `createdAt` omitted from the issue mock — the
# candidate PR's `mergedAt` is irrelevant to the guard. Expect exit 0
# (shipped) because the file-overlap threshold is cleared.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh.", "title": "dx: add scripts/foo.sh"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "Closes the widget loop (#3268)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0}], "mergedAt": "2026-04-08T15:39:47Z", "number": 3268, "title": "WIP: ralph output"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4353 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"shipped:"* ]]; then
    pass "guard fails open when issue createdAt is absent (regression #4353)"
else
    fail "guard fails open when issue createdAt is absent (regression #4353)" "exit=$exit_code output=$output"
fi

# ─── Test 20: PR merged AFTER the issue was filed → exit 0 (#4353) ───────

# Sanity check — the date guard must NOT block legitimate post-issue
# PRs. Issue created 2026-05-08; candidate PR merged 2026-05-09 (one
# day after). File overlap clears the threshold; the date guard does
# not fire. Expect exit 0 (shipped).
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh.", "title": "dx: add scripts/foo.sh", "createdAt": "2026-05-08T19:41:29Z"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "Add foo (#4400)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0}], "mergedAt": "2026-05-09T00:00:00Z", "number": 4400, "title": "feat: add foo"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4353 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"shipped:"* && "$output" == *"4400"* ]]; then
    pass "PR merged AFTER issue createdAt is still a valid shipped match (#4353 sanity)"
else
    fail "PR merged AFTER issue createdAt is still a valid shipped match (#4353 sanity)" "exit=$exit_code output=$output"
fi

# ─── Test 21: Verify-line-only file overlap → exit 1 (#4340) ─────────────

# Regression for #4340. Issue body cites three files only inside a
# `Verify:` line — they're search arguments to grep, not load-bearing
# targets. A merged PR happens to touch all three for unrelated reasons
# (1 MODIFIED with deletions>0, 1 MODIFIED with deletions==0, 1
# MODIFIED with deletions>0). Pre-fix: 1 added (heuristic-misclassified
# "scripts/rebuild_db.py +120 -0" as added) → exit 0 false-positive
# shipped match. Post-fix: those three are search-context only, the
# only target-context candidate has no overlap, threshold fails → exit 1.
#
# Concrete case: issue #4331 (filed 2026-05-08) hit a false positive
# against PR #3552 (merged 2026-04-27) — the PR body is the LLM-
# enrichment retry plumbing, completely unrelated to the splitter alias
# mechanism. The Verify line in #4331 cites
#   `grep -n "..." packages/A.py packages/B.py scripts/C.py`
# as a re-run convenience; PR #3552 happened to touch the three paths
# for unrelated reasons.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "## Problem\n\nSomething is wrong.\n\n## Acceptance criteria\n\n- [ ] Splitter aliases registered.\n  Verify: `grep -n alias packages/scraper-framework/src/courts/ca/sc_tentatives.py packages/scraper-framework/src/ingestion/worker.py scripts/reingest_from_s3.py`", "title": "feat: register CA splitters under aliases", "createdAt": "2026-05-08T17:28:43Z"}
JSON
            exit 0
        fi
        ;;
    api)
        # Each candidate path returns the same incidental PR.
        echo "fix(ingestion): add retry-on-transient (#3552)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            # PR #3552 — three modified files, all with deletions > 0.
            # changeType: MODIFIED is the authoritative gh-CLI signal —
            # post-fix the deletions==0 fallback no longer mis-classifies
            # these as ADDED.
            cat << 'JSON'
{"baseRefName": "main", "body": "Closes #3549", "files": [{"path": "packages/scraper-framework/src/courts/ca/sc_tentatives.py", "additions": 50, "deletions": 5, "changeType": "MODIFIED"}, {"path": "packages/scraper-framework/src/ingestion/worker.py", "additions": 13, "deletions": 32, "changeType": "MODIFIED"}, {"path": "scripts/reingest_from_s3.py", "additions": 15, "deletions": 19, "changeType": "MODIFIED"}], "mergedAt": "2026-04-27T10:16:05Z", "number": 3552, "title": "fix(ingestion): add retry-on-transient and exit-on-exhaustion for LLM enrichment"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4331 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"not-shipped:"* ]]; then
    pass "exits 1 when overlap is ONLY on Verify-line search-context paths (regression #4340)"
else
    fail "exits 1 when overlap is ONLY on Verify-line search-context paths (regression #4340)" "exit=$exit_code output=$output"
fi

# ─── Test 22: pure-additive MODIFIED diff is not "added" (#4340 changeType) ─

# Pre-#4340, the helper used a deletions==0 && additions>0 heuristic
# to detect added files (since gh CLI didn't surface a status field in
# older docs). That heuristic mis-classifies large *modifications* like
# `scripts/rebuild_db.py +120 -0` as "added" — exactly the false-
# positive pattern that tripped #4340. Post-fix the helper consults
# changeType: "ADDED"/"MODIFIED" (the authoritative gh CLI signal) and
# only falls back to deletions==0 when changeType is absent.
#
# Mock: a single MODIFIED file with `+200 -0` and `changeType: "MODIFIED"`.
# Issue body cites the file in narrative (target-context). Pre-fix:
# heuristic flags it as ADDED → 1 added overlap → shipped (false-
# positive). Post-fix: changeType=MODIFIED authoritative → 0 added,
# 1 total → fails threshold → not-shipped.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "Update scripts/big-mod.sh to add the new feature.", "title": "dx: extend big-mod"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "feat: add helper (#7000)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            # Pure-additive MODIFIED diff. changeType is authoritative
            # — overrides the deletions==0 fallback.
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/big-mod.sh", "additions": 200, "deletions": 0, "changeType": "MODIFIED"}], "mergedAt": "2026-04-15T00:00:00Z", "number": 7000, "title": "feat: add helper"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4400 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"not-shipped:"* ]]; then
    pass "exits 1 when changeType:MODIFIED overrides deletions==0 heuristic (regression #4340)"
else
    fail "exits 1 when changeType:MODIFIED overrides deletions==0 heuristic (regression #4340)" "exit=$exit_code output=$output"
fi

# ─── Test 23: changeType:ADDED is the authoritative ADDED signal (#4340) ──

# Sanity check — the changeType signal must NOT block legitimate adds.
# Issue body cites a target-context file in narrative; PR adds it
# (changeType: "ADDED"). Expect exit 0 (shipped).
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "Add scripts/new-helper.sh to validate widgets.", "title": "dx: add new-helper"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "feat: add helper (#8000)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/new-helper.sh", "additions": 100, "deletions": 0, "changeType": "ADDED"}], "mergedAt": "2026-04-15T00:00:00Z", "number": 8000, "title": "feat: add helper"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4500 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"shipped:"* && "$output" == *"8000"* ]]; then
    pass "exits 0 when changeType:ADDED ratifies legitimate added-file overlap (regression #4340)"
else
    fail "exits 0 when changeType:ADDED ratifies legitimate added-file overlap (regression #4340)" "exit=$exit_code output=$output"
fi

# ─── Test 24: canonical #2831 ↔ PR #3229 still detected (#4340 AC3) ──────

# AC3 of #4340: the fix must NOT regress the original #4204 use case —
# genuine zombie issues (closed PRs that shipped the work without
# `Closes #N`) are still detected. The canonical case from #2831 ↔ PR
# #3229: PR title is `WIP: ralph output` (placeholder), body is empty
# (no `Closes #N` keyword fired the auto-close), the PR added the file
# named in the issue's narrative.
#
# This test mirrors the existing Test 17 (canonical placeholder PR
# empty-body case) but uses the new authoritative changeType:"ADDED"
# signal instead of relying on the deletions==0 heuristic — locks in
# that the changeType fix doesn't regress this AC2 path.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh to validate widgets.", "title": "dx: add scripts/foo.sh"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "WIP: ralph output (#3229)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "body": "", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0, "changeType": "ADDED"}], "mergedAt": "2026-04-24T00:00:00Z", "number": 3229, "title": "WIP: ralph output"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"shipped:"* && "$output" == *"3229"* ]]; then
    pass "AC3: canonical #2831 ↔ PR #3229 still detected (regression #4340)"
else
    fail "AC3: canonical #2831 ↔ PR #3229 still detected (regression #4340)" "exit=$exit_code output=$output"
fi

# ─── Test 25: audit-class issue with single ADDED overlap → exit 1 (#4223) ─

# Regression for #4223. Issue title contains an audit-class keyword
# (`investigate`, `refactor`, `migrate`, `extend`, `tighten`, `harden`,
# `audit`, `additional`); a closed PR ADDED the file the issue cites.
# Pre-#4223: 1 added overlap clears the threshold → false-positive
# `shipped:` match. Post-#4223 with audit-class detection, audit issues
# require ≥2 target-context overlaps AND every overlap is ADDED → drops.
#
# Concrete shape: issue #4208 (investigate / audit / additional in title)
# matched against PR #2811 (which originally CREATED the file the
# investigation is auditing). The date-ordering guard (#4353) covers the
# specific examples in #4223, but the residual class — recent unrelated
# PR ADDED the file an audit issue cites — is what this test locks in.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "Audit Tailwind token pairs for additional one-character footguns.\n\nThe investigation should examine packages/web/tailwind.config.ts.", "title": "investigate: audit Tailwind token pairs for additional one-character footguns", "createdAt": "2026-05-08T00:00:00Z"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "feat(brand): add Tailwind token mapping (#5800)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            # PR ADDED the file the audit cites — but the audit work
            # is legitimately distinct from the file's creation.
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "packages/web/tailwind.config.ts", "additions": 200, "deletions": 0, "changeType": "ADDED"}], "mergedAt": "2026-05-09T00:00:00Z", "number": 5800, "title": "feat(brand): add Tailwind token mapping"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4208 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"not-shipped:"* ]]; then
    pass "audit-class issue with single ADDED overlap drops to not-shipped (regression #4223)"
else
    fail "audit-class issue with single ADDED overlap drops to not-shipped (regression #4223)" "exit=$exit_code output=$output"
fi

# ─── Test 26: audit-class issue with 2 MODIFIED overlaps → exit 1 (#4223) ──

# Companion to Test 25. Audit-class issue cites two files; a closed PR
# MODIFIED both. Pre-#4223: 2 total overlaps clears the threshold →
# false-positive shipped. Post-#4223: audit issues require all-ADDED →
# the modifications drop the match.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "Refactor scripts/dispatcher/foo.py and scripts/dispatcher/bar.py to use the new dispatcher.", "title": "refactor(dispatcher): migrate to fetch_responses", "createdAt": "2026-05-08T00:00:00Z"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "fix(dispatcher): unrelated bug (#6900)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/dispatcher/foo.py", "additions": 5, "deletions": 5, "changeType": "MODIFIED"}, {"path": "scripts/dispatcher/bar.py", "additions": 8, "deletions": 3, "changeType": "MODIFIED"}], "mergedAt": "2026-05-09T00:00:00Z", "number": 6900, "title": "fix(dispatcher): unrelated bug"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4213 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"not-shipped:"* ]]; then
    pass "audit-class issue with 2 MODIFIED overlaps drops to not-shipped (regression #4223)"
else
    fail "audit-class issue with 2 MODIFIED overlaps drops to not-shipped (regression #4223)" "exit=$exit_code output=$output"
fi

# ─── Test 27: audit-class issue with 2 ADDED overlaps → exit 0 (#4223) ─────

# Sanity: audit-class tightening must NOT block legitimate shipped
# matches where a PR genuinely created BOTH files the audit cites. The
# tightened rule is "≥2 target-context AND all-ADDED" — clearing both
# halves emits a shipped match.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "Audit needs scripts/audit-foo.sh and scripts/audit-bar.sh to be created.", "title": "investigate: audit foo and bar", "createdAt": "2026-05-08T00:00:00Z"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "feat: ship audit helpers (#7100)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/audit-foo.sh", "additions": 50, "deletions": 0, "changeType": "ADDED"}, {"path": "scripts/audit-bar.sh", "additions": 60, "deletions": 0, "changeType": "ADDED"}], "mergedAt": "2026-05-09T00:00:00Z", "number": 7100, "title": "feat: ship audit helpers"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4209 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"shipped:"* && "$output" == *"7100"* ]]; then
    pass "audit-class issue with 2 ADDED overlaps still reports shipped (regression #4223 sanity)"
else
    fail "audit-class issue with 2 ADDED overlaps still reports shipped (regression #4223 sanity)" "exit=$exit_code output=$output"
fi

# ─── Test 28: non-audit issue retains pre-#4223 threshold → exit 0 (#4223) ─

# Companion to Test 25. Same single-ADDED-overlap shape, but the issue
# title is `dx: add scripts/foo.sh` (no audit-class verb). The classifier
# does NOT fire → existing threshold (≥1 added) applies → shipped.
# This is the canonical zombie shape (#2831 ↔ #3229) — must NOT regress.
cat > "$MOCK_GH" << 'MOCKGH'
#!/usr/bin/env bash
case "${1:-}" in
    issue)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"body": "We need scripts/foo.sh to validate widgets.", "title": "dx: add scripts/foo.sh", "createdAt": "2026-05-08T00:00:00Z"}
JSON
            exit 0
        fi
        ;;
    api)
        echo "WIP: ralph output (#3229)"
        exit 0
        ;;
    pr)
        if [[ "${2:-}" == "view" ]]; then
            cat << 'JSON'
{"baseRefName": "main", "files": [{"path": "scripts/foo.sh", "additions": 100, "deletions": 0, "changeType": "ADDED"}], "mergedAt": "2026-05-09T00:00:00Z", "number": 3229, "title": "WIP: ralph output"}
JSON
            exit 0
        fi
        ;;
esac
exit 1
MOCKGH
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 2831 2>/dev/null) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"shipped:"* && "$output" == *"3229"* ]]; then
    pass "non-audit issue retains pre-#4223 single-ADDED threshold (regression #4223 sanity)"
else
    fail "non-audit issue retains pre-#4223 single-ADDED threshold (regression #4223 sanity)" "exit=$exit_code output=$output"
fi

# Restore PATH for cleanup
export PATH="$ORIG_PATH_SAVE"

# ─── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
