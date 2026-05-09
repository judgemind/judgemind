#!/usr/bin/env bash
# run-ci-guards.sh — Umbrella runner for every scripts/check-*.{sh,py} guard.
#
# permanent: true
#
# Discovers every executable matching ``scripts/check-*.sh`` and
# ``scripts/check-*.py`` and runs them in alphabetical order against the
# local working tree. Wired into ``.githooks/pre-push`` so guard failures
# surface locally before the CI round trip.
#
# Why
# ───
# CI runs ~30 ``check-*`` guards as separate workflow jobs (see
# ``.github/workflows/ci.yml``). Pre-#4332, pre-push only ran a hand-picked
# subset (markdown links, schema drift, dispatcher-image deps, etc.); the
# remaining guards only ran in CI, which means a one-line lint nit costs a
# full ~3-minute CI round trip to discover. PR #4325 (closing #4321) hit
# exactly this footgun on ``sql-column-check`` — see issue #4332 for the
# motivating retro.
#
# This runner closes the gap. It is self-maintaining: dropping a new
# ``scripts/check-foo.sh`` into the tree makes it pick up automatically,
# no umbrella-script edit required.
#
# Discovery rules
# ───────────────
# 1. Glob ``scripts/check-*.sh`` and ``scripts/check-*.py``.
# 2. Skip the umbrella itself and any script in the SKIP_LIST below.
# 3. When both ``check-foo.sh`` AND ``check-foo.py`` exist, run only the
#    ``.sh`` (it is the canonical wrapper; the ``.py`` is its
#    implementation). Same logic as the ``.github/workflows/ci.yml``
#    invocations — they call the ``.sh`` not the ``.py``.
# 4. A guard can opt out by adding ``# ci-guards: skip`` within the first
#    20 lines of the file. Use this for guards that depend on network /
#    issue context / Docker / npm-installed deps.
#
# SKIP_LIST entries (built-in, no per-file marker required) — these are
# guards that are not amenable to running blind from the local tree:
#
#   * check-issue-author.sh        — needs an issue number argument
#   * check-duplicate-pr.sh        — needs an issue number argument
#   * check-shipped-pr.sh          — needs an issue number argument
#   * check-issue-plan-blocked.sh  — needs an issue number argument
#   * check-near-duplicate-issue.sh — needs an issue number argument
#   * check-blocked-issues.sh      — scans live GitHub issues
#   * check-task-recovery.sh       — needs a worktree path argument
#   * check-pr-title.sh            — needs a PR number / title input
#   * check-issue-verify-sql.py    — needs ``--issue N`` or ``--body-file
#                                    PATH``; blind invocation exits 2 with
#                                    "one of the arguments --issue
#                                    --body-file is required" (#4372)
#   * check-issue-verify-test-filename.py
#                                  — same shape as check-issue-verify-sql.py;
#                                    needs ``--issue N`` or ``--body-file
#                                    PATH``; blind invocation exits 2 (#4549)
#   * check-graphql-queries.sh     — requires ``packages/web`` npm install
#   * check-migration-number-collision.{sh,py}
#                                  — requires ``gh pr list`` for cross-PR
#                                    diff and a ``--base`` argument
#   * check_schema_drift.sh        — already wired in pre-push, requires
#                                    Docker. Filename uses underscores so
#                                    it would not match this glob anyway,
#                                    listed defensively.
#   * check-diff-coverage.sh       — interactive tooling helper that takes
#                                    a positional ``<package>`` argument;
#                                    not a CI guard.
#   * check-scraper-zero-record-runner.py
#   * check-scraper-zero-record-streak.py
#   * check-short-unsubstantive-rulings.py
#                                  — scheduled-cron data-quality scripts
#                                    that run inside ECS against the dev
#                                    DB (``import psycopg`` + DATABASE_URL).
#                                    Wired by deploy-scraper.yml /
#                                    scraper-zero-record-check.yml /
#                                    short-unsubstantive-ruling-check.yml,
#                                    not by ci.yml.
#
# Usage
# ─────
#
#   scripts/run-ci-guards.sh                    # run every applicable guard
#   scripts/run-ci-guards.sh --list             # print the discovered guard list and exit
#   SKIP_CI_GUARDS=1 scripts/run-ci-guards.sh   # bypass; emit a warning to stderr
#
# Exit codes
# ──────────
#
#   0 — every applicable guard passed (or SKIP_CI_GUARDS=1 was set).
#   1 — one or more guards failed.
#   2 — usage / discovery error (e.g. invoked from outside the repo).
#
# Requires-argument hint (#4534)
# ──────────────────────────────
# When a guard fails with what looks like a "rejected blind invocation
# because it needs arguments" pattern (exit 2 + stderr containing
# "requires X argument" / "the following arguments are required" /
# "one of the arguments ... is required" / `${1:?}`'s "parameter null
# or not set"), the failure summary appends a copy-pasteable Fix block
# naming the SKIP_LIST as the remedy. The proactive guard
# `scripts/check-ci-guards-skip-list-coverage.sh` (#11a) catches most
# of this class at hygiene-check time; the runtime hint is a backstop
# for guards that ship before their SKIP_LIST entry lands or use
# argument-shapes the meta-check does not yet detect.

set -uo pipefail

# ────────────────────────────────────────────────────────────────────
# SKIP_CI_GUARDS=1 bypass — emit a warning, exit 0
# ────────────────────────────────────────────────────────────────────
if [ "${SKIP_CI_GUARDS:-0}" = "1" ]; then
    echo "WARNING: SKIP_CI_GUARDS=1 — bypassing scripts/run-ci-guards.sh." >&2
    echo "         CI will still run every guard. Use only when you" >&2
    echo "         intentionally want to push partial work knowing one" >&2
    echo "         or more guards will fail." >&2
    exit 0
fi

# ────────────────────────────────────────────────────────────────────
# Resolve the repo root from the script's own location
# ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts"
SELF_BASENAME="$(basename "${BASH_SOURCE[0]}")"

if [ ! -d "$SCRIPTS_DIR" ]; then
    echo "ERROR: scripts/ directory not found under repo root $REPO_ROOT" >&2
    exit 2
fi

# ────────────────────────────────────────────────────────────────────
# Built-in skip list
# ────────────────────────────────────────────────────────────────────
# Scripts that cannot be run blind from the local tree are skipped here
# rather than by per-file marker so we don't have to touch every guard
# file. Match by basename — both .sh and .py variants are skipped if
# named here.
SKIP_LIST=(
    "check-issue-author.sh"
    "check-duplicate-pr.sh"
    "check-shipped-pr.sh"
    "check-issue-plan-blocked.sh"
    "check-near-duplicate-issue.sh"
    "check-blocked-issues.sh"
    "check-task-recovery.sh"
    "check-pr-title.sh"
    # check-issue-verify-sql.py needs --issue N or --body-file PATH;
    # blind invocation exits 2 with "one of the arguments --issue
    # --body-file is required" (#4372).
    "check-issue-verify-sql.py"
    # check-issue-verify-test-filename.py mirrors the sibling above —
    # needs --issue N or --body-file PATH; blind invocation exits 2
    # (#4549).
    "check-issue-verify-test-filename.py"
    "check-graphql-queries.sh"
    "check-migration-number-collision.sh"
    "check-migration-number-collision.py"
    "check-diff-coverage.sh"
    "check-scraper-zero-record-runner.py"
    "check-scraper-zero-record-streak.py"
    "check-short-unsubstantive-rulings.py"
    # check-fix-block-coverage.py needs one of --check / --regenerate /
    # --print as a required mutually-exclusive argument; blind invocation
    # exits 2 with "one of the arguments --check --regenerate --print is
    # required". The CI step runs it explicitly as `--check`.
    "check-fix-block-coverage.py"
)

# is_in_skip_list <basename> -> 0 if skipped, 1 otherwise
is_in_skip_list() {
    local name="$1"
    local entry
    for entry in "${SKIP_LIST[@]}"; do
        [ "$name" = "$entry" ] && return 0
    done
    return 1
}

# has_opt_out_marker <path> -> 0 if "# ci-guards: skip" appears in
# the first 20 lines, 1 otherwise.
has_opt_out_marker() {
    local path="$1"
    head -n 20 "$path" 2>/dev/null | grep -qE '^[[:space:]]*#[[:space:]]*ci-guards:[[:space:]]*skip[[:space:]]*$'
}

# ────────────────────────────────────────────────────────────────────
# Discover guards
# ────────────────────────────────────────────────────────────────────
# Glob alphabetically into a sorted list. We prefer `find` over a bare
# glob so non-existent matches don't trip set -u. Sort is locale-stable
# (LC_ALL=C) so the order is deterministic across machines.
shopt -s nullglob

discovered=()
while IFS= read -r path; do
    [ -n "$path" ] && discovered+=("$path")
done < <(LC_ALL=C find "$SCRIPTS_DIR" -maxdepth 1 -type f \
            \( -name 'check-*.sh' -o -name 'check-*.py' \) | LC_ALL=C sort)

if [ "${#discovered[@]}" -eq 0 ]; then
    echo "ERROR: no scripts/check-*.{sh,py} guards discovered under $SCRIPTS_DIR" >&2
    exit 2
fi

# Build the list of basenames present so we can de-duplicate .sh/.py pairs.
declare -a basenames_present=()
for path in "${discovered[@]}"; do
    basenames_present+=("$(basename "$path")")
done

# basename_present <basename> -> 0 if present, 1 otherwise
basename_present() {
    local name="$1"
    local entry
    for entry in "${basenames_present[@]}"; do
        [ "$entry" = "$name" ] && return 0
    done
    return 1
}

# ────────────────────────────────────────────────────────────────────
# Filter discovered guards into the runnable list
# ────────────────────────────────────────────────────────────────────
runnable=()
skipped=()
for path in "${discovered[@]}"; do
    name="$(basename "$path")"

    # Skip the umbrella itself defensively (we are already a check-*
    # naming-pattern miss because the umbrella is "run-ci-guards", but
    # leave the guard for any future renaming).
    if [ "$name" = "$SELF_BASENAME" ]; then
        skipped+=("$name (umbrella self)")
        continue
    fi

    # Built-in skip list
    if is_in_skip_list "$name"; then
        skipped+=("$name (built-in skip)")
        continue
    fi

    # Per-file opt-out marker
    if has_opt_out_marker "$path"; then
        skipped+=("$name (# ci-guards: skip)")
        continue
    fi

    # When both check-foo.sh and check-foo.py exist, run only the .sh.
    # The .py is the implementation; the .sh is the canonical wrapper.
    case "$name" in
        check-*.py)
            stem="${name%.py}"
            sh_companion="${stem}.sh"
            if basename_present "$sh_companion"; then
                skipped+=("$name (.sh wrapper companion: $sh_companion)")
                continue
            fi
            ;;
    esac

    # Executability check: .sh files MUST be executable (we run them
    # directly). .py files do not — CI invokes them via ``python3 …``
    # so a missing +x bit is the CI-canonical state for several guards
    # (e.g. check-sql-columns.py at PR #4325 retro time was -rw-r--r--).
    case "$name" in
        check-*.py)
            ;;  # always runnable via python3 below
        *)
            if [ ! -x "$path" ]; then
                skipped+=("$name (not executable)")
                continue
            fi
            ;;
    esac

    runnable+=("$path")
done

# ────────────────────────────────────────────────────────────────────
# --list mode: print the discovered/runnable list and exit 0
# ────────────────────────────────────────────────────────────────────
if [ "${1:-}" = "--list" ]; then
    echo "Discovered: ${#discovered[@]} guard(s)"
    echo "Runnable:   ${#runnable[@]} guard(s)"
    # Empty-array iteration under bash 3.2 + set -u trips
    # ``unbound variable`` on ``"${arr[@]}"`` even when the array is
    # initialised as ``arr=()``.  Guard with a length check so the
    # umbrella is correct on macOS operator laptops too. Same root
    # cause class as ``scripts/check-bash-set-u-empty-array.sh`` —
    # but we declare-and-init so that check itself does not fire.
    if [ "${#runnable[@]}" -gt 0 ]; then
        for path in "${runnable[@]}"; do
            echo "  RUN  $(basename "$path")"
        done
    fi
    if [ "${#skipped[@]}" -gt 0 ]; then
        echo "Skipped:    ${#skipped[@]} guard(s)"
        for entry in "${skipped[@]}"; do
            echo "  SKIP $entry"
        done
    fi
    exit 0
fi

# ────────────────────────────────────────────────────────────────────
# Run the guards
# ────────────────────────────────────────────────────────────────────
echo "run-ci-guards: running ${#runnable[@]} guard(s) (${#skipped[@]} skipped)..." >&2

failures=0
failed_names=()
# Names of failures that look like "requires-argument" — we surface a
# dedicated SKIP_LIST Fix block in the summary so operators don't have to
# grep this script to discover the remedy. See is_requires_argument_failure
# below for the heuristic.
requires_arg_failures=()

# Empty-runnable guard: under bash 3.2 + set -u, iterating
# ``"${runnable[@]}"`` when the array is empty trips ``unbound
# variable`` even when initialised as ``runnable=()``. Skip the loop
# entirely in that case — there's nothing to run.
if [ "${#runnable[@]}" -eq 0 ]; then
    echo "run-ci-guards: no runnable guard(s); nothing to do." >&2
    exit 0
fi

# is_requires_argument_failure <log_file> <rc> -> 0 if the failure looks
# like the guard rejected a blind invocation because it requires an
# argument the umbrella cannot supply, 1 otherwise.
#
# The proactive guard `scripts/check-ci-guards-skip-list-coverage.sh`
# (#11a) catches most of this class at hygiene-check time by AST-walking
# argparse / `${1:?}` shapes — but a brand-new guard pushed before its
# row lands, or an exotic argparse shape the meta-check does not yet
# detect, can still slip through to umbrella-runtime. When that happens,
# the operator sees "FAILED: scripts/check-foo.sh (exit 2)" and has to
# read this script to find SKIP_LIST. The Fix block in the summary saves
# them that round-trip.
#
# Heuristic: rc must be 2 (the conventional argparse-style usage exit
# code, also `${1:?}`'s exit code), AND the captured stderr must contain
# one of the known "requires X" or "is required" patterns. We require
# both signals together so a guard that legitimately exits 2 with an
# unrelated message (e.g. a real source-code violation) does not mis-fire
# the SKIP_LIST hint.
is_requires_argument_failure() {
    local log_file="$1"
    local rc="$2"
    [ "$rc" -eq 2 ] || return 1
    # Match argparse "the following arguments are required:", custom
    # "requires an issue number argument" / "requires a <X> argument",
    # bash "${1:?usage}" parameter-substitution emission, and
    # mutually-exclusive-group "one of the arguments ... is required".
    grep -qiE \
        -e 'requires (an? )?[a-z_-]+ (number )?argument' \
        -e 'the following arguments are required' \
        -e 'one of the arguments .* is required' \
        -e ': parameter (null or )?not set' \
        -e ': usage:' \
        "$log_file" 2>/dev/null
}

for path in "${runnable[@]}"; do
    name="$(basename "$path")"
    log_file="${TMPDIR:-/tmp}/run-ci-guards-${name//\//_}.log"

    # cd to repo root so guards using $(pwd) or relative paths see the
    # expected working tree, matching how CI invokes them from
    # `actions/checkout` at the repo root.
    #
    # .py files are invoked via python3 explicitly so guards that ship
    # without a +x bit (the CI-canonical state for check-sql-columns.py
    # and friends — CI calls them as ``python3 scripts/check-foo.py``)
    # still run from this umbrella.
    rc=0
    case "$name" in
        check-*.py)
            (cd "$REPO_ROOT" && python3 "$path") > "$log_file" 2>&1 || rc=$?
            ;;
        *)
            (cd "$REPO_ROOT" && "$path") > "$log_file" 2>&1 || rc=$?
            ;;
    esac

    if [ "$rc" -ne 0 ]; then
        failures=$((failures + 1))
        failed_names+=("$name")
        if is_requires_argument_failure "$log_file" "$rc"; then
            requires_arg_failures+=("$name")
        fi
        echo "" >&2
        echo "  FAILED: $name (exit $rc)" >&2
        echo "  Last 20 lines of output:" >&2
        tail -n 20 "$log_file" | sed 's/^/    /' >&2
        echo "  Full log: $log_file" >&2
    fi
done

# ────────────────────────────────────────────────────────────────────
# Report
# ────────────────────────────────────────────────────────────────────
if [ "$failures" -gt 0 ]; then
    echo "" >&2
    echo "════════════════════════════════════════════════════════════════════" >&2
    echo "run-ci-guards: $failures of ${#runnable[@]} guard(s) failed:" >&2
    # Length-guard the iteration — see #4479. ``failures > 0``
    # implies failed_names was populated, but the static check
    # treats branch-conditional ``+=`` as non-binding.
    if [ "${#failed_names[@]}" -gt 0 ]; then
        for fname in "${failed_names[@]}"; do
            echo "  - $fname" >&2
        done
    fi
    # ────────────────────────────────────────────────────────────────
    # Requires-argument SKIP_LIST hint (#4534)
    # ────────────────────────────────────────────────────────────────
    # When a failure looks like "guard rejected blind invocation
    # because it needs arguments the umbrella cannot supply", surface
    # the SKIP_LIST remedy as a copy-pasteable Fix block. Same Fix-block
    # contract as the rest of the hygiene-guard fleet
    # (docs/dx/check-script-fix-block-coverage.md / #4346).
    if [ "${#requires_arg_failures[@]}" -gt 0 ]; then
        echo "" >&2
        echo "Fix: the guard(s) below appear to require an argument that the" >&2
        echo "umbrella cannot supply blind. Add each one to SKIP_LIST in" >&2
        echo "scripts/run-ci-guards.sh (alphabetical order), and add a row" >&2
        echo "in docs/dx/check-script-fix-block-coverage.md naming the" >&2
        echo "verdict (typically \"decision flow (no violation list)\")." >&2
        echo "" >&2
        echo "  SKIP_LIST=(" >&2
        echo "      \"check-issue-author.sh\"" >&2
        echo "      ..." >&2
        for fname in "${requires_arg_failures[@]}"; do
            echo "      \"$fname\"   # <-- insert here, alphabetical order" >&2
        done
        echo "  )" >&2
        echo "" >&2
        echo "If the guard is genuinely runnable blind from the local tree" >&2
        echo "and the failure is a real violation, ignore this hint and fix" >&2
        echo "the underlying issue instead. The hint fires on argparse-style" >&2
        echo "exit-2 + \"requires/required\" stderr; a true violation that" >&2
        echo "happens to use exit 2 will be a false positive." >&2
    fi
    echo "" >&2
    echo "Fix the issues above and re-run.  These are the same guards CI" >&2
    echo "runs — catching them locally saves a full CI round trip." >&2
    echo "" >&2
    echo "To bypass (with CI still running every guard):" >&2
    echo "  SKIP_CI_GUARDS=1 git push" >&2
    echo "════════════════════════════════════════════════════════════════════" >&2
    exit 1
fi

echo "run-ci-guards: all ${#runnable[@]} guard(s) passed." >&2
exit 0
