#!/usr/bin/env bash
# check-hyphen-underscore-collision.sh — Flag filename pairs in the same
# directory that differ only by `-` vs `_`.
#
# Motivating incident (#2751 / #2754):
#   `scripts/check-schema-drift.sh` (hyphen, broken grep-based checker)
#   coexisted with `scripts/check_schema_drift.sh` (underscore, correct
#   implementation used by CI + pre-push).  An agent ran the hyphen one
#   first (alphabetical), got a false-positive drift report, and burned
#   diagnostic time before realising the two are different scripts.
#   #2754 deleted the broken one; this check prevents a recurrence.
#
# For each directory in the scan set we list its files, normalise `-`
# to `_`, and report any normalised name claimed by more than one
# distinct original filename.
#
# Scan set (narrow on purpose — widen only if a real collision shows up
# outside it):
#   scripts/                          (top-level files only)
#   packages/*/src/**                 (recursive, per directory)
#   packages/*/tests/**               (recursive, per directory)
#   infra/**                          (recursive, per directory)
#
# Directories that are pure vendored state (.git, .venv, node_modules,
# .next, __pycache__, .terraform, tmp) are skipped even if they appear
# inside the scan set.
#
# Usage:
#   scripts/check-hyphen-underscore-collision.sh          # scan repo root
#   scripts/check-hyphen-underscore-collision.sh [dir]    # scan another root
#
# Exit codes:
#   0 — No collisions found.
#   1 — One or more directories contain a hyphen-vs-underscore pair.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_ROOT="${1:-$REPO_ROOT}"

if [[ ! -d "$SCAN_ROOT" ]]; then
    echo "ERROR: scan root is not a directory: $SCAN_ROOT" >&2
    exit 2
fi

# ─── Directories to scan (relative to SCAN_ROOT) ─────────────────────────
# `scripts/` is scanned non-recursively — its subdirectories (tests/,
# spotcheck/, eval/, dispatcher/) have their own conventions and it's
# the top-level entry points that incident #2751 was about.
#
# packages/<pkg>/src and packages/<pkg>/tests are scanned recursively
# because Python imports and pytest discovery treat `foo_bar.py` and
# `foo-bar.py` as two different files (the hyphenated one can't even
# be imported).  Similar reasoning for TypeScript module resolution.
#
# infra/ is scanned recursively because Terraform module paths are
# resolved literally — a hyphen/underscore pair in the same dir is a
# latent footgun.
SCAN_DIRS_NONRECURSIVE=(
    "scripts"
)

SCAN_DIRS_RECURSIVE=(
    "packages"
    "infra"
)

# ─── Directories to exclude from recursion ───────────────────────────────
# Standard ignored directories (vendored deps, VCS metadata, build output,
# local state).  A directory component anywhere in the path matching one
# of these skips the whole subtree.
EXCLUDE_DIRS=(
    ".git"
    ".venv"
    "node_modules"
    ".next"
    "__pycache__"
    ".vite"
    ".terraform"
    "tmp"
    "dist"
    "build"
    "coverage"
)

# Return 0 if the path has an excluded directory component.
path_is_excluded() {
    local p="$1"
    local excl
    for excl in "${EXCLUDE_DIRS[@]}"; do
        if [[ "$p" == *"/$excl/"* || "$p" == *"/$excl" ]]; then
            return 0
        fi
    done
    return 1
}

# ─── Helper: check a single directory (non-recursive) ───────────────────
# Prints every collision found on stdout in the format:
#     <abs_dir>\t<name_a>\t<name_b>
# and increments the global COLLISION_COUNT for each pair.
COLLISION_COUNT=0

check_directory() {
    local dir="$1"

    # List immediate children only, files only.  We print full paths and
    # strip the directory prefix in awk rather than using `find -printf`,
    # which is GNU-findutils-only (not available on macOS BSD find).
    local files
    files=$(find "$dir" -maxdepth 1 -mindepth 1 -type f 2>/dev/null \
        | awk -v prefix="$dir/" 'index($0, prefix) == 1 { print substr($0, length(prefix) + 1) }' \
        | LC_ALL=C sort || true)
    [[ -z "$files" ]] && return 0

    # Build associative array: normalised name → newline-separated list
    # of original names.  We can't rely on Bash 4 associative arrays on
    # every platform (older macOS Bash is 3.2), so use a plain name-based
    # tempfile approach via awk.
    echo "$files" | awk -v dir="$dir" '
        {
            orig = $0
            norm = orig
            gsub(/-/, "_", norm)
            # Append orig to the list for norm, separated by NUL.
            if (seen[norm]) {
                group[norm] = group[norm] "\n" orig
            } else {
                group[norm] = orig
                seen[norm] = 1
            }
            count[norm]++
        }
        END {
            for (norm in count) {
                if (count[norm] < 2) continue
                # Build a de-duplicated sorted list of originals for this
                # norm (a single real file can appear once; two different
                # originals means collision).
                n = split(group[norm], arr, "\n")
                # Deduplicate (shouldn\u2019t happen on a real filesystem but be
                # defensive).
                delete uniq
                out_n = 0
                for (i = 1; i <= n; i++) {
                    if (!(arr[i] in uniq)) {
                        uniq[arr[i]] = 1
                        out_n++
                        out[out_n] = arr[i]
                    }
                }
                if (out_n < 2) continue
                # Sort the output names lexicographically for stable output.
                for (i = 1; i <= out_n; i++) {
                    for (j = i + 1; j <= out_n; j++) {
                        if (out[i] > out[j]) {
                            tmp = out[i]; out[i] = out[j]; out[j] = tmp
                        }
                    }
                }
                # Emit one line per pair (i, j) for i < j.
                for (i = 1; i <= out_n; i++) {
                    for (j = i + 1; j <= out_n; j++) {
                        printf "%s\t%s\t%s\n", dir, out[i], out[j]
                    }
                }
            }
        }
    '
}

# ─── Scan ────────────────────────────────────────────────────────────────

all_collisions=""

# Non-recursive scan dirs: check just the dir itself.
for rel in "${SCAN_DIRS_NONRECURSIVE[@]}"; do
    abs="$SCAN_ROOT/$rel"
    [[ -d "$abs" ]] || continue
    result=$(check_directory "$abs")
    if [[ -n "$result" ]]; then
        all_collisions+="$result"$'\n'
    fi
done

# Recursive scan dirs: enumerate every subdirectory under them (respecting
# EXCLUDE_DIRS) and check each one.
for rel in "${SCAN_DIRS_RECURSIVE[@]}"; do
    abs="$SCAN_ROOT/$rel"
    [[ -d "$abs" ]] || continue

    # Build a find invocation that prunes excluded dirs.
    prune_args=()
    for excl in "${EXCLUDE_DIRS[@]}"; do
        if [[ ${#prune_args[@]} -gt 0 ]]; then
            prune_args+=( -o )
        fi
        prune_args+=( -name "$excl" )
    done

    # `find -type d` enumerates every subdir including the scan root.
    while IFS= read -r subdir; do
        [[ -z "$subdir" ]] && continue
        result=$(check_directory "$subdir")
        if [[ -n "$result" ]]; then
            all_collisions+="$result"$'\n'
        fi
    done < <(
        find "$abs" \( "${prune_args[@]}" \) -prune -o -type d -print 2>/dev/null | LC_ALL=C sort
    )
done

# Strip trailing blank lines.
all_collisions="${all_collisions%$'\n'}"

if [[ -n "$all_collisions" ]]; then
    echo "ERROR: Found hyphen-vs-underscore filename collisions." >&2
    echo "" >&2
    echo "  Two files in the same directory whose names differ only by" >&2
    echo "  '-' vs '_' are ambiguous — agents and humans routinely pick" >&2
    echo "  the wrong one (see #2751 / #2754).  Rename one, or delete" >&2
    echo "  the stale sibling." >&2
    echo "" >&2
    # Per-pair Fix block (#4346). Pick a canonical winner from the
    # file's extension: Python (.py), Terraform (.tf, .tfvars), and TOML
    # (.toml) prefer underscores; shell (.sh, .bash), markdown (.md),
    # YAML (.yml, .yaml), and JSON (.json) prefer hyphens. Emit a literal
    # `git rm` recipe + a grep call to find downstream importers.
    fix_blocks=""
    while IFS=$'\t' read -r dir a b; do
        [[ -z "$dir" ]] && continue
        COLLISION_COUNT=$((COLLISION_COUNT + 1))
        echo "    $dir/$a" >&2
        echo "    $dir/$b" >&2
        echo "" >&2

        ext_a="${a##*.}"
        # Identify the underscore form vs the hyphen form. The pair
        # always differs only by '-' vs '_', so swapping all '_' to '-'
        # in one yields the other.
        underscore_form=""
        hyphen_form=""
        if [[ "$a" == *"_"* && "${a//_/-}" == "$b" ]]; then
            underscore_form="$a"
            hyphen_form="$b"
        elif [[ "$b" == *"_"* && "${b//_/-}" == "$a" ]]; then
            underscore_form="$b"
            hyphen_form="$a"
        fi

        keep=""
        drop=""
        case "$ext_a" in
            py|tf|tfvars|toml)
                keep="$underscore_form"
                drop="$hyphen_form"
                ;;
            sh|bash|md|yml|yaml|json)
                keep="$hyphen_form"
                drop="$underscore_form"
                ;;
            *)
                # Unknown extension — leave the choice to the operator.
                ;;
        esac

        fix_blocks+="  Fix for collision in '$dir':"$'\n'
        if [[ -n "$keep" && -n "$drop" ]]; then
            fix_blocks+="    Convention for *.$ext_a prefers '$keep'. To collapse:"$'\n'
            fix_blocks+="      git rm '$dir/$drop'  # if confirmed-duplicate"$'\n'
            fix_blocks+="    Then find any importers / callers of the dropped name:"$'\n'
            fix_blocks+="      grep -rn '${drop%.*}' . --include='*.py' --include='*.sh' --include='*.tf'"$'\n'
        else
            fix_blocks+="    Pick a winner ('$a' or '$b') and run:"$'\n'
            fix_blocks+="      git rm '$dir/<loser>'"$'\n'
            fix_blocks+="    Then update every importer / caller of the deleted name."$'\n'
        fi
        fix_blocks+=$'\n'
    done <<< "$all_collisions"
    echo "  Found $COLLISION_COUNT collision pair(s)." >&2
    echo "" >&2
    echo "Fix:" >&2
    printf '%s' "$fix_blocks" >&2
    exit 1
fi

echo "All clean — no hyphen-vs-underscore filename collisions detected."
exit 0
