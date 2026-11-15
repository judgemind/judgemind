#!/usr/bin/env bash
# test_git_env_scrub_helper.sh — Tests for scripts/tests/_git-env-scrub.sh (#5058).
#
# A git hook exports GIT_DIR; a shell test that then runs `git init` /
# `git config` / `git commit` / `git update-ref` in a scratch dir writes to
# THAT repository (#5045). The helper unsets git's repository env vars.
#
# Each case exports GIT_DIR at a sacrificial bare repo, runs a child bash
# that does scratch-repo git writes, and checks the sacrificial repo:
#   1. with the helper sourced: sacrificial repo unchanged, writes land in
#      the scratch repo, global config is the fixture file.
#   2. without the helper (negative control): the sacrificial repo changes,
#      which is what makes case 1 meaningful.
#   3. a git that does not list GIT_DIR: the helper fails loudly with Fix:.
#
# Usage:
#   scripts/tests/test_git_env_scrub_helper.sh

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_git-env-scrub.sh"  # unset inherited GIT_DIR etc. (#5058)
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$SCRIPT_DIR/tests/_git-env-scrub.sh"
. "$SCRIPT_DIR/tests/_test-helpers.sh"
. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST="$(mktemp -d)"
register_temp_dir "$TMPDIR_TEST"

# Everything in a git dir except objects and hooks, as one string.
snapshot() {
    local d="$1"
    (cd "$d" && find . -type f ! -path './objects/*' ! -path './hooks/*' | LC_ALL=C sort | while IFS= read -r f; do
        printf '== %s\n' "$f"
        cat "$f"
    done
    printf '== objects\n'
    find objects -type f | LC_ALL=C sort)
}

make_canary() {
    local name="$1" seed="$TMPDIR_TEST/$1-seed"
    mkdir -p "$seed"
    git -C "$seed" init -q -b main
    echo canary > "$seed/README"
    git -C "$seed" add README
    git -C "$seed" commit -q -m canary
    git clone -q --bare "$seed" "$TMPDIR_TEST/$name.git"
}

# The scratch-repo writes a real test does. $1: "source" or "nosource".
write_child() {
    cat > "$TMPDIR_TEST/child.sh" <<'EOF'
set -euo pipefail
if [ "$1" = "source" ]; then
    . "$2"
fi
scratch="$3"
mkdir -p "$scratch"
cd "$scratch"
git init -q -b main
git config user.name "Scratch Writer"
git config user.email scratch@example.com
echo hi > file.txt
git add file.txt
git commit -q -m scratch
git update-ref refs/heads/scratch-ref HEAD
git config core.bare true
git config --global --get user.name || true
EOF
}
write_child

# ─── Case 1: helper sourced — sacrificial repo untouched ──────────────────
make_canary c1
fixture_before="$(cat "$SCRIPT_DIR/tests/fixtures/git-env-scrub/gitconfig")"
before="$(snapshot "$TMPDIR_TEST/c1.git")"
err1="$TMPDIR_TEST/c1.err"
rc=0
out1="$(GIT_DIR="$TMPDIR_TEST/c1.git" GIT_CONFIG_GLOBAL="$TMPDIR_TEST/outer-gitconfig" \
    bash "$TMPDIR_TEST/child.sh" source "$HELPER" "$TMPDIR_TEST/s1" 2>"$err1")" || rc=$?
assert_eq "case 1: scratch writes succeed with the helper sourced" "$rc" "0" --err-file "$err1"
after="$(snapshot "$TMPDIR_TEST/c1.git")"
if [ "$before" = "$after" ]; then
    pass "case 1: sacrificial repo is byte-for-byte unchanged"
else
    fail "case 1: sacrificial repo is byte-for-byte unchanged" "$(diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") || true)"
fi
assert_eq "case 1: commit landed in the scratch repo" \
    "$(git -C "$TMPDIR_TEST/s1" rev-parse --verify -q refs/heads/scratch-ref >/dev/null && echo yes || echo no)" "yes"
assert_eq "case 1: global config is the fixture file" "$out1" "Judgemind Test"
assert_eq "case 1: fixture gitconfig was not modified" \
    "$(cat "$SCRIPT_DIR/tests/fixtures/git-env-scrub/gitconfig")" "$fixture_before"

# ─── Case 2: negative control — no helper, GIT_DIR wins ───────────────────
make_canary c2
before="$(snapshot "$TMPDIR_TEST/c2.git")"
GIT_DIR="$TMPDIR_TEST/c2.git" GIT_CONFIG_GLOBAL="$TMPDIR_TEST/outer-gitconfig" \
    bash "$TMPDIR_TEST/child.sh" nosource "$HELPER" "$TMPDIR_TEST/s2" >/dev/null 2>&1 || true
after="$(snapshot "$TMPDIR_TEST/c2.git")"
if [ "$before" != "$after" ]; then
    pass "case 2: without the helper the sacrificial repo is modified"
else
    fail "case 2: without the helper the sacrificial repo is modified" "snapshot unchanged: the canary no longer detects the leak"
fi
assert_contains "case 2: the leaked config write hit the sacrificial repo" \
    "$(cat "$TMPDIR_TEST/c2.git/config")" "Scratch Writer"

# ─── Case 3: a git that does not list GIT_DIR fails loudly ────────────────
mkdir -p "$TMPDIR_TEST/fakebin"
cat > "$TMPDIR_TEST/fakebin/git" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$TMPDIR_TEST/fakebin/git"
err3="$TMPDIR_TEST/c3.err"
rc=0
PATH="$TMPDIR_TEST/fakebin:$PATH" bash -c '. "$1"; echo reached' _ "$HELPER" >/dev/null 2>"$err3" || rc=$?
assert_eq "case 3: helper exits 1 when GIT_DIR is not listed" "$rc" "1"
assert_contains "case 3: failure carries a Fix: block" "$(cat "$err3")" "Fix:"
assert_contains "case 3: Fix names the command to check" "$(cat "$err3")" "git rev-parse --local-env-vars"

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
