# _extract_guard_step_names.awk — helper for self-match tests.
#
# Extracts every `name:` value from a GitHub Actions workflow file
# whose step runs a given guard script.  A step is considered to run
# the guard if any line after the step's `- name:` header (until the
# next step header) contains the guard's script path.
#
# Why this exists
# ───────────────
# Guard scripts like scripts/check-no-ecs-wait-services-stable.sh
# forbid a specific string pattern anywhere in the repo.  If the CI
# step that runs the guard quotes the pattern in its `name:` field
# (e.g. `- name: Check for forbidden 'aws ecs wait services-stable'`),
# the step's own name matches the guard's forbidden regex — the guard
# fails on its very first CI run.  This happened on PR #2541 (see
# issue #2542) and the fix was to rename the step.
#
# Peer guard tests use this extractor to pull the step names that
# currently run their guard, write them to a temp file, and re-run
# the guard against that file.  If the step names self-match, the
# assertion fails at test time instead of at CI time.
#
# Usage
# ─────
#   awk -v script="scripts/check-no-ecs-wait-services-stable.sh" \
#       -f scripts/tests/_extract_guard_step_names.awk \
#       .github/workflows/ci.yml
#
# Output: one step name per line (quotes stripped).  Empty output is
# valid — it means no CI step currently runs this guard.
#
# Implementation notes
# ────────────────────
# Line-buffered single-pass: remember the most recent `- name:` seen,
# and when a line mentions the guard path, print that name.  Using
# `current_name` state instead of `getline` lookahead avoids the
# common awk pitfall where a lookahead consumes the next step-header
# line and the main loop never re-processes it.

BEGIN {
    if (script == "") {
        print "ERROR: script variable required (pass with -v script=...)" > "/dev/stderr"
        exit 2
    }
    current_name = ""
    printed_for_current = 0
}

/^[[:space:]]*-[[:space:]]*name:[[:space:]]*/ {
    line = $0
    sub(/^[[:space:]]*-[[:space:]]*name:[[:space:]]*/, "", line)
    # Strip surrounding single or double quotes if present (YAML
    # allows both bare and quoted scalar forms).
    if (length(line) >= 2) {
        first = substr(line, 1, 1)
        last = substr(line, length(line), 1)
        if ((first == "\"" && last == "\"") || (first == "'\''" && last == "'\''")) {
            line = substr(line, 2, length(line) - 2)
        }
    }
    current_name = line
    printed_for_current = 0
    next
}

# Any line that mentions the guard's path counts as "this step runs
# the guard."  Print the current step name once (a step may reference
# the guard on multiple lines, e.g. in a multi-line run block).
{
    if (current_name != "" && printed_for_current == 0 && index($0, script) > 0) {
        print current_name
        printed_for_current = 1
    }
}
