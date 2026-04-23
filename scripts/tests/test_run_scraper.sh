#!/usr/bin/env bash
# test_run_scraper.sh — Tests for run-scraper.sh
#
# Tests argument parsing, help text, and error handling without calling
# real AWS APIs.  Uses a mock aws CLI for --dry-run tests.
#
# Usage:
#   scripts/tests/test_run_scraper.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_SCRAPER="$SCRIPT_DIR/run-scraper.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

assert_exit_code() {
    local desc="$1"
    local expected="$2"
    local actual="$3"
    TESTS=$((TESTS + 1))
    if [[ "$actual" -eq "$expected" ]]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected exit $expected, got $actual)"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_output_contains() {
    local desc="$1"
    local pattern="$2"
    local output="$3"
    TESTS=$((TESTS + 1))
    if echo "$output" | grep -qF -- "$pattern"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (output does not contain '$pattern')"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_output_not_contains() {
    local desc="$1"
    local pattern="$2"
    local output="$3"
    TESTS=$((TESTS + 1))
    if echo "$output" | grep -qF -- "$pattern"; then
        echo "FAIL: $desc (output unexpectedly contains '$pattern')"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

# Helper: run a command, capture output and exit code separately
run_capturing_exit() {
    local _output _ec
    set +e
    _output=$("$@" 2>&1)
    _ec=$?
    set -e
    RUN_OUTPUT="$_output"
    RUN_EC="$_ec"
}

# ─── Test 1: --help exits 0 and shows usage ─────────────────────────────────

run_capturing_exit "$RUN_SCRAPER" --help
assert_exit_code "--help exits 0" 0 "$RUN_EC"
assert_output_contains "--help shows usage" "Usage:" "$RUN_OUTPUT"
assert_output_contains "--help mentions --detach" "--detach" "$RUN_OUTPUT"
assert_output_contains "--help mentions --env" "--env" "$RUN_OUTPUT"
assert_output_contains "--help mentions --dry-run" "--dry-run" "$RUN_OUTPUT"

# ─── Test 2: -h also works ──────────────────────────────────────────────────

run_capturing_exit "$RUN_SCRAPER" -h
assert_exit_code "-h exits 0" 0 "$RUN_EC"
assert_output_contains "-h shows usage" "Usage:" "$RUN_OUTPUT"

# ─── Test 3: No arguments exits 1 with error message ────────────────────────

run_capturing_exit "$RUN_SCRAPER"
assert_exit_code "No args exits 1" 1 "$RUN_EC"
assert_output_contains "No args shows error" "at least one scraper ID is required" "$RUN_OUTPUT"

# ─── Test 4: Unknown option exits 1 ─────────────────────────────────────────

run_capturing_exit "$RUN_SCRAPER" --bogus
assert_exit_code "Unknown option exits 1" 1 "$RUN_EC"
assert_output_contains "Unknown option shows error" "unknown option" "$RUN_OUTPUT"

# ─── Test 5: --dry-run with mock aws shows planned action ───────────────────
# Create a mock aws CLI that returns realistic JSON responses so the script
# can proceed to the dry-run output.

MOCK_BIN="$TMPDIR_TEST/bin"
mkdir -p "$MOCK_BIN"

# Mock aws CLI script
cat > "$MOCK_BIN/aws" << 'MOCK_EOF'
#!/usr/bin/env bash
# Minimal mock aws CLI for run-scraper.sh dry-run tests.
# Responds to the specific API calls the script makes.

# Find the subcommand (first non-flag arg)
subcmd=""
action=""
for arg in "$@"; do
    case "$arg" in
        --*) continue ;;
        ecs|scheduler|logs|s3) subcmd="$arg"; continue ;;
        *)
            if [[ -n "$subcmd" && -z "$action" ]]; then
                action="$arg"
            fi
            ;;
    esac
done

case "${subcmd}:${action}" in
    ecs:describe-task-definition)
        cat << 'JSON'
{
  "taskDefinition": {
    "taskDefinitionArn": "arn:aws:ecs:us-west-2:155326049300:task-definition/judgemind-scraper-dev:42",
    "family": "judgemind-scraper-dev",
    "containerDefinitions": [{
      "name": "scraper",
      "image": "155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper:abc123"
    }]
  }
}
JSON
        ;;
    scheduler:get-schedule)
        cat << 'JSON'
{
  "Target": {
    "Arn": "arn:aws:ecs:us-west-2:155326049300:cluster/judgemind-dev",
    "EcsParameters": {
      "TaskDefinitionArn": "arn:aws:ecs:us-west-2:155326049300:task-definition/judgemind-scraper-dev:42",
      "NetworkConfiguration": {
        "AwsvpcConfiguration": {
          "Subnets": ["subnet-aaa111", "subnet-bbb222"],
          "SecurityGroups": ["sg-ccc333"],
          "AssignPublicIp": "DISABLED"
        }
      }
    }
  }
}
JSON
        ;;
    *)
        echo "{}" ;;
esac
MOCK_EOF
chmod +x "$MOCK_BIN/aws"

# Run with the mock aws in PATH
run_capturing_exit env PATH="$MOCK_BIN:$PATH" "$RUN_SCRAPER" --dry-run ca-la-tentatives-civil
assert_exit_code "--dry-run exits 0" 0 "$RUN_EC"
assert_output_contains "--dry-run shows DRY RUN banner" "DRY RUN" "$RUN_OUTPUT"
assert_output_contains "--dry-run shows cluster" "judgemind-dev" "$RUN_OUTPUT"
assert_output_contains "--dry-run shows task def" "judgemind-scraper-dev" "$RUN_OUTPUT"
assert_output_contains "--dry-run shows subnets" "subnet-aaa111" "$RUN_OUTPUT"
assert_output_contains "--dry-run shows security group" "sg-ccc333" "$RUN_OUTPUT"

# ─── Test 6: Multiple scraper IDs in --dry-run ──────────────────────────────

run_capturing_exit env PATH="$MOCK_BIN:$PATH" "$RUN_SCRAPER" --dry-run ca-la-tentatives-civil ca-oc-tentatives
assert_exit_code "Multiple IDs --dry-run exits 0" 0 "$RUN_EC"
assert_output_contains "Multiple IDs shows first ID" "ca-la-tentatives-civil" "$RUN_OUTPUT"
assert_output_contains "Multiple IDs shows second ID" "ca-oc-tentatives" "$RUN_OUTPUT"

# ─── Test 7: --env flag changes the environment ─────────────────────────────

run_capturing_exit env PATH="$MOCK_BIN:$PATH" "$RUN_SCRAPER" --env production --dry-run ca-la-tentatives-civil
assert_exit_code "--env production --dry-run exits 0" 0 "$RUN_EC"
assert_output_contains "--env changes task family" "judgemind-scraper-production" "$RUN_OUTPUT"

# ─── Test 8: CMD override format is correct ──────────────────────────────────
# Verify the container override JSON uses the right format

run_capturing_exit env PATH="$MOCK_BIN:$PATH" "$RUN_SCRAPER" --dry-run ca-la-tentatives-civil
assert_output_contains "CMD has framework prefix" '"framework"' "$RUN_OUTPUT"
assert_output_contains "CMD has scraper ID" '"ca-la-tentatives-civil"' "$RUN_OUTPUT"
# Make sure it does NOT contain python -m (that's the ENTRYPOINT's job)
assert_output_not_contains "CMD does not include python" '"python"' "$RUN_OUTPUT"

# ─── Test 9: aws CLI failure is handled ──────────────────────────────────────
# Create a mock aws that always fails

cat > "$MOCK_BIN/aws_fail" << 'FAIL_EOF'
#!/usr/bin/env bash
exit 1
FAIL_EOF
chmod +x "$MOCK_BIN/aws_fail"

# Temporarily rename our aws mock
mv "$MOCK_BIN/aws" "$MOCK_BIN/aws_good"
mv "$MOCK_BIN/aws_fail" "$MOCK_BIN/aws"

run_capturing_exit env PATH="$MOCK_BIN:$PATH" "$RUN_SCRAPER" --dry-run ca-la-tentatives-civil
assert_exit_code "AWS failure exits non-zero" 1 "$RUN_EC"
assert_output_contains "AWS failure shows error" "Error:" "$RUN_OUTPUT"

# Restore the good mock
mv "$MOCK_BIN/aws" "$MOCK_BIN/aws_fail"
mv "$MOCK_BIN/aws_good" "$MOCK_BIN/aws"

# ─── Summary ─────────────────────────────────────────────────────────────────

echo ""
echo "─────────────────────────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
else
    echo "All tests passed."
    exit 0
fi
