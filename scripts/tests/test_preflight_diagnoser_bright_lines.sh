#!/usr/bin/env bash
# test_preflight_diagnoser_bright_lines.sh — Tests for the issue #3366
# diagnoser bright lines added to .claude/hooks/preflight-bash.sh.
#
# Asserts that each of the four bright lines fires when the
# JUDGEMIND_DIAGNOSER_RUN=1 sentinel is set (the env var the daemon
# exports on the diagnoser subprocess), and does NOT fire when the
# sentinel is unset (every other Bash invocation in the daemon /
# agent-runner / interactive operator session).
#
# The four bright lines (issue #3366):
#   1. No production deploy.
#   2. No PAT rotation / `gh auth switch`.
#   3. No force-push to main, no amending merged commits.
#   4. No recursive `/diagnose-failure` invocation.
#
# Usage:
#   scripts/tests/test_preflight_diagnoser_bright_lines.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$SCRIPT_DIR/.claude/hooks/preflight-bash.sh"

if [[ ! -x "$HOOK" ]]; then
    echo "FAIL: hook is not executable: $HOOK" >&2
    exit 1
fi

FAILURES=0
TESTS=0

# Run the hook with a given command + diagnoser-env state and assert the
# resulting exit code. The hook reads tool input as JSON on stdin.
#
# $1 = description, $2 = command, $3 = expected exit, $4 = "1" to set
#      JUDGEMIND_DIAGNOSER_RUN, "" to unset.
run_test() {
    local desc="$1"
    local cmd="$2"
    local expect="$3"
    local diag_env="${4:-}"
    TESTS=$((TESTS + 1))
    local payload
    payload=$(printf '{"tool_input": {"command": %s, "timeout": 1200000}}' \
        "$(printf '%s' "$cmd" | python3 -c 'import sys, json; print(json.dumps(sys.stdin.read()))')")
    local out
    local rc
    local payload_tmp
    payload_tmp=$(mktemp)
    printf '%s' "$payload" > "$payload_tmp"
    if [[ "$diag_env" == "1" ]]; then
        out=$(JUDGEMIND_DIAGNOSER_RUN=1 bash "$HOOK" < "$payload_tmp" 2>&1)
        rc=$?
    else
        # Defensive unset so the test is hermetic regardless of whether
        # the parent shell already had JUDGEMIND_DIAGNOSER_RUN set.
        out=$(env -u JUDGEMIND_DIAGNOSER_RUN bash "$HOOK" < "$payload_tmp" 2>&1)
        rc=$?
    fi
    rm -f "$payload_tmp"
    if [[ "$rc" -eq "$expect" ]]; then
        printf '  PASS: %s\n' "$desc"
    else
        printf '  FAIL: %s (expected exit=%s, got exit=%s)\n' "$desc" "$expect" "$rc"
        if [[ -n "$out" ]]; then
            printf '         output: %s\n' "$out"
        fi
        FAILURES=$((FAILURES + 1))
    fi
}

# ── Bright line 1: No production deploy ───────────────────────────────
echo "Bright line 1: no production deploy (terraform / ECS-production writes)"
run_test \
    "terraform apply environments/production blocked when JUDGEMIND_DIAGNOSER_RUN=1" \
    "terraform -chdir=infra/terraform/environments/production apply -auto-approve" \
    2 1
run_test \
    "terraform destroy environments/production blocked when JUDGEMIND_DIAGNOSER_RUN=1" \
    "terraform -chdir=infra/terraform/environments/production destroy" \
    2 1
# Sanity: terraform apply against a non-production environment is fine
# from the diagnoser context. (#3366 — only the production guard is
# diagnoser-scope; environments/dev applies remain allowed.)
run_test \
    "terraform apply environments/dev allowed when JUDGEMIND_DIAGNOSER_RUN=1" \
    "terraform -chdir=infra/terraform/environments/dev apply -auto-approve" \
    0 1
# When the sentinel is unset, even a production apply does not fire the
# diagnoser bright line. (Some other check may still block — terraform
# apply against environments/production is still valid via the human
# CI workflow.)
run_test \
    "terraform apply environments/production allowed when JUDGEMIND_DIAGNOSER_RUN unset" \
    "terraform -chdir=infra/terraform/environments/production apply -auto-approve" \
    0 ""
# ECS service writes against a *-production cluster are also blocked.
run_test \
    "aws ecs update-service against *-production blocked when diagnoser env set" \
    "aws ecs update-service --cluster judgemind-dispatcher-production --service api --desired-count 2" \
    2 1
run_test \
    "aws ecs run-task against *-production blocked when diagnoser env set" \
    "aws ecs run-task --cluster judgemind-api-production --task-definition foo" \
    2 1
# ECS writes against *-dev are allowed.
run_test \
    "aws ecs update-service against *-dev allowed when diagnoser env set" \
    "aws ecs update-service --cluster judgemind-dispatcher-dev --service api" \
    0 1
# ECS read commands are always allowed.
run_test \
    "aws ecs describe-tasks against *-production allowed (read-only)" \
    "aws ecs describe-tasks --cluster judgemind-dispatcher-production --tasks abc" \
    0 1

# ── Bright line 2: No PAT rotation / `gh auth switch` ─────────────────
echo "Bright line 2: no gh auth switch / PAT rotation"
run_test \
    "gh auth switch blocked when JUDGEMIND_DIAGNOSER_RUN=1" \
    "gh auth switch --user judgemind-agent" \
    2 1
run_test \
    "gh auth switch allowed when JUDGEMIND_DIAGNOSER_RUN unset" \
    "gh auth switch --user judgemind-agent" \
    0 ""
# gh auth status / login are not blocked (only switch).
run_test \
    "gh auth status allowed when diagnoser env set" \
    "gh auth status" \
    0 1

# ── Bright line 3: No force-push to main / amending merged commits ────
echo "Bright line 3: no force-push to main, no commit --amend"
# git push --force to feature branch — allowed (push-to-main check #0
# already covers feature-branch force-push semantics).
run_test \
    "git push --force-with-lease to feature branch allowed when diagnoser env set" \
    "git push --force-with-lease origin worktree-agent-foo" \
    0 1
# git push --force main blocked.
run_test \
    "git push --force origin main blocked when diagnoser env set" \
    "git push --force origin main" \
    2 1
run_test \
    "git push --force-with-lease origin main blocked when diagnoser env set" \
    "git push --force-with-lease origin main" \
    2 1
# git commit --amend is blocked from the diagnoser context (creates a
# fresh commit instead — preserves history).
run_test \
    "git commit --amend blocked when JUDGEMIND_DIAGNOSER_RUN=1" \
    "git commit --amend --no-edit" \
    2 1
run_test \
    "git commit --amend allowed when JUDGEMIND_DIAGNOSER_RUN unset" \
    "git commit --amend --no-edit" \
    0 ""
# Plain commits (without --amend) are allowed from the diagnoser.
run_test \
    "git commit -m allowed when diagnoser env set" \
    "git commit -m fix:foo" \
    0 1

# ── Bright line 4: No recursive /diagnose-failure invocation ──────────
echo "Bright line 4: no recursive /diagnose-failure invocation"
run_test \
    "claude -p /diagnose-failure blocked when JUDGEMIND_DIAGNOSER_RUN=1" \
    "claude -p /diagnose-failure 99 --max-turns 30" \
    2 1
run_test \
    "claude -p /diagnose-failure allowed when JUDGEMIND_DIAGNOSER_RUN unset (the daemon launches it)" \
    "claude -p /diagnose-failure 99 --max-turns 30" \
    0 ""
# Other claude skills are NOT blocked from the diagnoser context (sub-
# skill invocation is uncapped — see SKILL.md).
run_test \
    "claude -p /task-v2-fix-conflict allowed when diagnoser env set" \
    "claude -p /task-v2-fix-conflict 99" \
    0 1

# ── Sanity: hook exit-2 messages mention 'diagnoser bright line' ──────
# So an operator scanning CloudWatch can grep for the marker.
echo "Sanity: blocked-message tag"
TESTS=$((TESTS + 1))
_sanity_tmp=$(mktemp)
printf '{"tool_input": {"command": "gh auth switch", "timeout": 1200000}}' > "$_sanity_tmp"
_msg=$(JUDGEMIND_DIAGNOSER_RUN=1 bash "$HOOK" < "$_sanity_tmp" 2>&1 || true)
rm -f "$_sanity_tmp"
if printf '%s' "$_msg" | grep -q "diagnoser bright line"; then
    echo "  PASS: blocked message includes 'diagnoser bright line' tag"
else
    echo "  FAIL: blocked message did not include the 'diagnoser bright line' tag"
    printf '         output: %s\n' "$_msg"
    FAILURES=$((FAILURES + 1))
fi

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES)) / $TESTS passed"
if [[ "$FAILURES" -gt 0 ]]; then
    exit 1
fi
echo "All diagnoser bright-line tests passed."
exit 0
