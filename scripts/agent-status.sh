#!/usr/bin/env bash
# agent-status.sh — Show a human-readable summary of a Claude Code agent.
#
# Parses the agent's NDJSON output file and displays recent tool calls,
# status, duration, and token usage in a compact format.
#
# Usage:
#   scripts/agent-status.sh <agent-id>           # Looks up the output file
#   scripts/agent-status.sh <output-file-path>    # Direct path to .output file
#   scripts/agent-status.sh <target> --last 10    # Show last 10 actions
#   scripts/agent-status.sh <target> --full       # Show all actions

set -euo pipefail

# Resolve the directory this script lives in
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/agent_status.py" "$@"
