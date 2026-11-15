#!/usr/bin/env bash
# test_gh_isolate_helper.sh — Tests for scripts/tests/_gh-isolate.sh (#5071).
#
# With a fake GitHub token in the environment, a shell test that sources the
# helper and runs `gh issue close` must hit the shim (exit 4, "real gh called
# from a test", call logged) and never run a real gh binary. A fake gh the
# test prepends to PATH afterwards must still win.
#
# Usage:
#   scripts/tests/test_gh_isolate_helper.sh

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$SCRIPT_DIR/tests/_gh-isolate.sh"
SHIM_DIR="$SCRIPT_DIR/tests/fixtures/gh-isolate/bin"
. "$SCRIPT_DIR/tests/_test-helpers.sh"
. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST="$(mktemp -d)"
register_temp_dir "$TMPDIR_TEST"
LOG="$TMPDIR_TEST/gh-calls.log"

# Child script: fake tokens set BEFORE sourcing, as on a laptop with gh auth.
cat > "$TMPDIR_TEST/child.sh" <<EOF
#!/usr/bin/env bash
set -uo pipefail
export GH_TOKEN=ghp_fake_token_5071 GITHUB_TOKEN=ghp_fake_token_5071
export GH_ENTERPRISE_TOKEN=fake GITHUB_ENTERPRISE_TOKEN=fake
. "$HELPER"
echo "gh_path=\$(command -v gh)"
echo "gh_token=\${GH_TOKEN:-unset} github_token=\${GITHUB_TOKEN:-unset}"
echo "enterprise=\${GH_ENTERPRISE_TOKEN:-unset}/\${GITHUB_ENTERPRISE_TOKEN:-unset}"
echo "config=\$GH_CONFIG_DIR"
gh issue close 4673 --repo judgemind/judgemind --comment "should never post"
echo "close_rc=\$?"
gh auth status
echo "auth_rc=\$?"
EOF
chmod +x "$TMPDIR_TEST/child.sh"

out="$(GH_ISOLATE_LOG="$LOG" "$TMPDIR_TEST/child.sh" 2>"$TMPDIR_TEST/err")"
err="$(cat "$TMPDIR_TEST/err")"

assert_contains "gh resolves to the shim" "$out" "gh_path=$SHIM_DIR/gh"
assert_contains "GH_TOKEN and GITHUB_TOKEN are unset" "$out" "gh_token=unset github_token=unset"
assert_contains "enterprise tokens are unset" "$out" "enterprise=unset/unset"
assert_contains "GH_CONFIG_DIR is the empty fixture dir" "$out" "config=$SCRIPT_DIR/tests/fixtures/gh-isolate/config"
assert_contains "gh issue close exits 4" "$out" "close_rc=4"
assert_contains "gh auth status exits 4" "$out" "auth_rc=4"
assert_contains "shim says real gh was called" "$err" "FAIL: real gh called from a test (#5071): gh issue close 4673"
assert_contains "shim prints a Fix line" "$err" "Fix: stub gh in this test"
logged="$(cat "$LOG")"
assert_contains "shim logs the close call with the test name" "$logged" "child.sh	gh issue close 4673 --repo judgemind/judgemind"
assert_eq "shim logs exactly the two calls" "$(wc -l < "$LOG" | tr -d ' ')" "2"

# The fixture config dir holds no gh login.
if [ -e "$SCRIPT_DIR/tests/fixtures/gh-isolate/config/hosts.yml" ]; then
    fail "fixture config dir has no hosts.yml" "hosts.yml present"
else
    pass "fixture config dir has no hosts.yml"
fi

# A test's own fake gh, prepended after sourcing, wins over the shim.
mkdir -p "$TMPDIR_TEST/bin"
printf '#!/bin/sh\necho "fake gh: $*"\n' > "$TMPDIR_TEST/bin/gh"
chmod +x "$TMPDIR_TEST/bin/gh"
stub_out="$(PATH="$TMPDIR_TEST/bin:$PATH" gh issue view 1)"
assert_eq "a test's own gh stub wins" "$stub_out" "fake gh: issue view 1"

# The helper refuses to run when the fixture config dir holds a login.
mkdir -p "$TMPDIR_TEST/copy-tests/fixtures"
cp "$HELPER" "$TMPDIR_TEST/copy-tests/_gh-isolate.sh"
cp -R "$SCRIPT_DIR/tests/fixtures/gh-isolate" "$TMPDIR_TEST/copy-tests/fixtures/gh-isolate"
: > "$TMPDIR_TEST/copy-tests/fixtures/gh-isolate/config/hosts.yml"
rc=0
bash -c ". \"$TMPDIR_TEST/copy-tests/_gh-isolate.sh\"" 2>"$TMPDIR_TEST/err2" || rc=$?
assert_eq "hosts.yml in the fixture dir: exit 1" "$rc" "1"
assert_contains "hosts.yml in the fixture dir: Fix block" "$(cat "$TMPDIR_TEST/err2")" "Fix:"

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
