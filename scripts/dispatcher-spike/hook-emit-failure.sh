#!/usr/bin/env bash
# dispatcher-spike/hook-emit-failure.sh — installed into the spike container
# as /usr/local/bin/dispatcher-spike-hook.sh.
#
# Registered in /etc/dispatcher-spike/settings.json as a PreToolUse hook on
# matcher "Read". Fires exactly once per `Read` tool call inside a
# `claude -p` subprocess. Calls `scripts/dispatcher/emit_failure.py` with
# --category spike_test. The hook MUST NOT propagate a non-zero exit —
# even if the insert fails (bad DATABASE_URL, VPC unreachable, schema
# missing), the Read tool call continues.
#
# The hook receives the tool input as JSON on stdin. We don't actually
# need the fields for spike 0.2 — the measurement is purely latency of
# the insert path — but we forward the tool name into `--details` so the
# spike row shows which tool fired it.
#
# SPIKE_AGENT_ID is set by container-entry.sh to a unique id per task
# invocation, so the harness can filter its rows deterministically:
#   SELECT ... FROM dispatcher_spike.failures WHERE agent_id='harness-<uuid>';

set -u

SPIKE_AGENT_ID="${SPIKE_AGENT_ID:-unknown}"
EMIT_SCRIPT="${DISPATCHER_EMIT_FAILURE:-/usr/local/bin/emit_failure.py}"

# Read stdin — the tool_input JSON — but ignore its contents; we only
# need a deterministic `Read`-fired marker for the spike.
STDIN_INPUT="$(cat || true)"
TOOL_NAME="$(printf '%s' "$STDIN_INPUT" | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
    print(d.get('tool_name') or d.get('tool') or 'Read')
except Exception:
    print('Read')" 2>/dev/null || printf 'Read')"

DETAILS_JSON="$(printf '{"tool":"%s","hook":"spike-0.2"}' "$TOOL_NAME")"

# Invoke the emitter. Redirect stderr to stderr (Claude Code captures
# both streams), and swallow its exit code — it always exits 0, but be
# defensive against an argparse-level failure regression.
python3 "$EMIT_SCRIPT" \
    --category spike_test \
    --agent-id "$SPIKE_AGENT_ID" \
    --details "$DETAILS_JSON" \
    --table dispatcher_spike.failures || true

exit 0
