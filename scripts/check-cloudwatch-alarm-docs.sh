#!/usr/bin/env bash
# check-cloudwatch-alarm-docs.sh — Hygiene check that every CloudWatch alarm
# defined in `infra/terraform/modules/**/*.tf` has a corresponding row in
# `docs/agent/infrastructure-reference.md` (the §"CloudWatch Alarms" section).
#
# Why: the docs table is the only place an operator can answer "what alarms
# exist and what do they fire on" without reading every Terraform module. When
# an engineer adds a new `aws_cloudwatch_metric_alarm` resource and forgets the
# docs row, drift accumulates silently. This check fires at PR time so the
# author either documents the alarm or makes an explicit decision to skip it.
#
# Scope:
#   - Inspects `aws_cloudwatch_metric_alarm` resources only. Log metric
#     filters (`aws_cloudwatch_log_metric_filter`) are paired with their
#     consumer alarms in the docs table — they are documented transitively
#     and not separately enforced. See PR (#4119) discussion.
#
# Algorithm:
#   1. Grep every `infra/terraform/modules/**/*.tf` file for
#      `resource "aws_cloudwatch_metric_alarm" "<name>"` blocks.
#   2. For each block, extract the `alarm_name = "<expr>"` line.
#   3. Render `<expr>` by substituting the per-file `locals` references
#      (specifically `${local.service_name}` and `${local.task_family}`,
#      the two locals used as alarm-name building blocks across the repo)
#      and the per-module `${var.name_prefix}` default. `${var.environment}`
#      and `${each.key}` are env- or per-key-specific and are stripped
#      (collapsed dashes), leaving the static "search key" portion.
#   4. The resulting search key (lowercased, stripped of leading/trailing
#      dashes) must appear as a substring of
#      `docs/agent/infrastructure-reference.md`.
#
# Usage:
#   scripts/check-cloudwatch-alarm-docs.sh                       # scan repo root
#   scripts/check-cloudwatch-alarm-docs.sh <root-dir>            # scan specific dir
#   ALARM_DOCS_FILE=<path> scripts/check-cloudwatch-alarm-docs.sh <root-dir>
#                                                                # override docs path
#
# Exit codes:
#   0 — every alarm has a docs row.
#   1 — one or more alarms are missing from the docs.
#   2 — usage error (e.g. scan dir does not exist).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_ROOT="${1:-$REPO_ROOT}"

if [[ ! -d "$SCAN_ROOT" ]]; then
    echo "ERROR: scan root '$SCAN_ROOT' does not exist or is not a directory." >&2
    exit 2
fi

MODULES_DIR="$SCAN_ROOT/infra/terraform/modules"
DOCS_FILE="${ALARM_DOCS_FILE:-$SCAN_ROOT/docs/agent/infrastructure-reference.md}"

# If the scan tree has no Terraform modules dir, there is nothing to check.
if [[ ! -d "$MODULES_DIR" ]]; then
    echo "No infra/terraform/modules/ in '$SCAN_ROOT' — nothing to check."
    exit 0
fi

if [[ ! -f "$DOCS_FILE" ]]; then
    echo "ERROR: docs file '$DOCS_FILE' does not exist." >&2
    echo "Set ALARM_DOCS_FILE or check the layout of the scan tree." >&2
    exit 2
fi

# ─── Helpers ────────────────────────────────────────────────────────────────

# Look up a single-line `<key> = "<value>"` assignment inside a `locals { ... }`
# block (the only locals shape used by the modules in scope). Prints the value
# (without quotes) on stdout, or empty if not found.
local_value_in_file() {
    local file="$1"
    local key="$2"
    # Match: `<key> <whitespace>= "<value>"` (the value is a string literal).
    # The `awk` invocation pulls only the first match per file.
    awk -v key="$key" '
        /^locals[[:space:]]*\{/ { in_block = 1; next }
        in_block && /^\}/ { in_block = 0 }
        in_block {
            # match: key = "..."
            if (match($0, "^[[:space:]]*" key "[[:space:]]*=[[:space:]]*\"[^\"]*\"")) {
                line = substr($0, RSTART, RLENGTH)
                # Extract the quoted value.
                if (match(line, "\"[^\"]*\"")) {
                    val = substr(line, RSTART + 1, RLENGTH - 2)
                    print val
                    exit
                }
            }
        }
    ' "$file"
}

# Look up the default value of a `name_prefix` variable in a module's
# variables.tf file. Returns the default string or empty.
name_prefix_default() {
    local module_dir="$1"
    local vars_file="$module_dir/variables.tf"
    [[ -f "$vars_file" ]] || return 0
    awk '
        /^variable[[:space:]]+"name_prefix"[[:space:]]*\{/ { in_block = 1; next }
        in_block && /^\}/ { in_block = 0 }
        in_block {
            if (match($0, "^[[:space:]]*default[[:space:]]*=[[:space:]]*\"[^\"]*\"")) {
                line = substr($0, RSTART, RLENGTH)
                if (match(line, "\"[^\"]*\"")) {
                    val = substr(line, RSTART + 1, RLENGTH - 2)
                    print val
                    exit
                }
            }
        }
    ' "$vars_file"
}

# Render an alarm_name expression to a stable "search key":
#   - Expand ${local.service_name} and ${local.task_family} (per-file).
#   - Expand ${var.name_prefix} (per-module default).
#   - Replace ${var.environment} and ${each.key} with empty (env-specific
#     or per-key — not part of the documentable prefix).
#   - Collapse runs of '-' to a single '-' and trim leading/trailing '-'.
# Stdout: search key, or empty if any ${...} interpolation remains
#   unresolvable (meaning the script cannot decide and the caller should
#   warn rather than silently pass).
render_search_key() {
    local expr="$1"
    local module_dir="$2"
    local tf_file="$3"

    local svc tf prefix
    svc="$(local_value_in_file "$tf_file" "service_name")"
    tf="$(local_value_in_file "$tf_file" "task_family")"
    prefix="$(name_prefix_default "$module_dir")"

    local rendered="$expr"
    if [[ -n "$svc" ]]; then
        rendered="${rendered//\$\{local.service_name\}/$svc}"
    fi
    if [[ -n "$tf" ]]; then
        rendered="${rendered//\$\{local.task_family\}/$tf}"
    fi
    if [[ -n "$prefix" ]]; then
        rendered="${rendered//\$\{var.name_prefix\}/$prefix}"
    fi

    # Strip env-specific and per-key interpolations.
    rendered="${rendered//\$\{var.environment\}/}"
    rendered="${rendered//\$\{each.key\}/}"

    # Collapse runs of '-' to single '-' and trim leading/trailing '-'.
    # POSIX sed only — no GNU extensions.
    rendered="$(printf '%s' "$rendered" | sed -e 's/--*/-/g' -e 's/^-//' -e 's/-$//')"

    # If unresolved interpolations remain, return empty so the caller can warn.
    if [[ "$rendered" == *"\${"* ]]; then
        return 0
    fi
    printf '%s' "$rendered"
}

# ─── Scan ───────────────────────────────────────────────────────────────────

# Find every Terraform file in modules/ that contains a metric_alarm resource.
# Use `find` (POSIX) rather than globstar (bash 4+).
tf_files=()
while IFS= read -r f; do
    tf_files+=("$f")
done < <(find "$MODULES_DIR" -type f -name '*.tf' -print | sort)

missing=()
unresolved=()

for tf in "${tf_files[@]}"; do
    module_dir="$(dirname "$tf")"

    # Walk each metric_alarm resource block and pull its alarm_name line.
    # `awk` outputs lines of the form:
    #   <resource_name>|<alarm_name_expr>
    # for each alarm in the file (one per resource block).
    while IFS='|' read -r resource_name alarm_expr; do
        [[ -n "$resource_name" ]] || continue
        [[ -n "$alarm_expr" ]] || continue

        key="$(render_search_key "$alarm_expr" "$module_dir" "$tf")"
        rel="${tf#$REPO_ROOT/}"

        if [[ -z "$key" ]]; then
            unresolved+=("$rel: $resource_name (alarm_name=$alarm_expr)")
            continue
        fi

        # Substring search in the docs file. -F = fixed string, -q = quiet.
        if ! grep -Fq -- "$key" "$DOCS_FILE"; then
            missing+=("$rel: $resource_name -> '$key' not found in docs")
        fi
    done < <(awk '
        /^resource[[:space:]]+"aws_cloudwatch_metric_alarm"[[:space:]]+"[^"]+"[[:space:]]*\{/ {
            # Extract the resource name (second quoted string on the line).
            n = split($0, parts, "\"")
            # parts[2] = "aws_cloudwatch_metric_alarm"; parts[4] = resource name.
            current = parts[4]
            in_block = 1
            depth = 1
            next
        }
        in_block {
            # Track brace depth so nested { } in attribute values do not
            # close the resource block prematurely.
            o = gsub(/\{/, "{")
            c = gsub(/\}/, "}")
            depth += (o - c)
            if (match($0, "^[[:space:]]*alarm_name[[:space:]]*=[[:space:]]*\"[^\"]*\"")) {
                line = substr($0, RSTART, RLENGTH)
                if (match(line, "\"[^\"]*\"")) {
                    val = substr(line, RSTART + 1, RLENGTH - 2)
                    print current "|" val
                }
            }
            if (depth <= 0) {
                in_block = 0
                current = ""
            }
        }
    ' "$tf")
done

# ─── Report ────────────────────────────────────────────────────────────────

if [[ ${#unresolved[@]} -gt 0 ]]; then
    echo "WARN: the following alarm_name expressions contain interpolations" >&2
    echo "this check could not resolve. Either expand render_search_key in" >&2
    echo "scripts/check-cloudwatch-alarm-docs.sh, or document the alarm by" >&2
    echo "name and silence by adding the rendered prefix to the docs:" >&2
    for u in "${unresolved[@]}"; do
        echo "  $u" >&2
    done
    echo "" >&2
fi

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: the following CloudWatch alarms have no corresponding row in"
    echo "$DOCS_FILE."
    echo ""
    echo "Add a row to the §\"CloudWatch Alarms\" table with the alarm prefix,"
    echo "module, source metric, and a short \"Fires when\" description."
    echo ""
    for m in "${missing[@]}"; do
        echo "  $m"
    done
    echo ""
    echo "Found ${#missing[@]} missing alarm row(s)."
    exit 1
fi

# Treat unresolved-only as a warning, not a hard fail — the operator may have
# legitimate cause to add a new interpolation form. Surface it loudly so the
# next maintainer can extend render_search_key, but don't block the PR.
echo "All clean — every CloudWatch alarm has a row in $DOCS_FILE."
exit 0
