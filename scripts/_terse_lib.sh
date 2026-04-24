#!/usr/bin/env bash
# _terse_lib.sh — Shared verbosity helpers for agent-facing scripts.
#
# Source this file in agent-facing scripts to get three helpers:
#   terse_parse_verbose_flag "$@"  — parse --verbose/-v and JM_VERBOSE
#   vlog "msg"                     — print to stderr only when VERBOSE=1
#   err "msg"                      — print to stderr unconditionally
#
# Usage pattern:
#   source "$(dirname "${BASH_SOURCE[0]}")/_terse_lib.sh"
#   eval "$(terse_parse_verbose_flag "$@")"  # resets $@ after consuming --verbose
#   ... use vlog for progress, err for errors ...
#
# Bash 3.2 compatible (no mapfile, no namerefs).

# VERBOSE defaults to 0 unless JM_VERBOSE is already set in the environment.
if [[ "${JM_VERBOSE:-}" == "1" ]]; then
    VERBOSE=1
else
    VERBOSE="${VERBOSE:-0}"
fi

# terse_parse_verbose_flag — consume --verbose/-v from the argument list and
# set VERBOSE=1.  Also honours JM_VERBOSE=1 in the environment.
#
# Because bash functions cannot modify the caller's $@ directly, this function
# prints a shell eval-able "set -- ..." statement to stdout.  The caller must:
#
#   eval "$(terse_parse_verbose_flag "$@")"
#
# This is Bash 3.2 compatible (no namerefs, no mapfile).
terse_parse_verbose_flag() {
    local new_args=""
    local first=1
    local arg

    # Honor JM_VERBOSE env var
    if [[ "${JM_VERBOSE:-}" == "1" ]]; then
        VERBOSE=1
    fi

    for arg in "$@"; do
        case "$arg" in
            --verbose|-v)
                VERBOSE=1
                ;;
            *)
                if [[ "$first" -eq 1 ]]; then
                    # Use printf %q to safely quote any argument value
                    new_args="$(printf '%q' "$arg")"
                    first=0
                else
                    new_args="$new_args $(printf '%q' "$arg")"
                fi
                ;;
        esac
    done

    if [[ "$first" -eq 1 ]]; then
        # No non-verbose args were found
        printf 'set -- '
        printf '\n'
    else
        printf 'set -- %s\n' "$new_args"
    fi
}

# vlog — print an informational message to stderr, but only when VERBOSE=1.
vlog() {
    if [[ "${VERBOSE:-0}" == "1" ]]; then
        echo "$*" >&2
    fi
}

# err — print an error message to stderr unconditionally.
err() {
    echo "$*" >&2
}
