#!/usr/bin/env bash
# test_check_ingestion_worker_task_def_fingerprint.sh — Unit tests for
# scripts/check-ingestion-worker-task-def-fingerprint.sh
#
# The fingerprint guard exists to close the silent-drift class introduced
# by `aws_ecs_service.ingestion_worker.lifecycle.ignore_changes =
# [task_definition]` (#4044): when terraform-apply registers a new
# task-def revision but the service stays pinned to a stale one, the
# running container's env vars no longer match terraform's intent. The
# guard hashes the running task-def's container_definitions and the
# terraform-rendered SSM parameter (after stripping the per-deploy image
# tag and sorting keys) and exits non-zero on mismatch.
#
# Tests cover:
#
#   1. Match — image-tag-only diff between the two sources is treated as
#      a match (canonicalization works). Exit 0.
#
#   2. Match — keys in different order on each side are treated as a
#      match (recursive sort works). Exit 0.
#
#   3. Drift — env var differs between SSM and running task-def. Exit 1
#      with a unified diff on stderr and a recovery hint.
#
#   4. Drift — secrets list differs (one source has an extra secret).
#      Exit 1.
#
#   5. Tooling-broken — SSM parameter contains invalid JSON. Exit 2
#      (distinguishes "guard tool broken" from "drift detected", so the
#      caller can route to the right alert channel).
#
#   6. Tooling-broken — describe-services returns no PRIMARY deployment.
#      Exit 2.
#
# Usage:
#   scripts/tests/test_check_ingestion_worker_task_def_fingerprint.sh
#
# Exit codes:
#   0 — all tests passed.
#   1 — one or more tests failed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_UNDER_TEST="$REPO_ROOT/scripts/check-ingestion-worker-task-def-fingerprint.sh"

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

# Set up a mock `aws` CLI that emits canned responses for ssm get-parameter,
# ecs describe-services, and ecs describe-task-definition. The mock reads
# fixture files from $MOCK_TMPDIR and writes every invocation to call.log.
#
# Files in $tmpdir/:
#   ssm-param-value.txt       — emitted on `aws ssm get-parameter ... --query Parameter.Value --output text`
#   describe-services.json    — emitted on `aws ecs describe-services`
#   describe-task-def.json    — emitted on `aws ecs describe-task-definition`
#   call.log                  — full arg log (one line per call)
setup_mock_aws() {
    local tmpdir="$1"
    local mock="$tmpdir/aws"

    cat > "$mock" << 'MOCK'
#!/usr/bin/env bash
set -euo pipefail

TMPDIR="${MOCK_TMPDIR:?MOCK_TMPDIR required}"
echo "$*" >> "$TMPDIR/call.log"

case "$1 $2" in
    "ssm get-parameter")
        cat "$TMPDIR/ssm-param-value.txt"
        ;;
    "ecs describe-services")
        # The script runs --query "services[0].deployments[?status=='PRIMARY']|[0].taskDefinition" --output text
        # so emit just the task-def ARN string.
        cat "$TMPDIR/describe-services.json"
        ;;
    "ecs describe-task-definition")
        # --query 'taskDefinition.containerDefinitions' --output json
        # so the fixture should already be the containerDefinitions array.
        cat "$TMPDIR/describe-task-def.json"
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

# Common SSM payload — terraform's intent. JSON array of containers.
write_ssm_match_baseline() {
    local file="$1"
    cat > "$file" << 'JSON'
[
  {
    "name": "ingestion-worker",
    "image": "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper:TFAPPLY",
    "essential": true,
    "command": ["ingestion"],
    "environment": [
      {"name": "ENVIRONMENT", "value": "dev"},
      {"name": "LLM_PROVIDER", "value": "google"}
    ],
    "secrets": [
      {"name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:us-west-2:111:secret:db-AAA:url::"}
    ]
  }
]
JSON
}

# Running task-def's containerDefinitions — same content as SSM but with a
# different image tag (the per-deploy noise the guard must ignore).
write_running_match_baseline() {
    local file="$1"
    cat > "$file" << 'JSON'
[
  {
    "name": "ingestion-worker",
    "image": "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper:DEPLOYSCRAPER",
    "essential": true,
    "command": ["ingestion"],
    "environment": [
      {"name": "ENVIRONMENT", "value": "dev"},
      {"name": "LLM_PROVIDER", "value": "google"}
    ],
    "secrets": [
      {"name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:us-west-2:111:secret:db-AAA:url::"}
    ]
  }
]
JSON
}

# Run the script under test against a populated $tmpdir of fixtures.
run_script() {
    local tmpdir="$1"
    shift
    local mock_path
    mock_path=$(setup_mock_aws "$tmpdir")
    local mock_dir
    mock_dir="$(dirname "$mock_path")"

    PATH="$mock_dir:$PATH" \
        MOCK_TMPDIR="$tmpdir" \
        "$SCRIPT_UNDER_TEST" "$@"
}

# ── Test 1: image-tag-only diff is a match ─────────────────────────────────

test_match_image_tag_only_diff() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    write_ssm_match_baseline "$tmpdir/ssm-param-value.txt"
    write_running_match_baseline "$tmpdir/describe-task-def.json"
    echo "arn:aws:ecs:us-west-2:111:task-definition/judgemind-ingestion-worker-dev:42" > "$tmpdir/describe-services.json"

    if run_script "$tmpdir" >"$tmpdir/stdout" 2>"$tmpdir/stderr"; then
        if grep -q "OK:" "$tmpdir/stdout"; then
            pass "match: image-tag-only diff is treated as match"
        else
            fail "match: exit 0 but no OK: line on stdout" "$(cat "$tmpdir/stdout")"
        fi
    else
        fail "match: image-tag-only diff exited non-zero" "$(cat "$tmpdir/stderr")"
    fi
}

# ── Test 2: key-order diff is a match ──────────────────────────────────────

test_match_key_order_diff() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    # SSM in canonical order
    write_ssm_match_baseline "$tmpdir/ssm-param-value.txt"

    # Running task-def with the SAME logical content but keys in a different order
    cat > "$tmpdir/describe-task-def.json" << 'JSON'
[
  {
    "secrets": [
      {"valueFrom": "arn:aws:secretsmanager:us-west-2:111:secret:db-AAA:url::", "name": "DATABASE_URL"}
    ],
    "environment": [
      {"value": "dev", "name": "ENVIRONMENT"},
      {"value": "google", "name": "LLM_PROVIDER"}
    ],
    "command": ["ingestion"],
    "essential": true,
    "image": "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper:OTHERSHA",
    "name": "ingestion-worker"
  }
]
JSON
    echo "arn:aws:ecs:us-west-2:111:task-definition/judgemind-ingestion-worker-dev:42" > "$tmpdir/describe-services.json"

    if run_script "$tmpdir" >"$tmpdir/stdout" 2>"$tmpdir/stderr"; then
        pass "match: key-order diff is treated as match"
    else
        fail "match: key-order diff exited non-zero" "$(cat "$tmpdir/stderr")"
    fi
}

# ── Test 3: env var drift is caught ────────────────────────────────────────

test_drift_env_var() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    # SSM — terraform's intent has LLM_PROVIDER=google
    write_ssm_match_baseline "$tmpdir/ssm-param-value.txt"

    # Running task-def — has LLM_PROVIDER=anthropic (the pre-flip stale value)
    cat > "$tmpdir/describe-task-def.json" << 'JSON'
[
  {
    "name": "ingestion-worker",
    "image": "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper:STALE",
    "essential": true,
    "command": ["ingestion"],
    "environment": [
      {"name": "ENVIRONMENT", "value": "dev"},
      {"name": "LLM_PROVIDER", "value": "anthropic"}
    ],
    "secrets": [
      {"name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:us-west-2:111:secret:db-AAA:url::"}
    ]
  }
]
JSON
    echo "arn:aws:ecs:us-west-2:111:task-definition/judgemind-ingestion-worker-dev:601" > "$tmpdir/describe-services.json"

    set +e
    run_script "$tmpdir" >"$tmpdir/stdout" 2>"$tmpdir/stderr"
    local rc=$?
    set -e
    if [[ "$rc" -ne 1 ]]; then
        fail "drift: env var diff should exit 1, got $rc" "$(cat "$tmpdir/stderr")"
        return
    fi
    if ! grep -q "DRIFT:" "$tmpdir/stderr"; then
        fail "drift: missing DRIFT: marker on stderr" "$(cat "$tmpdir/stderr")"
        return
    fi
    if ! grep -q "anthropic\|google" "$tmpdir/stderr"; then
        fail "drift: diff body should mention the changed env value" "$(cat "$tmpdir/stderr")"
        return
    fi
    if ! grep -q "force-new-deployment" "$tmpdir/stderr"; then
        fail "drift: stderr should include a recovery hint with --force-new-deployment" "$(cat "$tmpdir/stderr")"
        return
    fi
    pass "drift: env var diff is caught with diff and recovery hint"
}

# ── Test 4: secrets-list drift is caught ───────────────────────────────────

test_drift_secrets_list() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    # SSM — terraform's intent has only DATABASE_URL
    write_ssm_match_baseline "$tmpdir/ssm-param-value.txt"

    # Running task-def — has an extra LEGACY_SECRET that terraform has removed
    cat > "$tmpdir/describe-task-def.json" << 'JSON'
[
  {
    "name": "ingestion-worker",
    "image": "111.dkr.ecr.us-west-2.amazonaws.com/judgemind/scraper:STALE",
    "essential": true,
    "command": ["ingestion"],
    "environment": [
      {"name": "ENVIRONMENT", "value": "dev"},
      {"name": "LLM_PROVIDER", "value": "google"}
    ],
    "secrets": [
      {"name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:us-west-2:111:secret:db-AAA:url::"},
      {"name": "LEGACY_SECRET", "valueFrom": "arn:aws:secretsmanager:us-west-2:111:secret:legacy-XXX"}
    ]
  }
]
JSON
    echo "arn:aws:ecs:us-west-2:111:task-definition/judgemind-ingestion-worker-dev:601" > "$tmpdir/describe-services.json"

    set +e
    run_script "$tmpdir" >"$tmpdir/stdout" 2>"$tmpdir/stderr"
    local rc=$?
    set -e
    if [[ "$rc" -eq 1 ]] && grep -q "LEGACY_SECRET" "$tmpdir/stderr"; then
        pass "drift: secrets-list diff is caught"
    else
        fail "drift: secrets-list diff should exit 1 with LEGACY_SECRET in diff" "rc=$rc; $(cat "$tmpdir/stderr")"
    fi
}

# ── Test 5: invalid SSM JSON is exit 2 (tooling broken) ────────────────────

test_tooling_broken_invalid_ssm() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    echo "this-is-not-json" > "$tmpdir/ssm-param-value.txt"
    write_running_match_baseline "$tmpdir/describe-task-def.json"
    echo "arn:aws:ecs:us-west-2:111:task-definition/judgemind-ingestion-worker-dev:42" > "$tmpdir/describe-services.json"

    set +e
    run_script "$tmpdir" >"$tmpdir/stdout" 2>"$tmpdir/stderr"
    local rc=$?
    set -e
    if [[ "$rc" -eq 2 ]] && grep -q "did not contain valid JSON" "$tmpdir/stderr"; then
        pass "tooling broken: invalid SSM JSON exits 2 (distinct from drift exit 1)"
    else
        fail "tooling broken: invalid SSM should exit 2, got $rc" "$(cat "$tmpdir/stderr")"
    fi
}

# ── Test 6: missing PRIMARY deployment is exit 2 ───────────────────────────

test_tooling_broken_no_primary() {
    local tmpdir
    tmpdir=$(make_temp_dir)

    write_ssm_match_baseline "$tmpdir/ssm-param-value.txt"
    write_running_match_baseline "$tmpdir/describe-task-def.json"
    # describe-services returns the literal "None" that the AWS CLI emits when
    # --query has no match and --output text is set.
    echo "None" > "$tmpdir/describe-services.json"

    set +e
    run_script "$tmpdir" >"$tmpdir/stdout" 2>"$tmpdir/stderr"
    local rc=$?
    set -e
    if [[ "$rc" -eq 2 ]] && grep -q "PRIMARY" "$tmpdir/stderr"; then
        pass "tooling broken: missing PRIMARY deployment exits 2"
    else
        fail "tooling broken: missing PRIMARY should exit 2, got $rc" "$(cat "$tmpdir/stderr")"
    fi
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_match_image_tag_only_diff
test_match_key_order_diff
test_drift_env_var
test_drift_secrets_list
test_tooling_broken_invalid_ssm
test_tooling_broken_no_primary

echo ""
echo "Tests: $TESTS, Failures: $FAILURES"
[[ "$FAILURES" -eq 0 ]] || exit 1
