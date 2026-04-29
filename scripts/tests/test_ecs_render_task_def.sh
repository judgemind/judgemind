#!/usr/bin/env bash
# test_ecs_render_task_def.sh — Unit tests for scripts/ecs-render-task-def.sh
#
# Exercises the register-task-definition pre-flight against a mock aws CLI.
# Verifies:
#
#   1. SSM-source path: when a desired-container-definitions SSM parameter name
#      is provided, the script fetches container_definitions from SSM (terraform-
#      managed source of truth) instead of from describe-task-definition. This
#      is the regression test for #3765 — the deploy-api preserve-secrets bug
#      class where the running task-def has stale fields that terraform has
#      since removed.
#
#   2. Legacy path: when no SSM parameter is provided, the script falls back to
#      describe-task-definition (preserves existing deploy-scraper /
#      deploy-production behavior).
#
#   3. Image swap: in both modes, the named container's image field is replaced
#      with the input image URI.
#
#   4. Conflict scenario (the #3765 regression): SSM-source mode with a
#      describe-task-definition response that still contains a removed secret —
#      the rendered output must NOT contain that secret (terraform's removal
#      wins).
#
# Usage:
#   scripts/tests/test_ecs_render_task_def.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_UNDER_TEST="$REPO_ROOT/scripts/ecs-render-task-def.sh"

if [[ ! -x "$SCRIPT_UNDER_TEST" ]]; then
    echo "FATAL: $SCRIPT_UNDER_TEST is not executable." >&2
    exit 1
fi

FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

TEMP_DIRS=()
# shellcheck disable=SC2329
cleanup() {
    set +e
    for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
}
trap cleanup EXIT

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    TEMP_DIRS+=("$dir")
    echo "$dir"
}

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

# Set up a mock `aws` CLI that emits canned responses for describe-task-
# definition, ssm get-parameter, and register-task-definition. The mock writes
# every invocation's args to $CALL_LOG so tests can assert which path was
# taken.
#
# Files in $tmpdir/:
#   describe-task-def.json   — emitted on `aws ecs describe-task-definition`
#   ssm-param.json           — emitted on `aws ssm get-parameter`
#   register-args.log        — captures the register-task-definition arg list
#   register-output.json     — emitted on `aws ecs register-task-definition`
#   call.log                 — full arg log (one line per call)
setup_mock_aws() {
    local tmpdir="$1"
    local mock="$tmpdir/aws"

    cat > "$mock" << 'MOCK'
#!/usr/bin/env bash
set -euo pipefail

TMPDIR="${MOCK_TMPDIR:?MOCK_TMPDIR required}"
echo "$*" >> "$TMPDIR/call.log"

case "$1 $2" in
    "ecs describe-task-definition")
        # Honor a `--query 'taskDefinition'` flag the way the real CLI does:
        # strip the outer wrapper and emit just the inner object.
        if [[ "$*" == *"--query taskDefinition"* ]]; then
            jq '.taskDefinition' < "$TMPDIR/describe-task-def.json"
        else
            cat "$TMPDIR/describe-task-def.json"
        fi
        ;;
    "ssm get-parameter")
        # Honor `--query 'Parameter.Value' --output text`: emit just the string.
        if [[ "$*" == *"--query Parameter.Value"* && "$*" == *"--output text"* ]]; then
            jq -r '.Parameter.Value' < "$TMPDIR/ssm-param.json"
        else
            cat "$TMPDIR/ssm-param.json"
        fi
        ;;
    "ecs register-task-definition")
        # Capture all args verbatim so the test can verify what got registered.
        # Use a stable separator so we can re-split at assertion time.
        printf '%s\n' "$@" > "$TMPDIR/register-args.log"
        if [[ "$*" == *"--query taskDefinition.taskDefinitionArn"* && "$*" == *"--output text"* ]]; then
            jq -r '.taskDefinition.taskDefinitionArn' < "$TMPDIR/register-output.json"
        else
            cat "$TMPDIR/register-output.json"
        fi
        ;;
    "iam get-role")
        # Emit a synthetic role ARN so the legacy "self-heal task-role" branch
        # can flow through tests that exercise it.
        echo "arn:aws:iam::123456789012:role/${5:-judgemind-api-task-dev}"
        ;;
    *)
        echo "MOCK ERROR: unhandled aws call: $*" >&2
        exit 99
        ;;
esac
MOCK
    chmod +x "$mock"
    echo "$mock"
}

# Build the synthetic "current task-def" response. Includes a GITHUB_TOKEN
# secret to simulate the #3765 stale-content scenario where the running
# revision still has a secret terraform has since removed.
write_describe_task_def() {
    local file="$1"
    local include_github_token="${2:-true}"

    local secrets='[{"name":"DATABASE_URL","valueFrom":"arn:aws:secretsmanager:us-west-2:111:secret:db-AAA:url::"},{"name":"JWT_SECRET","valueFrom":"arn:aws:secretsmanager:us-west-2:111:secret:jwt-BBB:secret::"}]'
    if [[ "$include_github_token" == "true" ]]; then
        secrets='[{"name":"DATABASE_URL","valueFrom":"arn:aws:secretsmanager:us-west-2:111:secret:db-AAA:url::"},{"name":"JWT_SECRET","valueFrom":"arn:aws:secretsmanager:us-west-2:111:secret:jwt-BBB:secret::"},{"name":"GITHUB_TOKEN","valueFrom":"arn:aws:secretsmanager:us-west-2:111:secret:gh-CCC:token::"}]'
    fi

    cat > "$file" << JSON
{
  "taskDefinition": {
    "taskDefinitionArn": "arn:aws:ecs:us-west-2:111:task-definition/judgemind-api-dev:99",
    "family": "judgemind-api-dev",
    "executionRoleArn": "arn:aws:iam::111:role/exec-role",
    "taskRoleArn": "arn:aws:iam::111:role/judgemind-api-task-dev",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "512",
    "memory": "1024",
    "containerDefinitions": [
      {
        "name": "api",
        "image": "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/api:OLDSHA",
        "essential": true,
        "secrets": $secrets,
        "environment": [{"name":"NODE_ENV","value":"production"}]
      }
    ]
  }
}
JSON
}

# Build the synthetic SSM-parameter response: terraform has rendered the
# desired container_definitions JSON (without GITHUB_TOKEN) and stored it as
# an SSM string parameter. The shape mirrors `aws ssm get-parameter --name X`.
write_ssm_param() {
    local file="$1"

    # The Value field is a JSON string containing the container_definitions
    # array. We embed it as a JSON-encoded string (escaped quotes).
    cat > "$file" << 'JSON'
{
  "Parameter": {
    "Name": "/judgemind/api/dev/container-definitions",
    "Type": "String",
    "Value": "[{\"name\":\"api\",\"image\":\"PLACEHOLDER\",\"essential\":true,\"secrets\":[{\"name\":\"DATABASE_URL\",\"valueFrom\":\"arn:aws:secretsmanager:us-west-2:111:secret:db-AAA:url::\"},{\"name\":\"JWT_SECRET\",\"valueFrom\":\"arn:aws:secretsmanager:us-west-2:111:secret:jwt-BBB:secret::\"}],\"environment\":[{\"name\":\"NODE_ENV\",\"value\":\"production\"}]}]"
  }
}
JSON
}

# Build the synthetic register-task-definition response.
write_register_output() {
    local file="$1"
    cat > "$file" << 'JSON'
{
  "taskDefinition": {
    "taskDefinitionArn": "arn:aws:ecs:us-west-2:111:task-definition/judgemind-api-dev:100",
    "family": "judgemind-api-dev",
    "revision": 100
  }
}
JSON
}

# Run the script under test with the given environment and args. Captures
# stdout, stderr, and exit code into the temp dir.
run_script() {
    local tmpdir="$1"
    shift
    local mock_aws
    mock_aws=$(setup_mock_aws "$tmpdir")
    local mock_dir
    mock_dir=$(dirname "$mock_aws")

    write_register_output "$tmpdir/register-output.json"

    set +e
    PATH="$mock_dir:$PATH" \
        MOCK_TMPDIR="$tmpdir" \
        "$SCRIPT_UNDER_TEST" "$@" \
        > "$tmpdir/stdout.log" 2> "$tmpdir/stderr.log"
    local ec=$?
    set -e
    echo "$ec" > "$tmpdir/exit_code"
}

assert_exit() {
    local tmpdir="$1"
    local expected="$2"
    local label="$3"
    local actual
    actual=$(cat "$tmpdir/exit_code")
    if [[ "$actual" != "$expected" ]]; then
        fail "$label" "expected exit $expected, got $actual. stderr: $(cat "$tmpdir/stderr.log")"
        return 1
    fi
    return 0
}

assert_register_args_contain() {
    local tmpdir="$1"
    local needle="$2"
    local label="$3"
    if [[ ! -f "$tmpdir/register-args.log" ]]; then
        fail "$label" "register-task-definition was never called"
        return 1
    fi
    if ! grep -q -F "$needle" "$tmpdir/register-args.log"; then
        fail "$label" "register-args.log does not contain '$needle'. Contents: $(cat "$tmpdir/register-args.log")"
        return 1
    fi
    return 0
}

assert_register_args_NOT_contain() {
    local tmpdir="$1"
    local needle="$2"
    local label="$3"
    if [[ ! -f "$tmpdir/register-args.log" ]]; then
        fail "$label" "register-task-definition was never called"
        return 1
    fi
    if grep -q -F "$needle" "$tmpdir/register-args.log"; then
        fail "$label" "register-args.log unexpectedly contains '$needle' (terraform-removed field clobbered through). Contents: $(cat "$tmpdir/register-args.log")"
        return 1
    fi
    return 0
}

assert_call_log_contains() {
    local tmpdir="$1"
    local needle="$2"
    local label="$3"
    if ! grep -q -F "$needle" "$tmpdir/call.log"; then
        fail "$label" "call.log does not contain '$needle'. Contents: $(cat "$tmpdir/call.log")"
        return 1
    fi
    return 0
}

# ─── Test 1: legacy path (no SSM param) ───────────────────────────────────
test_legacy_path_uses_describe_task_def() {
    local label="legacy path (no SSM param) reads from describe-task-definition"
    local tmpdir
    tmpdir=$(make_temp_dir)

    write_describe_task_def "$tmpdir/describe-task-def.json" "true"

    run_script "$tmpdir" \
        --task-family judgemind-api-dev \
        --container-name api \
        --image-uri "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/api:NEWSHA"

    assert_exit "$tmpdir" "0" "$label" || return
    assert_call_log_contains "$tmpdir" "describe-task-definition" "$label" || return
    # Legacy path: GITHUB_TOKEN preserved (this is the bug the SSM path fixes).
    assert_register_args_contain "$tmpdir" "GITHUB_TOKEN" "$label" || return
    # Image swap happened.
    assert_register_args_contain "$tmpdir" "judgemind/api:NEWSHA" "$label" || return

    pass "$label"
}

# ─── Test 2: SSM-source path drops terraform-removed fields ──────────────
# This is the #3765 regression test.
test_ssm_path_does_not_clobber_terraform_removal() {
    local label="SSM source path: terraform-removed GITHUB_TOKEN does NOT come back"
    local tmpdir
    tmpdir=$(make_temp_dir)

    # The CURRENTLY RUNNING task-def still has GITHUB_TOKEN (stale content).
    write_describe_task_def "$tmpdir/describe-task-def.json" "true"
    # But the SSM parameter (terraform-managed source of truth) does NOT.
    write_ssm_param "$tmpdir/ssm-param.json"

    run_script "$tmpdir" \
        --task-family judgemind-api-dev \
        --container-name api \
        --image-uri "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/api:NEWSHA" \
        --desired-container-definitions-ssm-parameter "/judgemind/api/dev/container-definitions"

    assert_exit "$tmpdir" "0" "$label" || return
    # SSM was queried.
    assert_call_log_contains "$tmpdir" "ssm get-parameter" "$label" || return
    # GITHUB_TOKEN must NOT have leaked through from the running task-def.
    assert_register_args_NOT_contain "$tmpdir" "GITHUB_TOKEN" "$label" || return
    # Image swap still happened.
    assert_register_args_contain "$tmpdir" "judgemind/api:NEWSHA" "$label" || return
    # Terraform-managed secrets ARE present.
    assert_register_args_contain "$tmpdir" "DATABASE_URL" "$label" || return
    assert_register_args_contain "$tmpdir" "JWT_SECRET" "$label" || return

    pass "$label"
}

# ─── Test 3: SSM path swaps image (placeholder replaced) ─────────────────
test_ssm_path_swaps_image() {
    local label="SSM source path: image PLACEHOLDER replaced with input image-uri"
    local tmpdir
    tmpdir=$(make_temp_dir)

    write_describe_task_def "$tmpdir/describe-task-def.json" "false"
    write_ssm_param "$tmpdir/ssm-param.json"

    run_script "$tmpdir" \
        --task-family judgemind-api-dev \
        --container-name api \
        --image-uri "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/api:abc1234" \
        --desired-container-definitions-ssm-parameter "/judgemind/api/dev/container-definitions"

    assert_exit "$tmpdir" "0" "$label" || return
    assert_register_args_contain "$tmpdir" "judgemind/api:abc1234" "$label" || return
    # The PLACEHOLDER from the SSM template must NOT appear in the registered
    # container_definitions.
    assert_register_args_NOT_contain "$tmpdir" "PLACEHOLDER" "$label" || return

    pass "$label"
}

# ─── Test 4: SSM path still fetches family-level metadata ────────────────
# Some fields (cpu, memory, network mode, exec role, task role) live on the
# task-definition itself, not container_definitions. The SSM-source path
# still needs `describe-task-definition` to read those — only the
# container_definitions array is sourced from SSM. Verify the metadata is
# preserved.
test_ssm_path_preserves_task_metadata() {
    local label="SSM source path: cpu/memory/exec-role still come from describe-task-definition"
    local tmpdir
    tmpdir=$(make_temp_dir)

    write_describe_task_def "$tmpdir/describe-task-def.json" "false"
    write_ssm_param "$tmpdir/ssm-param.json"

    run_script "$tmpdir" \
        --task-family judgemind-api-dev \
        --container-name api \
        --image-uri "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/api:NEWSHA" \
        --desired-container-definitions-ssm-parameter "/judgemind/api/dev/container-definitions"

    assert_exit "$tmpdir" "0" "$label" || return
    # Both data sources were consulted.
    assert_call_log_contains "$tmpdir" "describe-task-definition" "$label" || return
    assert_call_log_contains "$tmpdir" "ssm get-parameter" "$label" || return
    # Metadata from the running task-def made it into the register call.
    assert_register_args_contain "$tmpdir" "FARGATE" "$label" || return
    assert_register_args_contain "$tmpdir" "awsvpc" "$label" || return

    pass "$label"
}

# ─── Test 5: missing required arg ────────────────────────────────────────
test_missing_required_arg_fails() {
    local label="missing --image-uri exits non-zero"
    local tmpdir
    tmpdir=$(make_temp_dir)

    write_describe_task_def "$tmpdir/describe-task-def.json" "false"

    run_script "$tmpdir" \
        --task-family judgemind-api-dev \
        --container-name api

    local ec
    ec=$(cat "$tmpdir/exit_code")
    if [[ "$ec" == "0" ]]; then
        fail "$label" "expected non-zero exit, got 0"
        return
    fi
    pass "$label"
}

# ─── Test 6: container name not found in SSM JSON fails ──────────────────
test_ssm_path_unknown_container_fails() {
    local label="SSM path: unknown container-name in SSM JSON exits non-zero"
    local tmpdir
    tmpdir=$(make_temp_dir)

    write_describe_task_def "$tmpdir/describe-task-def.json" "false"
    write_ssm_param "$tmpdir/ssm-param.json"

    run_script "$tmpdir" \
        --task-family judgemind-api-dev \
        --container-name not-a-real-container \
        --image-uri "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/api:NEWSHA" \
        --desired-container-definitions-ssm-parameter "/judgemind/api/dev/container-definitions"

    local ec
    ec=$(cat "$tmpdir/exit_code")
    if [[ "$ec" == "0" ]]; then
        fail "$label" "expected non-zero exit when container-name not found in SSM JSON, got 0. stderr: $(cat "$tmpdir/stderr.log")"
        return
    fi
    pass "$label"
}

# ─── Run all tests ───────────────────────────────────────────────────────

test_legacy_path_uses_describe_task_def
test_ssm_path_does_not_clobber_terraform_removal
test_ssm_path_swaps_image
test_ssm_path_preserves_task_metadata
test_missing_required_arg_fails
test_ssm_path_unknown_container_fails

echo
echo "Tests: $TESTS, Failures: $FAILURES"

if [[ "$FAILURES" -gt 0 ]]; then
    exit 1
fi
exit 0
