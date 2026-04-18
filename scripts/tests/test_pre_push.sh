#!/usr/bin/env bash
# Integration test suite for .githooks/pre-push.
#
# Exercises the hook in a scratch git repo, driving it through a range of
# push scenarios and asserting on exit code + expected log output.
#
# Why this exists (#2488):
#
#   .githooks/pre-push is the single most load-bearing piece of agent
#   pre-PR enforcement — it runs ruff, pytest, actionlint, markdown-links,
#   coverage, diff coverage, and terraform fmt. A silent breakage in any
#   of those branches disables CI-safety locally and has in the past
#   hidden latent bugs for arbitrary time (see #2487 for the stdin
#   field-order regression, uncovered only when #2464 added a markdown
#   check that didn't fire on new-branch pushes).
#
#   This test suite runs each scenario against the hook in a freshly
#   initialised temp repo so we can assert on behaviour without pushing
#   anything.
#
# Scenarios covered:
#
#   1. Clean push (no-op change)          exits 0, no FAIL messages
#   2. New-branch push detects changes    ruff check fires on testpkg
#   3. Existing-branch push detects too   ruff check fires on testpkg
#   4. Delete push is allowed             exit 0, no per-package checks
#   5. New-branch markdown scan fires     broken `.md` backtick ref rejected
#   6. Missing ruff -> WARNING only       no failure, WARNING in output
#   7. Missing actionlint on workflow     WARNING only, exit 0
#   8. Missing terraform on infra change  WARNING only, exit 0
#
# Run:
#   scripts/tests/test_pre_push.sh
#
# Exits non-zero if any scenario fails.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO_ROOT/.githooks/pre-push"
MARKDOWN_SH="$REPO_ROOT/scripts/check-markdown-links.sh"
MARKDOWN_PY="$REPO_ROOT/scripts/check-markdown-links.py"

if [ ! -x "$HOOK" ]; then
    echo "FAIL: hook not found or not executable at $HOOK" >&2
    exit 1
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

REMOTE="$TMPDIR/remote.git"
WORK="$TMPDIR/work"

# ───────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────

pass=0
fail=0
skip=0

report_pass() {
    pass=$((pass + 1))
    echo "  PASS: $1"
}

report_fail() {
    fail=$((fail + 1))
    echo "  FAIL: $1" >&2
    if [ -n "${2-}" ]; then
        echo "--- hook output ---" >&2
        echo "$2" >&2
        echo "--- end ---" >&2
    fi
}

report_skip() {
    skip=$((skip + 1))
    echo "  SKIP: $1"
}

ZERO_SHA="0000000000000000000000000000000000000000"

# Initialise a fresh remote + working copy. Each scenario resets to this
# state so they are independent.
init_workspace() {
    rm -rf "$REMOTE" "$WORK"
    git init --quiet --bare "$REMOTE"
    git init --quiet "$WORK"
    git -C "$WORK" config user.email "test@example.com"
    git -C "$WORK" config user.name "Test"
    git -C "$WORK" config commit.gpgsign false
    git -C "$WORK" remote add origin "$REMOTE"
    echo "# test" > "$WORK/README.md"
    git -C "$WORK" add README.md
    git -C "$WORK" commit --quiet -m "initial"
    git -C "$WORK" branch -M main
    git -C "$WORK" push --quiet origin main
}

# Write a ruff-clean testpkg. Callers add further files as needed.
seed_testpkg() {
    mkdir -p "$WORK/packages/testpkg/src" "$WORK/packages/testpkg/tests"
    cat > "$WORK/packages/testpkg/pyproject.toml" <<'PYPROJ'
[project]
name = "testpkg"
version = "0.0.1"
PYPROJ
    cat > "$WORK/packages/testpkg/src/good.py" <<'PY'
"""A minimal, ruff-clean module for pre-push hook testing."""


def greet() -> str:
    """Return a greeting."""
    return "hello"
PY
}

# Feed stdin to the hook from inside $WORK. Captures stdout+stderr.
# Usage: run_hook "<stdin line>" -> sets $hook_out and $hook_rc
run_hook() {
    local stdin_line="$1"
    hook_out="$(cd "$WORK" && echo "$stdin_line" | "$HOOK" origin "$REMOTE" 2>&1)" \
        && hook_rc=0 || hook_rc=$?
}

# Run the hook with a PATH that shadows a named binary, so `command -v`
# returns false inside the hook. Used to test the "missing tool" branches.
# Usage: run_hook_without <tool1> [tool2 ...] "<stdin line>"
run_hook_without() {
    local tools=()
    while [ $# -gt 1 ]; do
        tools+=("$1")
        shift
    done
    local stdin_line="$1"

    local sanitized_bin="$TMPDIR/sanitized-bin"
    rm -rf "$sanitized_bin"
    mkdir -p "$sanitized_bin"

    # Symlink everything on the current PATH except the named tools.
    local IFS=:
    for dir in $PATH; do
        [ -d "$dir" ] || continue
        for src in "$dir"/*; do
            [ -e "$src" ] || continue
            local name
            name="$(basename "$src")"
            local skip_this=false
            for t in "${tools[@]}"; do
                if [ "$name" = "$t" ]; then
                    skip_this=true
                    break
                fi
            done
            [ "$skip_this" = true ] && continue
            [ -e "$sanitized_bin/$name" ] && continue
            ln -s "$src" "$sanitized_bin/$name" 2>/dev/null || true
        done
    done

    hook_out="$(cd "$WORK" && PATH="$sanitized_bin" echo "$stdin_line" \
        | PATH="$sanitized_bin" "$HOOK" origin "$REMOTE" 2>&1)" \
        && hook_rc=0 || hook_rc=$?
}

# ───────────────────────────────────────────────────────────────────────
# Scenario 1: Clean no-op push succeeds
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 1] clean push — hook should exit 0 with no FAIL"
init_workspace

# Add a trivial, non-package-triggering change: a top-level README tweak.
echo "updated" >> "$WORK/README.md"
git -C "$WORK" add README.md
git -C "$WORK" commit --quiet -m "chore: update readme"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"
prev_sha="$(git -C "$WORK" rev-parse origin/main)"

run_hook "refs/heads/main $feat_sha refs/heads/main $prev_sha"

if [ "$hook_rc" -ne 0 ]; then
    report_fail "clean push should exit 0, got $hook_rc" "$hook_out"
elif echo "$hook_out" | grep -qE "FAILED|Push aborted"; then
    report_fail "clean push produced FAIL/abort output" "$hook_out"
else
    report_pass "clean push exits 0 with no failures"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 2: New-branch push detects changed files
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 2] new-branch push — hook detects testpkg"
init_workspace

git -C "$WORK" checkout --quiet -b feature-new-branch
seed_testpkg
git -C "$WORK" add packages/testpkg
git -C "$WORK" commit --quiet -m "feat: add testpkg"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"

run_hook "refs/heads/feature-new-branch $feat_sha refs/heads/feature-new-branch $ZERO_SHA"

if echo "$hook_out" | grep -q "checking Python package 'testpkg'"; then
    report_pass "hook detected testpkg on new-branch push (#2487 guard)"
else
    report_fail "hook did NOT detect testpkg on new-branch push" "$hook_out"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 3: Existing-branch push detects changed files
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 3] existing-branch push — hook still detects testpkg"
# Reuse workspace from scenario 2.
prev_sha="$(git -C "$WORK" rev-parse main)"

run_hook "refs/heads/feature-new-branch $feat_sha refs/heads/feature-new-branch $prev_sha"

if echo "$hook_out" | grep -q "checking Python package 'testpkg'"; then
    report_pass "hook detected testpkg on existing-branch push"
else
    report_fail "hook did NOT detect testpkg on existing-branch push" "$hook_out"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 4: Delete push is allowed (no per-package checks)
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 4] delete push — hook should skip without error"
prev_sha="$(git -C "$WORK" rev-parse main)"

run_hook "refs/heads/feature-new-branch $ZERO_SHA refs/heads/feature-new-branch $prev_sha"

if [ "$hook_rc" -ne 0 ]; then
    report_fail "delete push should exit 0, got $hook_rc" "$hook_out"
elif echo "$hook_out" | grep -q "checking Python package"; then
    report_fail "delete push should not run per-package checks" "$hook_out"
else
    report_pass "delete push skipped cleanly"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 5: New-branch markdown scan rejects broken pointer
# ───────────────────────────────────────────────────────────────────────
# Depends on scripts/check-markdown-links.{sh,py} existing in the scratch
# repo — otherwise the hook prints a WARNING and skips. We copy both from
# the real repo so the check actually runs.
echo "[scenario 5] new-branch push with broken .md pointer — rejected"
init_workspace

if [ ! -x "$MARKDOWN_SH" ] || [ ! -f "$MARKDOWN_PY" ]; then
    report_skip "scripts/check-markdown-links.{sh,py} unavailable — cannot exercise"
else
    git -C "$WORK" checkout --quiet -b feature-md
    mkdir -p "$WORK/docs" "$WORK/scripts"
    cp "$MARKDOWN_SH" "$WORK/scripts/check-markdown-links.sh"
    cp "$MARKDOWN_PY" "$WORK/scripts/check-markdown-links.py"
    chmod +x "$WORK/scripts/check-markdown-links.sh"
    # docs/**/*.md is in DEFAULT_SCAN_GLOBS — this file will be scanned.
    cat > "$WORK/docs/broken.md" <<'MD'
# Broken link doc

See `docs/this-file-does-not-exist.md` for details.
MD
    git -C "$WORK" add scripts docs
    git -C "$WORK" commit --quiet -m "docs: add broken.md"
    feat_sha="$(git -C "$WORK" rev-parse HEAD)"

    run_hook "refs/heads/feature-md $feat_sha refs/heads/feature-md $ZERO_SHA"

    if [ "$hook_rc" -eq 0 ]; then
        report_fail "expected hook to reject broken markdown link, exit was 0" "$hook_out"
    elif echo "$hook_out" | grep -q "scripts/check-markdown-links.sh"; then
        report_pass "hook rejected push with broken markdown link (#2464 guard)"
    else
        report_fail "hook exited non-zero but without markdown-check failure text" "$hook_out"
    fi
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 6: Missing ruff emits WARNING but does not fail the push
# ───────────────────────────────────────────────────────────────────────
# Seed testpkg and run the hook with a PATH that hides any ruff on the
# system. The package has no .venv/bin/ruff of its own, so the hook falls
# through to the WARNING branch.
echo "[scenario 6] missing ruff — WARNING only, no failure"
init_workspace
git -C "$WORK" checkout --quiet -b feature-noruff
seed_testpkg
git -C "$WORK" add packages/testpkg
git -C "$WORK" commit --quiet -m "feat: add testpkg"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"

run_hook_without ruff \
    "refs/heads/feature-noruff $feat_sha refs/heads/feature-noruff $ZERO_SHA"

if echo "$hook_out" | grep -q "WARNING: ruff not found"; then
    if [ "$hook_rc" -eq 0 ]; then
        report_pass "missing ruff prints WARNING and exits 0"
    else
        report_fail "missing ruff should not fail the push, got rc=$hook_rc" "$hook_out"
    fi
else
    report_fail "expected 'WARNING: ruff not found' in output" "$hook_out"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 7: Workflow change + missing actionlint emits WARNING only
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 7] workflow change + missing actionlint — WARNING only"
init_workspace
git -C "$WORK" checkout --quiet -b feature-workflow
mkdir -p "$WORK/.github/workflows"
cat > "$WORK/.github/workflows/toy.yml" <<'YML'
name: toy
on: [push]
jobs:
  hello:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
YML
git -C "$WORK" add .github/workflows/toy.yml
git -C "$WORK" commit --quiet -m "ci: add toy workflow"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"

run_hook_without actionlint \
    "refs/heads/feature-workflow $feat_sha refs/heads/feature-workflow $ZERO_SHA"

if echo "$hook_out" | grep -q "WARNING: actionlint not found"; then
    if [ "$hook_rc" -eq 0 ]; then
        report_pass "missing actionlint prints WARNING and exits 0"
    else
        report_fail "missing actionlint should not fail push, got rc=$hook_rc" "$hook_out"
    fi
else
    report_fail "expected 'WARNING: actionlint not found' in output" "$hook_out"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 8: Terraform change + missing terraform emits WARNING only
# ───────────────────────────────────────────────────────────────────────
echo "[scenario 8] terraform change + missing terraform — WARNING only"
init_workspace
git -C "$WORK" checkout --quiet -b feature-tf
mkdir -p "$WORK/infra/terraform"
cat > "$WORK/infra/terraform/main.tf" <<'TF'
# placeholder
TF
git -C "$WORK" add infra/terraform/main.tf
git -C "$WORK" commit --quiet -m "infra: add placeholder tf"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"

run_hook_without terraform \
    "refs/heads/feature-tf $feat_sha refs/heads/feature-tf $ZERO_SHA"

if echo "$hook_out" | grep -q "WARNING: terraform not found"; then
    if [ "$hook_rc" -eq 0 ]; then
        report_pass "missing terraform prints WARNING and exits 0"
    else
        report_fail "missing terraform should not fail push, got rc=$hook_rc" "$hook_out"
    fi
else
    report_fail "expected 'WARNING: terraform not found' in output" "$hook_out"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 9: Migration change fires schema drift check
# ───────────────────────────────────────────────────────────────────────
# A push that touches packages/api/migrations/*.sql must run the
# schema-drift check. Without Docker we should see the WARNING branch
# (exit 0); with Docker unavailable in this scratch repo we exercise
# the "docker not found" path, matching the pattern used for ruff /
# actionlint / terraform missing-tool tests. See #2702.
echo "[scenario 9] migration change + missing docker — WARNING only, hook fires"
init_workspace
git -C "$WORK" checkout --quiet -b feature-migration
mkdir -p "$WORK/packages/api/migrations"
cat > "$WORK/packages/api/migrations/0001_test.sql" <<'SQL'
-- Up migration
CREATE TABLE IF NOT EXISTS test_table (id SERIAL PRIMARY KEY);

-- Down migration
DROP TABLE IF EXISTS test_table;
SQL
git -C "$WORK" add packages/api/migrations/0001_test.sql
git -C "$WORK" commit --quiet -m "migration: add test table"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"

run_hook_without docker \
    "refs/heads/feature-migration $feat_sha refs/heads/feature-migration $ZERO_SHA"

if ! echo "$hook_out" | grep -q "checking schema drift"; then
    report_fail "hook did NOT run schema drift check on migration push (#2702)" "$hook_out"
elif ! echo "$hook_out" | grep -q "WARNING: docker not found"; then
    report_fail "expected 'WARNING: docker not found' when docker is missing" "$hook_out"
elif [ "$hook_rc" -ne 0 ]; then
    report_fail "missing docker should not fail push, got rc=$hook_rc" "$hook_out"
else
    report_pass "migration push fires schema drift check; missing docker -> WARNING only"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 10: Schema drift failure names regenerate_schema.sh verbatim
# ───────────────────────────────────────────────────────────────────────
# Acceptance criterion from #2702: "Failure message names the exact fix
# (scripts/regenerate_schema.sh) verbatim." We stub check_schema_drift.sh
# to fail and assert the hook's failure message surfaces the fix string.
echo "[scenario 10] schema drift failure surfaces 'scripts/regenerate_schema.sh' fix text"
init_workspace
git -C "$WORK" checkout --quiet -b feature-drift
mkdir -p "$WORK/packages/api/migrations" "$WORK/scripts"
cat > "$WORK/packages/api/migrations/0001_drift.sql" <<'SQL'
-- Up migration
CREATE TABLE IF NOT EXISTS drift_table (id SERIAL PRIMARY KEY);

-- Down migration
DROP TABLE IF EXISTS drift_table;
SQL
# Stub check_schema_drift.sh: pretend docker is present and fail immediately.
cat > "$WORK/scripts/check_schema_drift.sh" <<'STUB'
#!/usr/bin/env bash
echo "=== FAIL: schema.sql has drifted from migrations ==="
exit 1
STUB
chmod +x "$WORK/scripts/check_schema_drift.sh"
# Also stub a `docker` binary on PATH that reports `docker info` success,
# so the hook takes the "run check" branch rather than the WARNING branch.
STUB_BIN="$TMPDIR/stub-bin"
rm -rf "$STUB_BIN"
mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/docker" <<'DOCKER'
#!/usr/bin/env bash
# Minimal docker stub — `docker info` succeeds, anything else succeeds too.
exit 0
DOCKER
chmod +x "$STUB_BIN/docker"
# Symlink the rest of PATH so git/grep/etc still work.
IFS=:
for dir in $PATH; do
    [ -d "$dir" ] || continue
    for src in "$dir"/*; do
        [ -e "$src" ] || continue
        name="$(basename "$src")"
        [ "$name" = "docker" ] && continue
        [ -e "$STUB_BIN/$name" ] && continue
        ln -s "$src" "$STUB_BIN/$name" 2>/dev/null || true
    done
done
unset IFS
git -C "$WORK" add packages/api/migrations/0001_drift.sql scripts/check_schema_drift.sh
git -C "$WORK" commit --quiet -m "migration: add drift table"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"

hook_out="$(cd "$WORK" && echo "refs/heads/feature-drift $feat_sha refs/heads/feature-drift $ZERO_SHA" \
    | PATH="$STUB_BIN" "$HOOK" origin "$REMOTE" 2>&1)" \
    && hook_rc=0 || hook_rc=$?

if [ "$hook_rc" -eq 0 ]; then
    report_fail "expected hook to reject drift, exit was 0" "$hook_out"
elif ! echo "$hook_out" | grep -q "scripts/regenerate_schema.sh"; then
    report_fail "expected failure message to name 'scripts/regenerate_schema.sh' verbatim (#2702 AC1)" "$hook_out"
elif ! echo "$hook_out" | grep -q "FAILED: scripts/check_schema_drift.sh"; then
    report_fail "expected 'FAILED: scripts/check_schema_drift.sh' in output" "$hook_out"
else
    report_pass "hook rejects drift and names regenerate_schema.sh in the fix message"
fi

# ───────────────────────────────────────────────────────────────────────
# Summary
# ───────────────────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "pre-push hook test suite: $pass passed, $fail failed, $skip skipped"
echo "────────────────────────────────────────────────────────────────"

if [ "$fail" -gt 0 ]; then
    exit 1
fi
exit 0
