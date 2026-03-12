#!/usr/bin/env bash
# Stop the Telegram responder daemon.
#
# Reads the PID from tmp/tg_responder.pid, sends SIGTERM, and waits for
# the process to exit (up to 10 seconds). Falls back to SIGKILL if it
# doesn't exit gracefully.
#
# Usage:
#   scripts/tg-stop-responder.sh [--pid-file tmp/tg_responder.pid]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="${REPO_ROOT}/tmp/tg_responder.pid"

# Allow overriding the PID file path.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --pid-file)
            PID_FILE="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if [[ ! -f "$PID_FILE" ]]; then
    echo "No PID file found at ${PID_FILE} — daemon is not running."
    exit 0
fi

PID="$(cat "$PID_FILE")"

if ! kill -0 "$PID" 2>/dev/null; then
    echo "Stale PID file (pid ${PID} is not running) — cleaning up."
    rm -f "$PID_FILE" "${PID_FILE%.pid}.stop"
    exit 0
fi

echo "Sending SIGTERM to responder daemon (pid ${PID})..."
kill "$PID"

# Wait up to 10 seconds for graceful shutdown.
for i in $(seq 1 20); do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "Responder daemon stopped."
        rm -f "$PID_FILE" "${PID_FILE%.pid}.stop"
        exit 0
    fi
    sleep 0.5
done

# Still alive — escalate to SIGKILL.
echo "Daemon did not exit within 10 seconds — sending SIGKILL."
kill -9 "$PID" 2>/dev/null || true
rm -f "$PID_FILE" "${PID_FILE%.pid}.stop"
echo "Responder daemon killed."
