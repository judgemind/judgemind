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
#  11. run_check helper surfaces ruff error tail + Full log path (#2973 AC1)
#  12. Migration-only push skips TS lint  no 'checking TypeScript package' (#2877 AC1)
#  13. Real TS change still fires lint    'checking TypeScript package' present (#2877 AC2)
#  18. Non-ASCII tf description rejected  em-dash in description -> hook fails (#3923)
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
# Scenario 11: run_check helper surfaces ruff output tail + Full log path
# ───────────────────────────────────────────────────────────────────────
# Seeds testpkg with a clean src/good.py AND a syntactically-broken
# src/bad.py, stubs a ruff binary in the package .venv so the hook uses
# it, runs the hook, and asserts:
#   - exit != 0
#   - output contains "FAILED: ruff check for testpkg"
#   - output contains "E999" or "SyntaxError" (ruff's syntax-error signal)
#   - output contains "Full log: /tmp/prepush-testpkg-ruff-check.log"
# This directly exercises AC1 (run_check tees combined output to a log and
# prints the last 20 lines + log path on failure) without needing a real
# ruff installation or pytest.
echo "[scenario 11] run_check helper surfaces ruff error tail + Full log path"
init_workspace
git -C "$WORK" checkout --quiet -b feature-syntax-error
seed_testpkg

# Add a syntactically broken Python file that ruff will reject with E999.
cat > "$WORK/packages/testpkg/src/bad.py" <<'PY'
def greet( -> str:
    return "hello"
PY

# Stub a ruff binary in the package .venv so the hook uses it (the hook
# prefers $pkg_dir/.venv/bin/ruff over the global ruff). The stub:
#   - exits 0 for "ruff format --check ..." (format passes)
#   - exits 1 for "ruff check ..." and prints an E999 error
mkdir -p "$WORK/packages/testpkg/.venv/bin"
cat > "$WORK/packages/testpkg/.venv/bin/ruff" <<'RUFF'
#!/usr/bin/env bash
# Stub ruff: fail on "ruff check", pass on "ruff format --check"
if [ "${1:-}" = "format" ]; then
    exit 0
fi
# "ruff check" path — emit a fake E999 syntax error and exit 1
echo "packages/testpkg/src/bad.py:1:16: E999 SyntaxError: invalid syntax"
exit 1
RUFF
chmod +x "$WORK/packages/testpkg/.venv/bin/ruff"

git -C "$WORK" add packages/testpkg
git -C "$WORK" commit --quiet -m "feat: add testpkg with syntax error"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"

run_hook "refs/heads/feature-syntax-error $feat_sha refs/heads/feature-syntax-error $ZERO_SHA"

if [ "$hook_rc" -eq 0 ]; then
    report_fail "expected hook to exit non-zero on ruff syntax error, got 0" "$hook_out"
elif ! echo "$hook_out" | grep -q "FAILED: ruff check for testpkg"; then
    report_fail "expected 'FAILED: ruff check for testpkg' in output (#2973 AC1)" "$hook_out"
elif ! echo "$hook_out" | grep -qE "E999|SyntaxError"; then
    report_fail "expected E999 or SyntaxError in output tail (#2973 AC1)" "$hook_out"
elif ! echo "$hook_out" | grep -q "Full log: /tmp/prepush-testpkg-ruff-check.log"; then
    report_fail "expected 'Full log: /tmp/prepush-testpkg-ruff-check.log' in output (#2973 AC1)" "$hook_out"
else
    report_pass "run_check helper surfaces ruff error tail and Full log path (#2973 AC1)"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 12: Migration-only push does NOT fire TS lint (#2877 AC1)
# ───────────────────────────────────────────────────────────────────────
# A push that touches only packages/api/migrations/*.sql must NOT trigger
# the TypeScript lint pass. Seeding only a package.json + SQL migration
# (no .ts files) and asserting the hook does not emit
# "checking TypeScript package 'api'" verifies the fix.
echo "[scenario 12] migration-only push — TS lint should NOT fire (#2877 AC1)"
init_workspace
git -C "$WORK" checkout --quiet -b feature-migration-only
# Seed package.json in the initial commit so the hook can detect it as a
# TS package, but do NOT include it in the feature diff — only the SQL
# migration is new in this push.
mkdir -p "$WORK/packages/api/migrations"
cat > "$WORK/packages/api/package.json" <<'JSON'
{
  "name": "api",
  "version": "0.0.1",
  "scripts": {}
}
JSON
git -C "$WORK" add packages/api/package.json
git -C "$WORK" commit --quiet -m "chore: seed api package.json"
git -C "$WORK" push --quiet origin feature-migration-only
# Now add only the migration — this is what the hook diff will see.
cat > "$WORK/packages/api/migrations/0001_foo.sql" <<'SQL'
-- Up migration
CREATE TABLE IF NOT EXISTS foo (id SERIAL PRIMARY KEY);

-- Down migration
DROP TABLE IF EXISTS foo;
SQL
git -C "$WORK" add packages/api/migrations/0001_foo.sql
git -C "$WORK" commit --quiet -m "migration: add foo table"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"
prev_sha="$(git -C "$WORK" rev-parse origin/feature-migration-only)"

run_hook "refs/heads/feature-migration-only $feat_sha refs/heads/feature-migration-only $prev_sha"

if [ "$hook_rc" -ne 0 ]; then
    report_fail "migration-only push should exit 0, got rc=$hook_rc (#2877 AC1)" "$hook_out"
elif echo "$hook_out" | grep -q "checking TypeScript package 'api'"; then
    report_fail "migration-only push must NOT fire TS lint (#2877 AC1)" "$hook_out"
else
    report_pass "migration-only push skips TS lint check (#2877 AC1)"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 13: Real TS change still fires lint (#2877 AC2)
# ───────────────────────────────────────────────────────────────────────
# A push that touches a real .ts file under packages/api/src/ must still
# trigger the TS lint pass. We seed a package.json without a lint script
# so the hook takes the "No lint script found" path but still emits the
# "checking TypeScript package 'api'" line, confirming the branch fires.
echo "[scenario 13] real TS change — TS lint should fire (#2877 AC2)"
init_workspace
git -C "$WORK" checkout --quiet -b feature-ts-change
mkdir -p "$WORK/packages/api/src"
cat > "$WORK/packages/api/package.json" <<'JSON'
{
  "name": "api",
  "version": "0.0.1",
  "scripts": {}
}
JSON
cat > "$WORK/packages/api/src/index.ts" <<'TS'
// Minimal TypeScript stub for pre-push hook testing.
export const hello = (): string => "hello";
TS
git -C "$WORK" add packages/api
git -C "$WORK" commit --quiet -m "feat: add api src/index.ts"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"

run_hook "refs/heads/feature-ts-change $feat_sha refs/heads/feature-ts-change $ZERO_SHA"

if echo "$hook_out" | grep -q "checking TypeScript package 'api'"; then
    report_pass "real TS change fires TS lint check (#2877 AC2)"
else
    report_fail "real TS change must fire TS lint check (#2877 AC2)" "$hook_out"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 14: Rebase + force-push ignores rebased-in packages (#3228)
# ───────────────────────────────────────────────────────────────────────
# After `git rebase main`, the feature branch contains commits originally
# authored on main (sibling PR merge). A force-push with-lease passes
# remote_sha=<pre-rebase tip> (the old branch tip, not the zero SHA), so
# the old code diffed against that stale ref and included every rebased-in
# package. With the fix the hook uses merge-base(local, origin/main)
# instead, so only packages touched by this branch's own commits are checked.
echo "[scenario 14] rebase+force-push — rebased-in packages must NOT fire"
init_workspace

# Step 1: create webpkg on a feature branch and push it
git -C "$WORK" checkout --quiet -b feature-rebase-test
mkdir -p "$WORK/packages/webpkg/src"
cat > "$WORK/packages/webpkg/pyproject.toml" <<'PYPROJ'
[project]
name = "webpkg"
version = "0.0.1"
PYPROJ
cat > "$WORK/packages/webpkg/src/foo.py" <<'PY'
"""webpkg module."""


def hello() -> str:
    """Return hello."""
    return "hello"
PY
git -C "$WORK" add packages/webpkg
git -C "$WORK" commit --quiet -m "feat: add webpkg"
original_feature_tip="$(git -C "$WORK" rev-parse HEAD)"
git -C "$WORK" push --quiet origin feature-rebase-test

# Step 2: merge a sibling commit into main that touches apipkg
git -C "$WORK" checkout --quiet main
mkdir -p "$WORK/packages/apipkg/src"
cat > "$WORK/packages/apipkg/pyproject.toml" <<'PYPROJ'
[project]
name = "apipkg"
version = "0.0.1"
PYPROJ
cat > "$WORK/packages/apipkg/src/bar.py" <<'PY'
"""apipkg module."""


def world() -> str:
    """Return world."""
    return "world"
PY
git -C "$WORK" add packages/apipkg
git -C "$WORK" commit --quiet -m "feat: add apipkg (simulates sibling PR merge)"
git -C "$WORK" push --quiet origin main

# Step 3: rebase feature-rebase-test onto updated main
git -C "$WORK" checkout --quiet feature-rebase-test
git -C "$WORK" rebase --quiet main
rebased_tip="$(git -C "$WORK" rev-parse HEAD)"

# Step 4: run hook simulating a force-push-with-lease
# remote_sha = original_feature_tip (the pre-rebase ref, NOT zero SHA)
run_hook "refs/heads/feature-rebase-test $rebased_tip refs/heads/feature-rebase-test $original_feature_tip"

if echo "$hook_out" | grep -q "checking Python package 'apipkg'"; then
    report_fail "rebased-in package 'apipkg' must NOT fire on force-push after rebase (#3228)" "$hook_out"
elif echo "$hook_out" | grep -q "checking Python package 'webpkg'"; then
    report_pass "force-push after rebase: only branch-owned 'webpkg' fires, 'apipkg' ignored (#3228)"
else
    # webpkg not detected either — could be a missing ruff warning (acceptable)
    # Confirm apipkg definitely didn't fire (the primary AC)
    if ! echo "$hook_out" | grep -q "checking Python package 'apipkg'"; then
        report_pass "force-push after rebase: 'apipkg' correctly ignored (#3228 AC1)"
    else
        report_fail "unexpected output for scenario 14" "$hook_out"
    fi
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 15: pytest runs with CWD = package dir; coverage.xml lands
#              in pkg dir, NOT repo root (#2548)
# ───────────────────────────────────────────────────────────────────────
# Seeds testpkg with a stub pytest that writes coverage.xml to its CWD.
# With the old hook (CWD = repo root) the file would appear at $WORK/coverage.xml.
# With the fixed hook (CWD = pkg_dir) it appears at $WORK/packages/testpkg/coverage.xml.
echo "[scenario 15] pytest CWD = package dir; coverage.xml lands in pkg (#2548)"
init_workspace
git -C "$WORK" checkout --quiet -b feature-pytest-cwd

seed_testpkg
mkdir -p "$WORK/packages/testpkg/.venv/bin"

# Stub ruff: always exits 0 so pytest path is reached.
cat > "$WORK/packages/testpkg/.venv/bin/ruff" <<'RUFF'
#!/usr/bin/env bash
exit 0
RUFF
chmod +x "$WORK/packages/testpkg/.venv/bin/ruff"

# Stub pytest: writes coverage.xml containing a sentinel to its CWD and exits 0.
cat > "$WORK/packages/testpkg/.venv/bin/pytest" <<'PYTEST'
#!/usr/bin/env bash
echo "sentinel-coverage-2548" > coverage.xml
exit 0
PYTEST
chmod +x "$WORK/packages/testpkg/.venv/bin/pytest"

git -C "$WORK" add packages/testpkg
git -C "$WORK" commit --quiet -m "feat: add testpkg"
feat_sha="$(git -C "$WORK" rev-parse HEAD)"

run_hook "refs/heads/feature-pytest-cwd $feat_sha refs/heads/feature-pytest-cwd $ZERO_SHA"

if [ "$hook_rc" -ne 0 ]; then
    report_fail "hook should exit 0 with stub pytest that passes (#2548)" "$hook_out"
elif [ ! -f "$WORK/packages/testpkg/coverage.xml" ]; then
    report_fail "packages/testpkg/coverage.xml must exist — pytest must run with CWD = pkg_dir (#2548)" "$hook_out"
elif [ -f "$WORK/coverage.xml" ]; then
    report_fail "coverage.xml must NOT exist in repo root — pytest must not run with CWD = repo root (#2548)" "$hook_out"
else
    report_pass "pytest CWD = package dir: coverage.xml lands in pkg dir, not repo root (#2548)"
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 16: Unannotated dispatcher.agents SQL site is rejected (#3818)
# ───────────────────────────────────────────────────────────────────────
# A daemon.py change that adds an UPDATE/SELECT against dispatcher.agents
# without a nearby execution_mode token, agent_task_arn token, or
# `# exec-mode-agnostic (#NNNN)` annotation must fail the pre-push hook
# locally — mirrors the CI `Verify every dispatcher.agents SQL site is
# execution_mode-aware` step. Without the wiring (#3818), the hook would
# silently pass and the agent would learn about the failure 10-20 minutes
# later when CI runs.
echo "[scenario 16] unannotated dispatcher.agents SQL site rejected (#3818)"
init_workspace

# Copy the real check script into the scratch workspace so the hook can
# run it. The hook gates on `scripts/dispatcher/daemon.py` change
# detection; we seed a daemon.py with a deliberately unannotated SQL site.
CHECK_EXEC_MODE_SH="$REPO_ROOT/scripts/check-dispatcher-execution-mode-aware.sh"
if [ ! -x "$CHECK_EXEC_MODE_SH" ]; then
    report_skip "scripts/check-dispatcher-execution-mode-aware.sh unavailable — cannot exercise"
else
    git -C "$WORK" checkout --quiet -b feature-exec-mode-bad
    mkdir -p "$WORK/scripts/dispatcher" "$WORK/scripts"
    cp "$CHECK_EXEC_MODE_SH" "$WORK/scripts/check-dispatcher-execution-mode-aware.sh"
    chmod +x "$WORK/scripts/check-dispatcher-execution-mode-aware.sh"
    # daemon.py with an UPDATE site lacking execution_mode / agent_task_arn /
    # exec-mode-agnostic annotation within the +/- 60-line window.
    cat > "$WORK/scripts/dispatcher/daemon.py" <<'PY'
"""Daemon stub for testing the pre-push check (#3818)."""


def mark_agent_done(agent_id: str) -> None:
    """Mark an agent done by writing to the agents table.

    No mode awareness here, no task ARN field reference,
    no audit annotation — this is exactly the shape the
    #3818 wiring is supposed to catch locally.
    """
    _execute(
        "UPDATE dispatcher.agents SET status = 'done' WHERE id = %s",
        (agent_id,),
    )


def _execute(sql: str, params: tuple) -> None:
    """Stub query executor."""
    del sql, params
PY
    git -C "$WORK" add scripts/check-dispatcher-execution-mode-aware.sh \
        scripts/dispatcher/daemon.py
    git -C "$WORK" commit --quiet -m "feat: add unannotated daemon.py SQL site"
    feat_sha="$(git -C "$WORK" rev-parse HEAD)"

    run_hook "refs/heads/feature-exec-mode-bad $feat_sha refs/heads/feature-exec-mode-bad $ZERO_SHA"

    if [ "$hook_rc" -eq 0 ]; then
        report_fail "expected hook to reject unannotated daemon.py SQL site (#3818), exit was 0" "$hook_out"
    elif ! echo "$hook_out" | grep -q "scripts/check-dispatcher-execution-mode-aware.sh"; then
        report_fail "expected check-dispatcher-execution-mode-aware.sh to fire on daemon.py change (#3818)" "$hook_out"
    elif ! echo "$hook_out" | grep -qE "exec-mode-agnostic|execution_mode"; then
        report_fail "expected failure message to surface annotation guidance (#3818)" "$hook_out"
    else
        report_pass "hook rejects unannotated dispatcher.agents SQL site in daemon.py (#3818)"
    fi
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 17: Annotated dispatcher.agents SQL site passes (#3818)
# ───────────────────────────────────────────────────────────────────────
# Regression guard: a daemon.py change that DOES carry the
# `# exec-mode-agnostic (#NNNN)` annotation must NOT fail the new check.
# Without this scenario the negative test above could be satisfied by a
# check that fails on every daemon.py change — we need the positive case
# to pin down the actual contract.
echo "[scenario 17] annotated dispatcher.agents SQL site passes (#3818)"
init_workspace

if [ ! -x "$CHECK_EXEC_MODE_SH" ]; then
    report_skip "scripts/check-dispatcher-execution-mode-aware.sh unavailable — cannot exercise"
else
    git -C "$WORK" checkout --quiet -b feature-exec-mode-good
    mkdir -p "$WORK/scripts/dispatcher" "$WORK/scripts"
    cp "$CHECK_EXEC_MODE_SH" "$WORK/scripts/check-dispatcher-execution-mode-aware.sh"
    chmod +x "$WORK/scripts/check-dispatcher-execution-mode-aware.sh"
    cat > "$WORK/scripts/dispatcher/daemon.py" <<'PY'
"""Daemon stub for testing the exec-mode-aware pre-push check (#3818)."""


def mark_agent_done(agent_id: str) -> None:
    """Mark an agent done by writing to dispatcher.agents.

    exec-mode-agnostic (#3158): terminal stamp; teardown is
    handled by the caller per execution_mode in
    _mark_agent_terminal — no mode branch needed here.
    """
    # exec-mode-agnostic (#3158): phase-driven terminal writer.
    _execute(
        "UPDATE dispatcher.agents SET status = 'done' WHERE id = %s",
        (agent_id,),
    )


def _execute(sql: str, params: tuple) -> None:
    """Stub query executor."""
    del sql, params
PY
    git -C "$WORK" add scripts/check-dispatcher-execution-mode-aware.sh \
        scripts/dispatcher/daemon.py
    git -C "$WORK" commit --quiet -m "feat: add annotated daemon.py SQL site"
    feat_sha="$(git -C "$WORK" rev-parse HEAD)"

    run_hook "refs/heads/feature-exec-mode-good $feat_sha refs/heads/feature-exec-mode-good $ZERO_SHA"

    if [ "$hook_rc" -ne 0 ]; then
        # The hook may exit non-zero for unrelated reasons in the
        # scratch workspace (e.g. ruff missing), so we only fail
        # this scenario if the exec-mode check itself flagged.
        if echo "$hook_out" | grep -q "FAILED: scripts/check-dispatcher-execution-mode-aware.sh"; then
            report_fail "annotated daemon.py SQL site must NOT trigger exec-mode check (#3818)" "$hook_out"
        else
            report_pass "annotated daemon.py SQL site does not trigger exec-mode check (#3818); hook rc=$hook_rc came from unrelated branch"
        fi
    else
        report_pass "annotated daemon.py SQL site passes pre-push hook (#3818)"
    fi
fi

# ───────────────────────────────────────────────────────────────────────
# Scenario 18: Non-ASCII tf description rejected by pre-push hook (#3923)
# ───────────────────────────────────────────────────────────────────────
# Seeds infra/terraform/bad.tf with a description containing an em-dash
# (U+2014), copies the real check script into the scratch repo (mirrors the
# markdown-links scenario 5 pattern), and asserts the hook exits non-zero
# with the expected FAILED message.
echo "[scenario 18] non-ASCII tf description (em-dash) — hook rejects push (#3923)"
init_workspace

NONASCII_TF_SH="$REPO_ROOT/scripts/check-no-nonascii-tf-descriptions.sh"
if [ ! -x "$NONASCII_TF_SH" ]; then
    report_skip "scripts/check-no-nonascii-tf-descriptions.sh unavailable — cannot exercise"
else
    git -C "$WORK" checkout --quiet -b feature-nonascii-tf
    mkdir -p "$WORK/infra/terraform" "$WORK/scripts"
    cp "$NONASCII_TF_SH" "$WORK/scripts/check-no-nonascii-tf-descriptions.sh"
    chmod +x "$WORK/scripts/check-no-nonascii-tf-descriptions.sh"
    # Write a .tf file with an em-dash (U+2014) in the description field.
    # Use printf to embed the UTF-8 encoding of U+2014 (0xE2 0x80 0x94)
    # without relying on the editor or locale interpretation.
    printf 'resource "aws_security_group" "bad" {\n  description = "Allows inbound traffic \xe2\x80\x94 see docs"\n}\n' \
        > "$WORK/infra/terraform/bad.tf"
    git -C "$WORK" add infra/terraform/bad.tf scripts/check-no-nonascii-tf-descriptions.sh
    git -C "$WORK" commit --quiet -m "infra: add tf with em-dash description"
    feat_sha="$(git -C "$WORK" rev-parse HEAD)"

    run_hook "refs/heads/feature-nonascii-tf $feat_sha refs/heads/feature-nonascii-tf $ZERO_SHA"

    if [ "$hook_rc" -eq 0 ]; then
        report_fail "expected hook to reject em-dash tf description (#3923), exit was 0" "$hook_out"
    elif ! echo "$hook_out" | grep -q "FAILED: check-no-nonascii-tf-descriptions for terraform"; then
        report_fail "expected 'FAILED: check-no-nonascii-tf-descriptions for terraform' in output (#3923)" "$hook_out"
    else
        report_pass "hook rejects push with non-ASCII Terraform description field (#3923)"
    fi
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
