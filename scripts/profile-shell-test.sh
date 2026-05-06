#!/usr/bin/env bash
# profile-shell-test.sh — Wrap a shell test file with per-section timing.
#
# Reads a `.sh` test file, finds all section boundaries (default: lines
# matching `^# Tests? +(T(_issue)?)?N(a-z)?:`, which covers both the
# legacy `# Test 1:` and the post-#3666 `# Test T_issue3656:` /
# `# Test T3675:` / `# Test T3656a:` conventions described in
# `docs/agent/code-standards.md` §Test marker convention), injects
# timing markers around each section, runs the instrumented copy, and
# prints the top-N longest-running sections in sorted order.
#
# Why this exists
# ---------------
# Issue #4139's wall-clock optimization wasted iterations on a wrong
# hypothesis ("parse cost dominates") that a per-section profile would
# have falsified in one tool invocation. The actual cost was unconfigured
# CI_POLL_INTERVAL=60 / DEPLOY_GRACE_SECONDS=90 sleeps in awaiting_ci /
# awaiting_deploy sections — visible in 5 seconds with this profiler,
# invisible without it. See #4176.
#
# Usage
# -----
#   scripts/profile-shell-test.sh <test.sh>
#   scripts/profile-shell-test.sh --section-pattern '^# T[0-9]+:' <test.sh>
#   scripts/profile-shell-test.sh --top 30 --tsv /tmp/sections.tsv <test.sh>
#
# Options
# -------
#   --section-pattern REGEX
#       Extended regex that identifies a section header line.
#       Default: ^#[[:space:]]+Tests?[[:space:]]+(T(_issue)?)?[0-9]+[a-z]?:
#       This covers all four header forms used in scripts/tests/:
#         * `# Test 1:`           (legacy sequential — T44–T59 grandfathered)
#         * `# Test T3675:`       (new convention, bare T<N>)
#         * `# Test T_issue3656:` (new convention, T_issue<N>)
#         * `# Test T3656a:`      (same-issue disambiguation, T<N><a-z>)
#       See `docs/agent/code-standards.md` §Test marker convention. The
#       trailing `[a-z]?` covers same-issue disambiguation; sub-test
#       markers like `# T57a:` (no leading `Test` keyword) are out of
#       scope for the default and require an explicit
#       `--section-pattern '^# T[0-9]+[a-z]?:'` override.
#   --top N
#       Number of slowest sections to print at the end (default 20).
#   --tsv PATH
#       Where to write the per-section TSV. Default: a tempfile under
#       $TMPDIR. The path is always echoed to stderr at the end of the
#       run so the caller can inspect it.
#   --keep
#       Do not delete the instrumented script + TSV on exit.
#       Useful for debugging the instrumented output itself.
#
# Output format
# -------------
# Stdout: every line of the wrapped test's stdout, then on exit:
#   ----- profile-shell-test summary -----
#   Top N longest sections:
#   <elapsed_seconds>\t<section_label>
#   ...
#   ----- end -----
#
# Stderr: a one-line `profile-shell-test: tsv=<path>` pointer.
#
# Exit code: the wrapped test's exit code (preserved verbatim).
#
# bash 3.2 compatibility
# ----------------------
# This script must run on macOS's stock bash 3.2. No mapfile, declare -A,
# nameref, or ${var,,} parameter expansions. See
# docs/agent/code-standards.md §macOS bash 3.2 compatibility.

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────
SECTION_PATTERN='^#[[:space:]]+Tests?[[:space:]]+(T(_issue)?)?[0-9]+[a-z]?:'
TOP_N=20
TSV_PATH=""
KEEP=0
INPUT=""

# ─── Argument parsing ────────────────────────────────────────────────────
usage() {
    sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --section-pattern)
            [[ $# -ge 2 ]] || { echo "ERROR: --section-pattern needs an argument" >&2; exit 2; }
            SECTION_PATTERN="$2"
            shift 2
            ;;
        --top)
            [[ $# -ge 2 ]] || { echo "ERROR: --top needs an argument" >&2; exit 2; }
            TOP_N="$2"
            shift 2
            ;;
        --tsv)
            [[ $# -ge 2 ]] || { echo "ERROR: --tsv needs an argument" >&2; exit 2; }
            TSV_PATH="$2"
            shift 2
            ;;
        --keep)
            KEEP=1
            shift
            ;;
        -h|--help)
            usage 0
            ;;
        --)
            shift
            INPUT="${1:-}"
            shift || true
            break
            ;;
        -*)
            echo "ERROR: unknown option '$1'" >&2
            usage 2
            ;;
        *)
            if [[ -z "$INPUT" ]]; then
                INPUT="$1"
                shift
            else
                echo "ERROR: extra positional argument '$1' (only one input file allowed)" >&2
                usage 2
            fi
            ;;
    esac
done

if [[ -z "$INPUT" ]]; then
    echo "ERROR: missing input shell test file" >&2
    usage 2
fi

if [[ ! -f "$INPUT" ]]; then
    echo "ERROR: input file does not exist: $INPUT" >&2
    exit 2
fi

# ─── Workspace ───────────────────────────────────────────────────────────
WORKDIR=$(mktemp -d -t profile-shell-test.XXXXXX)

if [[ -z "$TSV_PATH" ]]; then
    TSV_PATH="$WORKDIR/sections.tsv"
fi

# The instrumented copy MUST live in the same directory as the original
# input — many test files compute their REPO_ROOT from
# ``$(dirname "${BASH_SOURCE[0]}")/...``, which only resolves correctly
# when the executing script is at the original path. Putting the
# instrumented copy in a tempdir breaks that discovery (#4176 verified
# against scripts/tests/test_agent_runner_entrypoint.sh).
#
# The naming pattern ``.profiled.<pid>.<basename>`` is chosen so:
#   1. The leading ``.`` keeps it out of ``ls`` and out of glob auto-
#      discovery (e.g. scripts/run-scripts-tests.sh's ``*.sh`` glob does
#      not match dotfiles by default).
#   2. The PID disambiguates concurrent runs.
#   3. The original basename suffix preserves the ``.sh`` extension so
#      ``shellcheck`` / editors recognise the file as bash if a run is
#      kept around for inspection.
INPUT_DIR=$(cd "$(dirname "$INPUT")" && pwd)
INPUT_BASE=$(basename "$INPUT")
INSTRUMENTED="$INPUT_DIR/.profiled.$$.$INPUT_BASE"

STATE="$WORKDIR/state"

cleanup() {
    # Always remove the in-place instrumented copy — even with --keep —
    # because it lives next to source-controlled files and would confuse
    # subsequent runs / git status. The TSV and the workdir's instrumented
    # debugging artifacts are still preserved when --keep is set.
    rm -f "$INSTRUMENTED"
    if [[ "$KEEP" -eq 0 ]]; then
        rm -rf "$WORKDIR"
    else
        # Stash a debug copy of the instrumented file inside the workdir
        # before it's removed, so --keep callers can still inspect it.
        # (The copy was made before this trap fires only when KEEP=1.)
        :
    fi
}
trap cleanup EXIT

# ─── Build the instrumentation header ────────────────────────────────────
# The header defines two functions used by the injected calls:
#
#   _section_record "<label>"
#       Closes the currently-open section (if any) by reading its start
#       time + label from the state file, computing elapsed seconds,
#       appending a TSV row, then opens a new section by writing the new
#       start time + label to the state file.
#
#   _section_close
#       Closes the final section. Called once at end-of-script.
#
# State is kept in a regular file ($_PROFILE_STATE) so subshell-scoped
# variables don't lose the start time when section bodies use ( … ). The
# state file holds two lines: the start epoch-ms (line 1) and the section
# label (line 2). Empty/missing means "no section currently open".
#
# Epoch ms is computed via python3 -c. python3 is required on every
# operator laptop and CI runner; if it's somehow missing, the calls fall
# back to second-precision via $SECONDS.

cat > "$WORKDIR/header.sh" <<'PROFILE_HEADER_EOF'
# ─── profile-shell-test.sh injected header ───────────────────────────
_PROFILE_TSV="__TSV_PATH__"
_PROFILE_STATE="__STATE_PATH__"
: > "$_PROFILE_TSV"
: > "$_PROFILE_STATE"

_section_now_ms() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import time; print(int(time.time()*1000))'
    else
        # Fallback: second precision. Multiplied by 1000 for unit consistency.
        printf '%s000\n' "$(date +%s)"
    fi
}

_section_record() {
    local _label="$1"
    local _now
    _now=$(_section_now_ms)
    # Close prior section (if any).
    if [[ -s "$_PROFILE_STATE" ]]; then
        local _t0 _prev_label
        _t0=$(sed -n '1p' "$_PROFILE_STATE")
        _prev_label=$(sed -n '2p' "$_PROFILE_STATE")
        if [[ -n "$_t0" && -n "$_prev_label" ]]; then
            local _elapsed_ms=$(( _now - _t0 ))
            # Format as <int>.<3-digit-ms> seconds, bash 3.2 friendly.
            local _secs=$(( _elapsed_ms / 1000 ))
            local _frac=$(( _elapsed_ms % 1000 ))
            # Zero-pad fractional part to 3 digits.
            local _frac_pad
            if   [[ $_frac -lt 10  ]]; then _frac_pad="00$_frac"
            elif [[ $_frac -lt 100 ]]; then _frac_pad="0$_frac"
            else                            _frac_pad="$_frac"
            fi
            printf '%d.%s\t%s\n' "$_secs" "$_frac_pad" "$_prev_label" >> "$_PROFILE_TSV"
        fi
    fi
    # Open new section.
    printf '%s\n%s\n' "$_now" "$_label" > "$_PROFILE_STATE"
}

_section_close() {
    local _now
    _now=$(_section_now_ms)
    if [[ -s "$_PROFILE_STATE" ]]; then
        local _t0 _prev_label
        _t0=$(sed -n '1p' "$_PROFILE_STATE")
        _prev_label=$(sed -n '2p' "$_PROFILE_STATE")
        if [[ -n "$_t0" && -n "$_prev_label" ]]; then
            local _elapsed_ms=$(( _now - _t0 ))
            local _secs=$(( _elapsed_ms / 1000 ))
            local _frac=$(( _elapsed_ms % 1000 ))
            local _frac_pad
            if   [[ $_frac -lt 10  ]]; then _frac_pad="00$_frac"
            elif [[ $_frac -lt 100 ]]; then _frac_pad="0$_frac"
            else                            _frac_pad="$_frac"
            fi
            printf '%d.%s\t%s\n' "$_secs" "$_frac_pad" "$_prev_label" >> "$_PROFILE_TSV"
        fi
        : > "$_PROFILE_STATE"
    fi
}

# Install an EXIT trap so the final section closes even when the test
# calls `exit` mid-script (a very common pattern). If the test installs
# its own EXIT trap later, our injected `_section_record` call BEFORE
# the offending boundary still records the LAST section's elapsed time,
# so the only data loss is when a test exits inside its very last
# section AND overwrites our trap — that's fine for the dx-tooling
# use case (the long pole is almost never the last section anyway).
trap '_section_close' EXIT
# ─── end injected header ─────────────────────────────────────────────
PROFILE_HEADER_EOF

# Substitute the placeholders. sed -i is BSD/GNU-incompatible; use a
# tempfile + mv instead.
sed \
    -e "s|__TSV_PATH__|${TSV_PATH//|/\\|}|g" \
    -e "s|__STATE_PATH__|${STATE//|/\\|}|g" \
    "$WORKDIR/header.sh" > "$WORKDIR/header.sh.tmp"
mv "$WORKDIR/header.sh.tmp" "$WORKDIR/header.sh"

# ─── Instrument the input ────────────────────────────────────────────────
# Walk the input line by line. When a line matches SECTION_PATTERN, emit
# a _section_record call BEFORE the matched line, using the matched line
# (stripped of the leading "# " and trailing whitespace) as the label.
# Then append a _section_close call at end-of-script.
#
# Key subtlety (#4176 implementation note 3): the header must be inserted
# AFTER the input's shebang (line 1) so the shebang stays at line 1 of
# the instrumented file. If the input has no shebang, header goes at the
# very top.

awk -v pattern="$SECTION_PATTERN" -v header_path="$WORKDIR/header.sh" '
function read_header(   line, out) {
    out = ""
    while ((getline line < header_path) > 0) {
        out = out line "\n"
    }
    close(header_path)
    return out
}
function shell_quote(s,    r) {
    # Wrap in single quotes; embedded single quotes become '\''.
    gsub(/\x27/, "\x27\\\x27\x27", s)
    return "\x27" s "\x27"
}
function emit_label(line,    label) {
    # Strip leading "# " (any amount of whitespace after #) and trailing
    # whitespace. Bash equivalent of: label=${line# *#*( )}; label=${label%% *( )}
    label = line
    sub(/^[[:space:]]*#[[:space:]]*/, "", label)
    sub(/[[:space:]]+$/, "", label)
    if (label == "") {
        label = "(unlabeled)"
    }
    return label
}
function chain_exit_trap(line,    indent, m) {
    # Detect `trap <single-arg> EXIT` (with optional surrounding whitespace
    # and trailing comment) and rewrite the trap command so _section_close
    # runs first. Examples handled:
    #   trap cleanup EXIT
    #   trap "cleanup" EXIT
    #   trap '\''cleanup'\'' EXIT
    # NOT handled (bail out — return the original line unchanged):
    #   trap "do_a; do_b" EXIT     # would lose the chained command if we
    #                                # naively quote
    # For the typical pattern (single function name or a quoted string with
    # no nested quotes), we rewrite by capturing the arg and prepending
    # "_section_close; " to it.
    if (line ~ /^[[:space:]]*trap[[:space:]]+.*[[:space:]]+EXIT[[:space:]]*(#.*)?$/) {
        # Extract leading whitespace.
        indent = line
        sub(/[^[:space:]].*$/, "", indent)
        # Try the three quote forms in turn. We use sub() so we can pull
        # the captured group via match() + substr().
        # Form 1: trap "..." EXIT — double-quoted command.
        if (match(line, /trap[[:space:]]+"[^"]*"[[:space:]]+EXIT/)) {
            cmd_full = substr(line, RSTART, RLENGTH)
            # Strip leading "trap " and trailing " EXIT".
            sub(/^trap[[:space:]]+"/, "", cmd_full)
            sub(/"[[:space:]]+EXIT$/, "", cmd_full)
            return indent "trap \"_section_close; " cmd_full "\" EXIT"
        }
        # Form 2: trap '\''...'\'' EXIT — single-quoted command.
        if (match(line, /trap[[:space:]]+\x27[^\x27]*\x27[[:space:]]+EXIT/)) {
            cmd_full = substr(line, RSTART, RLENGTH)
            sub(/^trap[[:space:]]+\x27/, "", cmd_full)
            sub(/\x27[[:space:]]+EXIT$/, "", cmd_full)
            return indent "trap \x27_section_close; " cmd_full "\x27 EXIT"
        }
        # Form 3: trap NAME EXIT — bare function/command name.
        if (match(line, /trap[[:space:]]+[A-Za-z_][A-Za-z0-9_]*[[:space:]]+EXIT/)) {
            cmd_full = substr(line, RSTART, RLENGTH)
            sub(/^trap[[:space:]]+/, "", cmd_full)
            sub(/[[:space:]]+EXIT$/, "", cmd_full)
            return indent "trap \x27_section_close; " cmd_full "\x27 EXIT"
        }
        # Otherwise leave the line untouched. The header-installed
        # trap is still in place if no other trap was registered.
    }
    return line
}
BEGIN {
    header_inserted = 0
    line_no = 0
}
{
    line_no++
    # Insert header after the shebang (line 1) or at the very top if no
    # shebang. If line 1 is blank or not a shebang, treat it as no shebang.
    if (header_inserted == 0) {
        if (line_no == 1) {
            if ($0 ~ /^#!/) {
                # Shebang — print as-is, defer header to after this line.
                print $0
                next
            } else {
                # No shebang — emit header first, then this line.
                printf "%s", read_header()
                header_inserted = 1
                # Fall through to print this line below.
            }
        } else if (line_no == 2) {
            # We deferred header insertion past the shebang. Insert it now.
            printf "%s", read_header()
            header_inserted = 1
            # Fall through to emit this line below.
        }
    }

    # Rewrite trap … EXIT lines so they chain _section_close.
    out_line = chain_exit_trap($0)

    # Section detection — does THIS line match the section header pattern?
    if (match($0, pattern) > 0) {
        label = emit_label($0)
        printf "_section_record %s\n", shell_quote(label)
    }
    print out_line
}
END {
    # If the input is empty or only had a shebang, ensure header is in.
    if (header_inserted == 0) {
        printf "%s", read_header()
    }
}
' "$INPUT" > "$INSTRUMENTED"

chmod +x "$INSTRUMENTED"

# When --keep is set, stash a debug copy in the workdir before the EXIT
# trap removes the in-place instrumented file. The debug copy is what
# users inspect when iterating on the section pattern.
if [[ "$KEEP" -eq 1 ]]; then
    cp "$INSTRUMENTED" "$WORKDIR/instrumented.sh"
fi

# ─── Run the instrumented script ─────────────────────────────────────────
# Preserve the input's exit code. set -e is in force, so use a guard.
set +e
"$INSTRUMENTED"
EXIT_CODE=$?
set -e

# ─── Render summary ──────────────────────────────────────────────────────
echo ""
echo "----- profile-shell-test summary -----"
if [[ -s "$TSV_PATH" ]]; then
    SECTION_COUNT=$(wc -l < "$TSV_PATH" | tr -d ' ')
    echo "Sections recorded: $SECTION_COUNT"
    echo "Top $TOP_N longest sections:"
    # Sort by first column (elapsed_seconds) numerically, descending.
    sort -t '	' -k1,1 -n -r "$TSV_PATH" | head -n "$TOP_N"
else
    echo "Sections recorded: 0 (no sections matched pattern: $SECTION_PATTERN)"
fi
echo "----- end -----"
echo "profile-shell-test: tsv=$TSV_PATH" >&2

exit "$EXIT_CODE"
