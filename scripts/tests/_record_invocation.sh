#!/usr/bin/env bash
# _record_invocation.sh — Shared stub recorder for test harnesses.
#
# Source this file (do NOT execute it) from a stub shim to record the
# invocation into $INVOCATIONS_DIR/<tool>.log.
#
# Usage (from a stub script):
#   . "$(dirname "$0")/_record_invocation.sh" <tool_name> "$@"
#
# What gets written to the log:
#   CALL <quoted-args>         — the full argv, shell-quoted via printf %q
#   STDIN:<content>            — if stdin was non-tty, the full stdin bytes
#   FILE:<key>=<contents>      — for each argv arg of the form key=/abs/path
#                                where /abs/path is a regular file
#
# After sourcing, the variable $stdin_content is available in the caller's
# scope (empty string if stdin was a tty or was empty).
#
# bash 3.2 compatible — no bash 4+ features.

TOOL_NAME="$1"
shift
INVOCATIONS_LOG="${INVOCATIONS_DIR:-/tmp}/${TOOL_NAME}.log"

# ── Record argv ────────────────────────────────────────────────────────────
{
    printf 'CALL '
    for arg in "$@"; do
        printf '%q ' "$arg"
    done
    printf '\n'
} >> "$INVOCATIONS_LOG"

# ── Capture stdin (non-tty only) ───────────────────────────────────────────
stdin_content=""
if [[ ! -t 0 ]]; then
    stdin_content=$(cat)
    if [[ -n "$stdin_content" ]]; then
        printf 'STDIN:%s\n' "$stdin_content" >> "$INVOCATIONS_LOG"
    fi
fi

# ── Resolve key=/abs/path argv slots ──────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        *=/* )
            _key="${arg%%=*}"
            _path="${arg#*=}"
            if [[ -f "$_path" ]]; then
                _contents=$(cat "$_path" 2>/dev/null || true)
                printf 'FILE:%s=%s\n' "$_key" "$_contents" >> "$INVOCATIONS_LOG"
            fi
            ;;
    esac
done
unset _key _path _contents
