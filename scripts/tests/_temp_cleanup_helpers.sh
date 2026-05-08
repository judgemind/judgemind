#!/usr/bin/env bash
# _temp_cleanup_helpers.sh — Shared EXIT-trap cleanup for shell-test fixtures.
#
# Source this file (do NOT execute it) from a shell test to register
# temporary directories and files for automatic cleanup on exit.
#
# Usage:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#   . "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"
#
#   tmp=$(mktemp -d)
#   register_temp_dir "$tmp"
#
#   tmpfile=$(mktemp)
#   register_temp_file "$tmpfile"
#
# The first call to ``register_temp_dir`` / ``register_temp_file``
# installs an EXIT trap that ``rm -rf`` (for dirs) / ``rm -f`` (for
# files) every registered path. Re-installation is idempotent — calling
# either helper after the trap is already set is a no-op for the trap
# itself.
#
# Why this helper exists
# ----------------------
# Before #4343, ~12+ shell test fixtures hand-rolled the same
# EXIT-trap cleanup pattern. Each copy independently had to remember
# three concerns and get them all right:
#
#   1. **Guarded iteration** for bash 3.2 + ``set -u``. The naive form
#      ``for d in "${TEMP_DIRS[@]}"`` trips ``unbound variable`` on
#      bash 3.2 when the array was declared but never populated. The
#      bash-3.2-safe idiom is
#      ``for d in ${TEMP_DIRS[@]+"${TEMP_DIRS[@]}"}; do`` — the
#      ``[@]+...`` substitutes nothing when the array is empty/unset,
#      and the inner ``${TEMP_DIRS[@]}`` only evaluates when the array
#      is non-empty. See #4336 / scripts/check-bash-set-u-empty-array.sh
#      Pass 3 for the full rationale.
#
#   2. **``set +eu`` scope** inside the trap body so an individual
#      ``rm -rf`` failure on one entry does not propagate up and
#      stomp on a later one (or the test's actual exit code).
#
#   3. **Existence check** before ``rm`` so a path that was already
#      removed (or that ``mktemp`` failed to create) does not produce
#      a spurious error.
#
# This helper bakes all three concerns in once, audited once. The
# ``scripts/check-bash-set-u-empty-array.sh`` Pass 3 guard prevents
# future drift on the iteration form, but the boilerplate itself is
# still copy-paste churn — the sourceable helper turns that into one
# ``source`` line plus one ``register_temp_*`` call per resource.
#
# Compatibility
# -------------
# bash 3.2 compatible — no bash 4+ features (no ``mapfile``, no
# associative arrays, no namerefs, no ``${var,,}`` case-conversion).
#
# Trap composition
# ----------------
# The helper installs a single ``EXIT`` trap that calls the internal
# ``_temp_cleanup_helpers__cleanup`` function. The cleanup function
# does THREE things, in order:
#
#   1. Run any registered cleanup hooks (``register_cleanup_hook
#      <fn>``), in registration order. Hooks are bash functions in
#      the caller's scope — typical use is restoring a saved ``PATH``
#      or running ``git rebase --abort``.
#   2. ``rm -rf`` registered directories (``register_temp_dir``).
#   3. ``rm -f`` registered files (``register_temp_file``).
#
# This three-phase design means a test that needs both standard
# temp-dir cleanup AND a custom step (PATH restore, mock-server
# teardown) can use this helper without rolling its own trap.
# Hooks run BEFORE the rm phase so a hook can rely on registered
# directories still existing.
#
# A test that needs more invasive cleanup (e.g.
# ``test_adopt_pr.sh``'s in-progress-rebase recovery loop with
# branching logic on git state) is welcome to roll its own trap
# instead of registering a hook — the hook API is for one-line
# restorations, not multi-line procedures.
#
# Idempotence
# -----------
# Sourcing this file twice is safe — the registration arrays are
# initialised only on first source (``${_var:-}`` guards) and the
# trap installer flag ``_TEMP_CLEANUP_HELPERS__TRAP_INSTALLED`` is
# checked before re-running ``trap``. This matters when a test
# sources both this helper and another helper that itself sources
# this one.
#
# When the trap is installed
# --------------------------
# At SOURCE time, not lazily on first ``register_*`` call. This
# matches the eager-install behavior of the hand-rolled boilerplate
# the helper replaces, and avoids a footgun where the first
# ``register_*`` happens inside a command-substitution subshell
# (e.g. ``bindir=$(make_stub_bin)``): in that case a lazy installer
# would install the trap in the subshell, the parent shell would
# never see the trap, and registered paths would not be cleaned up.
# Eager install at source time guarantees the parent shell owns the
# trap and the registration arrays.

# ── Module-private state ──────────────────────────────────────────────────
# Initialised on first source. The ``${_var-}`` guards make re-sourcing
# safe (the existing array is preserved). Both arrays are declared as
# empty bare arrays here; consumers register entries via the helpers
# below, and the cleanup function uses the bash-3.2-safe guarded
# iteration form ``${_TEMP_CLEANUP_HELPERS__DIRS[@]+...}`` to avoid the
# empty-array footgun documented in #4336.

if [[ -z "${_TEMP_CLEANUP_HELPERS__SOURCED:-}" ]]; then
    _TEMP_CLEANUP_HELPERS__SOURCED=1
    _TEMP_CLEANUP_HELPERS__DIRS=()
    _TEMP_CLEANUP_HELPERS__FILES=()
    _TEMP_CLEANUP_HELPERS__HOOKS=()
fi

# ── Trap body ─────────────────────────────────────────────────────────────
# Removes every registered directory / file. Runs with ``set +eu`` so a
# failed ``rm`` on one entry does not abort cleanup of the rest.
# Existence checks before ``rm`` avoid spurious errors when a path was
# already removed by the test body (or never created because mktemp
# failed). The ``${arr[@]+...}`` parameter-expansion guard is the
# canonical bash-3.2-safe idiom for iterating a maybe-empty array
# under ``set -u`` — see scripts/check-bash-set-u-empty-array.sh Pass 3.
# shellcheck disable=SC2329  # invoked indirectly via ``trap ... EXIT``.
_temp_cleanup_helpers__cleanup() {
    set +eu
    # Phase 1: run registered hooks in registration order. A hook is
    # a bash function name in the caller's scope; we invoke it via
    # the runtime command resolver so the function lookup happens at
    # trap-fire time rather than registration time. ``|| true`` is
    # belt-and-suspenders — ``set +eu`` already prevents propagation,
    # but the explicit ignore makes the intent loud.
    for hook in ${_TEMP_CLEANUP_HELPERS__HOOKS[@]+"${_TEMP_CLEANUP_HELPERS__HOOKS[@]}"}; do
        if [[ -n "$hook" ]]; then
            "$hook" || true
        fi
    done
    # Phase 2: rm -rf registered directories.
    for d in ${_TEMP_CLEANUP_HELPERS__DIRS[@]+"${_TEMP_CLEANUP_HELPERS__DIRS[@]}"}; do
        if [[ -n "$d" && -d "$d" ]]; then
            rm -rf "$d"
        fi
    done
    # Phase 3: rm -f registered files.
    for f in ${_TEMP_CLEANUP_HELPERS__FILES[@]+"${_TEMP_CLEANUP_HELPERS__FILES[@]}"}; do
        if [[ -n "$f" && -e "$f" ]]; then
            rm -f "$f"
        fi
    done
}

# ── Eager trap installer ──────────────────────────────────────────────────
# Install the EXIT trap right now, at source time, in the shell that is
# sourcing this file (not inside any subshell). See the "When the trap
# is installed" header comment for why eager-install rather than lazy
# is the right behavior for this helper's contract.
trap _temp_cleanup_helpers__cleanup EXIT

# ── Public API ────────────────────────────────────────────────────────────

# register_temp_dir <path>
#   Append <path> to the cleanup list and ensure the EXIT trap is
#   installed. The path will be ``rm -rf``'d on exit if it still exists
#   and is a directory.
register_temp_dir() {
    if [[ $# -ne 1 ]]; then
        echo "register_temp_dir: expected 1 argument, got $#" >&2
        return 2
    fi
    if [[ -z "$1" ]]; then
        echo "register_temp_dir: empty path" >&2
        return 2
    fi
    _TEMP_CLEANUP_HELPERS__DIRS+=("$1")
}

# register_temp_file <path>
#   Append <path> to the cleanup list and ensure the EXIT trap is
#   installed. The path will be ``rm -f``'d on exit if it still
#   exists.
register_temp_file() {
    if [[ $# -ne 1 ]]; then
        echo "register_temp_file: expected 1 argument, got $#" >&2
        return 2
    fi
    if [[ -z "$1" ]]; then
        echo "register_temp_file: empty path" >&2
        return 2
    fi
    _TEMP_CLEANUP_HELPERS__FILES+=("$1")
}

# register_cleanup_hook <function-name>
#   Append <function-name> to the cleanup-hook list and ensure the
#   EXIT trap is installed. Hooks run BEFORE the rm phase so they
#   can rely on registered directories/files still existing.
#
#   Typical use: a test mocks ``$PATH`` and needs to restore it on
#   exit. Save the original PATH at setup time, define a tiny
#   restore function, and register it:
#
#       ORIG_PATH_SAVE="$PATH"
#       restore_path() { export PATH="$ORIG_PATH_SAVE"; }
#       register_cleanup_hook restore_path
#
#   <function-name> must be the NAME of a bash function visible in
#   the caller's scope at trap-fire time. The function is invoked
#   with no arguments under ``set +eu`` so a failing hook does not
#   abort the rest of cleanup.
register_cleanup_hook() {
    if [[ $# -ne 1 ]]; then
        echo "register_cleanup_hook: expected 1 argument, got $#" >&2
        return 2
    fi
    if [[ -z "$1" ]]; then
        echo "register_cleanup_hook: empty hook name" >&2
        return 2
    fi
    _TEMP_CLEANUP_HELPERS__HOOKS+=("$1")
}
