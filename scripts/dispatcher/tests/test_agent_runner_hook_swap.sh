#!/usr/bin/env bash
# test_agent_runner_hook_swap.sh — Regression test for issue #3757.
#
# The Fargate `Dockerfile.dispatcher-agent-runner` stages a narrowed
# preflight hook at `/app/fargate-hooks/preflight-bash.sh` and sets
# `DISPATCHER_FARGATE_HOOKS_DIR=/app/fargate-hooks`. The agent-runner
# entrypoint must swap that narrowed hook over the worktree's tracked
# `.claude/hooks/preflight-bash.sh` after cloning the repo, mirroring
# `daemon.py::_install_fargate_preflight_hook` (which performs the same
# swap on the daemon-side worktree path).
#
# Without the swap, ECS-mode ralph workers run with the FULL operator-
# laptop hook ruleset, which blocks the `;`+double-quoted-string Bash
# patterns that the ralph Step 2.5 pre-push command uses. Claude sees
# `permission_denied`, exits cleanly, and never writes the ralph SHIP
# marker → silent ralph_not_ship terminals (root cause of #3757).
#
# Acceptance criterion (#3757): "set AGENT_WORKSPACE to a tmpdir, set
# DISPATCHER_FARGATE_HOOKS_DIR to a fixture dir containing a stub
# preflight-bash.sh with a recognizable marker, run the entrypoint's
# hook-swap function in isolation (sourceable), assert the marker exists
# in $REPO_ROOT/.claude/hooks/preflight-bash.sh. Test must FAIL against
# current code (no swap)."
#
# Usage:
#   scripts/dispatcher/tests/test_agent_runner_hook_swap.sh
#
# Exit codes:
#   0 — all assertions passed.
#   1 — one or more assertions failed (regression).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELPER="$SCRIPT_DIR/dispatcher/agent_runner_install_fargate_hook.sh"

FAILURES=0
TESTS=0

TEMP_DIRS=()
cleanup() {
    set +e
    # ${arr[@]+...} expansion guards against unbound under set -u when
    # cleanup runs before any temp dir was added (early-exit path).
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

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

# ── Precondition: the sourceable helper exists ────────────────────────────

if [[ ! -f "$HELPER" ]]; then
    fail "helper script exists" "expected $HELPER to be present"
    echo
    echo "Tests: $TESTS, Failures: $FAILURES"
    exit 1
fi
pass "helper script $HELPER exists"

# Sourceable helper must define `install_fargate_preflight_hook` without
# side effects when sourced. Source it under a subshell so the test
# script's environment isn't perturbed.
if ! ( set +u; source "$HELPER"; type install_fargate_preflight_hook >/dev/null 2>&1 ); then
    fail "helper defines install_fargate_preflight_hook" \
        "sourcing $HELPER did not define the function"
    echo
    echo "Tests: $TESTS, Failures: $FAILURES"
    exit 1
fi
pass "helper defines install_fargate_preflight_hook function"

# ── Test 1: marker swap (the happy path) ──────────────────────────────────
#
# Build a fake worktree containing a stock operator-laptop hook (with
# `OPERATOR_VARIANT_MARKER`), then build a fixture stage dir containing
# a stub Fargate hook (with `FARGATE_VARIANT_MARKER`) plus a stub
# preflight_cross_worktree.py. Invoke the swap function. Assert the
# Fargate marker now lives in the worktree's hook file and the operator
# marker is gone.

run_swap_marker_test() {
    local tmp
    tmp=$(mktemp -d)
    TEMP_DIRS+=("$tmp")

    local repo="$tmp/repo"
    local hooks_dir="$repo/.claude/hooks"
    local stage="$tmp/fargate-hooks"

    mkdir -p "$hooks_dir"
    mkdir -p "$stage"

    # The pre-existing operator-laptop hook (what the clone left behind).
    cat > "$hooks_dir/preflight-bash.sh" <<'EOF'
#!/usr/bin/env bash
# OPERATOR_VARIANT_MARKER — stand-in for the full operator-laptop ruleset.
exit 0
EOF
    chmod +x "$hooks_dir/preflight-bash.sh"

    cat > "$hooks_dir/preflight_cross_worktree.py" <<'EOF'
# OPERATOR_HELPER_MARKER
EOF

    # The narrowed Fargate hook + helper that should land in the worktree.
    cat > "$stage/preflight-bash.sh" <<'EOF'
#!/usr/bin/env bash
# FARGATE_VARIANT_MARKER — narrowed Fargate hook ruleset.
exit 0
EOF
    chmod +x "$stage/preflight-bash.sh"

    cat > "$stage/preflight_cross_worktree.py" <<'EOF'
# FARGATE_HELPER_MARKER
EOF

    # init a tiny git repo so update-index --skip-worktree has somewhere
    # to land; the helper must tolerate update-index failures, but the
    # happy path should succeed.
    git -C "$repo" init -q --initial-branch main
    git -C "$repo" config user.email "test@example.com"
    git -C "$repo" config user.name "Test"
    git -C "$repo" add .
    git -C "$repo" commit -q -m "init"

    # Source the helper and call the function. The function reads
    # DISPATCHER_FARGATE_HOOKS_DIR + REPO_ROOT (or accepts REPO_ROOT as
    # the first arg) to know which paths to operate on. Use a subshell
    # so the source doesn't leak into the test script's environment.
    (
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export DISPATCHER_FARGATE_HOOKS_DIR="$stage"
        export REPO_ROOT="$repo"
        export AGENT_ID="test-agent"
        install_fargate_preflight_hook >/dev/null 2>&1
    )

    # Assert: the Fargate marker is present in the worktree's hook file.
    if ! grep -q "FARGATE_VARIANT_MARKER" "$hooks_dir/preflight-bash.sh"; then
        fail "swap copies Fargate hook over worktree hook" \
            "FARGATE_VARIANT_MARKER missing from $hooks_dir/preflight-bash.sh"
        return
    fi
    pass "swap copies Fargate hook over worktree hook"

    # Assert: the operator marker is gone (full overwrite, not append).
    if grep -q "OPERATOR_VARIANT_MARKER" "$hooks_dir/preflight-bash.sh"; then
        fail "swap fully overwrites operator hook" \
            "OPERATOR_VARIANT_MARKER still present in $hooks_dir/preflight-bash.sh"
        return
    fi
    pass "swap fully overwrites operator hook"

    # Assert: the helper python file is also swapped.
    if ! grep -q "FARGATE_HELPER_MARKER" "$hooks_dir/preflight_cross_worktree.py"; then
        fail "swap copies Fargate helper over worktree helper" \
            "FARGATE_HELPER_MARKER missing from $hooks_dir/preflight_cross_worktree.py"
        return
    fi
    pass "swap copies Fargate helper over worktree helper"

    # Assert: the worktree hook is executable (chmod preserved).
    if [[ ! -x "$hooks_dir/preflight-bash.sh" ]]; then
        fail "swap preserves executable bit on hook" \
            "$hooks_dir/preflight-bash.sh is not executable after swap"
        return
    fi
    pass "swap preserves executable bit on hook"
}

# ── Test 2: missing-stage skip ────────────────────────────────────────────
#
# When `DISPATCHER_FARGATE_HOOKS_DIR` is unset (operator-laptop fallback),
# the helper must not touch the worktree hook. Mirrors daemon.py's
# `fargate_hook_skip` branch.

run_skip_when_unset_test() {
    local tmp
    tmp=$(mktemp -d)
    TEMP_DIRS+=("$tmp")

    local repo="$tmp/repo"
    local hooks_dir="$repo/.claude/hooks"

    mkdir -p "$hooks_dir"
    cat > "$hooks_dir/preflight-bash.sh" <<'EOF'
#!/usr/bin/env bash
# OPERATOR_LAPTOP_FALLBACK_MARKER
exit 0
EOF
    chmod +x "$hooks_dir/preflight-bash.sh"

    (
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        unset DISPATCHER_FARGATE_HOOKS_DIR
        export REPO_ROOT="$repo"
        export AGENT_ID="test-agent"
        install_fargate_preflight_hook >/dev/null 2>&1
    )

    if ! grep -q "OPERATOR_LAPTOP_FALLBACK_MARKER" "$hooks_dir/preflight-bash.sh"; then
        fail "swap is no-op when DISPATCHER_FARGATE_HOOKS_DIR unset" \
            "operator hook was modified — should have been left untouched"
        return
    fi
    pass "swap is no-op when DISPATCHER_FARGATE_HOOKS_DIR unset"
}

# ── Test 3: missing-stage-file skip ───────────────────────────────────────
#
# When `DISPATCHER_FARGATE_HOOKS_DIR` is set but the source files are
# missing (e.g. a rogue local image), the helper must warn and continue,
# leaving the operator hook in place.

run_skip_when_stage_missing_test() {
    local tmp
    tmp=$(mktemp -d)
    TEMP_DIRS+=("$tmp")

    local repo="$tmp/repo"
    local hooks_dir="$repo/.claude/hooks"
    local stage="$tmp/fargate-hooks"

    mkdir -p "$hooks_dir"
    mkdir -p "$stage"  # exists but empty — no preflight-bash.sh inside

    cat > "$hooks_dir/preflight-bash.sh" <<'EOF'
#!/usr/bin/env bash
# OPERATOR_STAGE_MISSING_MARKER
exit 0
EOF
    chmod +x "$hooks_dir/preflight-bash.sh"

    (
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export DISPATCHER_FARGATE_HOOKS_DIR="$stage"
        export REPO_ROOT="$repo"
        export AGENT_ID="test-agent"
        install_fargate_preflight_hook >/dev/null 2>&1
    )

    if ! grep -q "OPERATOR_STAGE_MISSING_MARKER" "$hooks_dir/preflight-bash.sh"; then
        fail "swap is no-op when stage files missing" \
            "operator hook was modified — should have been left untouched"
        return
    fi
    pass "swap is no-op when stage files missing"
}

# ── Test 4: entrypoint sources the helper ─────────────────────────────────
#
# The agent-runner entrypoint must invoke the swap (directly or via
# `source`) AFTER the `git checkout -B "$BRANCH_NAME" origin/main` line.
# Static lint of the entrypoint text — same shape as
# scripts/dispatcher/tests/test_agent_runner_no_unmerged_files.py.

run_entrypoint_invokes_swap_test() {
    local entrypoint="$SCRIPT_DIR/dispatcher/agent-runner-entrypoint.sh"

    if [[ ! -f "$entrypoint" ]]; then
        fail "agent-runner-entrypoint.sh exists" "expected at $entrypoint"
        return
    fi

    if ! grep -q "install_fargate_preflight_hook" "$entrypoint"; then
        fail "entrypoint invokes install_fargate_preflight_hook" \
            "the entrypoint must call install_fargate_preflight_hook so the swap runs in production"
        return
    fi
    pass "entrypoint invokes install_fargate_preflight_hook"

    # The invocation must come after the branch checkout and before
    # apply_prior_patch (per #3757's fix-shape spec). Use awk to find
    # the line numbers and assert the ordering.
    local checkout_line
    local install_line
    local apply_prior_line
    checkout_line=$(grep -n "^git checkout -B \"\$BRANCH_NAME\" origin/main" "$entrypoint" | head -1 | cut -d: -f1 || true)
    install_line=$(grep -n "install_fargate_preflight_hook" "$entrypoint" | grep -v "^[^:]*:#" | head -1 | cut -d: -f1 || true)
    apply_prior_line=$(grep -n "^apply_prior_patch$" "$entrypoint" | head -1 | cut -d: -f1 || true)

    if [[ -z "$checkout_line" || -z "$install_line" || -z "$apply_prior_line" ]]; then
        fail "entrypoint ordering: all three landmarks present" \
            "checkout=$checkout_line install=$install_line apply_prior=$apply_prior_line"
        return
    fi
    pass "entrypoint ordering: all three landmarks present"

    if (( install_line <= checkout_line )); then
        fail "swap runs after branch checkout" \
            "install_fargate_preflight_hook (line $install_line) must come after git checkout -B (line $checkout_line)"
        return
    fi
    pass "swap runs after branch checkout"

    if (( install_line >= apply_prior_line )); then
        fail "swap runs before apply_prior_patch" \
            "install_fargate_preflight_hook (line $install_line) must come before apply_prior_patch (line $apply_prior_line)"
        return
    fi
    pass "swap runs before apply_prior_patch"
}

# ── Test 5: helper logs fargate_hook_installed event on success ───────────
#
# AC: "Logs emit fargate_hook_installed event on every ECS agent
# startup". The helper must emit a structured log line with that event
# name when the swap runs successfully.

run_helper_logs_installed_event_test() {
    local tmp
    tmp=$(mktemp -d)
    TEMP_DIRS+=("$tmp")

    local repo="$tmp/repo"
    local hooks_dir="$repo/.claude/hooks"
    local stage="$tmp/fargate-hooks"
    local logfile="$tmp/log.out"

    mkdir -p "$hooks_dir"
    mkdir -p "$stage"

    cat > "$hooks_dir/preflight-bash.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "$hooks_dir/preflight-bash.sh"

    cat > "$hooks_dir/preflight_cross_worktree.py" <<'EOF'
# helper
EOF

    cat > "$stage/preflight-bash.sh" <<'EOF'
#!/usr/bin/env bash
# FARGATE_VARIANT_MARKER
exit 0
EOF
    chmod +x "$stage/preflight-bash.sh"

    cat > "$stage/preflight_cross_worktree.py" <<'EOF'
# FARGATE_HELPER_MARKER
EOF

    git -C "$repo" init -q --initial-branch main
    git -C "$repo" config user.email "test@example.com"
    git -C "$repo" config user.name "Test"
    git -C "$repo" add .
    git -C "$repo" commit -q -m "init"

    (
        set +u
        # shellcheck disable=SC1090
        source "$HELPER"
        export DISPATCHER_FARGATE_HOOKS_DIR="$stage"
        export REPO_ROOT="$repo"
        export AGENT_ID="test-agent"
        install_fargate_preflight_hook
    ) > "$logfile" 2>&1 || true

    if ! grep -q "fargate_hook_installed" "$logfile"; then
        fail "helper logs fargate_hook_installed event on success" \
            "expected 'fargate_hook_installed' in helper output. Output was: $(cat "$logfile")"
        return
    fi
    pass "helper logs fargate_hook_installed event on success"
}

# ── Run tests ─────────────────────────────────────────────────────────────

run_swap_marker_test
run_skip_when_unset_test
run_skip_when_stage_missing_test
run_entrypoint_invokes_swap_test
run_helper_logs_installed_event_test

echo
echo "Tests: $TESTS, Failures: $FAILURES"

if (( FAILURES > 0 )); then
    exit 1
fi
exit 0
