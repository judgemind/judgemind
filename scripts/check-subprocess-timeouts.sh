#!/usr/bin/env bash
# check-subprocess-timeouts.sh — Every ``subprocess.run(...)`` and
# ``urllib.request.urlopen(...)`` call site in ``scripts/**/*.py``
# (excluding any path with a ``tests/`` segment) must have either a
# ``timeout=`` keyword argument OR a ``**kwargs`` splat (treated as a
# transparent pass-through that delegates timeout responsibility to the
# caller).
#
# Why this check exists
# ---------------------
# An unbounded network egress call on the MainThread of a long-lived process
# can wedge the process silently forever — confirmed as the prime hypothesis
# for the #3205 dispatcher silent-wedge class. Issue #3213 audited all
# scripts/*.py subprocess.run call sites and fixed the 13 violations; issue
# #3309 extended the rule to urllib.request.urlopen calls. This check
# prevents the invariant from regressing for both call shapes.
#
# Rule
# ----
# Every ``subprocess.run(...)`` or ``urllib.request.urlopen(...)`` call in
# ``scripts/**/*.py`` (excluding ``scripts/**/tests/**``) must pass:
#   (a) ``timeout=<value>`` as an explicit keyword argument, OR
#   (b) ``**kwargs`` (``kw.arg is None``) as a keyword splat — the wrapper
#       itself is expected to set the timeout and this tool cannot verify the
#       chain statically.
#
# ``subprocess.Popen`` is out of scope: the daemon already pairs every Popen
# with a subsequent ``proc.wait(timeout=...)`` call. A follow-up issue will
# extend this check to the Popen-pairing invariant.
#
# Tracking: issues #3213 (subprocess.run) and #3309 (urlopen).
#
# Usage
# -----
#   scripts/check-subprocess-timeouts.sh            # scan all scripts/**/*.py
#   scripts/check-subprocess-timeouts.sh [file]     # scan a specific file (tests)
#
# Exit codes
# ----------
#   0 — Every network egress call site has a timeout= kwarg or **kwargs splat.
#   1 — At least one call site is missing a timeout constraint.

# venv: none
# permanent: true

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── Build the list of files to scan ─────────────────────────────────────────
# If a specific file was passed as $1, scan only that file.
# Otherwise walk every *.py under scripts/ excluding any path containing /tests/.

if [[ $# -gt 0 ]]; then
    TARGET_FILE="$1"
    if [[ ! -f "$TARGET_FILE" ]]; then
        echo "check-subprocess-timeouts: no target file at $TARGET_FILE — nothing to check."
        exit 0
    fi
    # Run python helper on a single file and capture output.
    python_output="$(python3 - "$TARGET_FILE" <<'PYEOF'
import ast
import sys
from pathlib import Path

path = Path(sys.argv[1])
source = path.read_text()
try:
    tree = ast.parse(source)
except SyntaxError as exc:
    print(f"SYNTAX_ERROR:{exc}", file=sys.stderr)
    sys.exit(0)

def is_subprocess_run(func):
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )

def is_urlopen(func):
    # urllib.request.urlopen(...)
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "urlopen"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "request"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "urllib"
    ):
        return True
    # from urllib.request import urlopen; urlopen(...)
    if isinstance(func, ast.Name) and func.id == "urlopen":
        return True
    return False

for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    func = node.func
    if not (is_subprocess_run(func) or is_urlopen(func)):
        continue
    # Check for timeout= kwarg or **kwargs splat (kw.arg is None).
    has_timeout = any(
        kw.arg == "timeout" or kw.arg is None
        for kw in node.keywords
    )
    if not has_timeout:
        # Print file:line: source_snippet
        snippet = source.splitlines()[node.lineno - 1].strip()
        print(f"{path}:{node.lineno}: {snippet}")
PYEOF
)"

    if [[ -z "${python_output// /}" ]]; then
        echo "check-subprocess-timeouts: $TARGET_FILE — OK (all network egress calls have timeout= or **kwargs)"
        exit 0
    fi
    echo "ERROR: network egress call(s) missing timeout= kwarg or **kwargs splat."
    echo ""
    echo "  Violations:"
    echo ""
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        echo "    $line"
    done <<< "$python_output"
    echo ""
    echo "  Fix: add timeout=<seconds> to each call. Choose a value appropriate"
    echo "  to the operation (e.g. timeout=30 for local git ops, timeout=120 for"
    echo "  network gh calls)."
    echo ""
    echo "  See #3213 (subprocess.run) and #3309 (urlopen) for the full audit and rationale."
    exit 1
fi

# ─── Multi-file scan mode ─────────────────────────────────────────────────────
# Collect all *.py files under scripts/, excluding any path with /tests/ in it.
mapfile_compat() {
    # bash 3.2 compat: read into array without mapfile/readarray.
    # Usage: mapfile_compat varname command [args...]
    # We use a temp file instead of process substitution + readarray.
    local _varname="$1"
    shift
    local _tmpfile
    _tmpfile="$(mktemp)"
    "$@" > "$_tmpfile" 2>/dev/null || true
    # Read lines into positional params then copy to named array via eval.
    # Portable: bash 3.2+.
    local _line
    local _arr=()
    while IFS= read -r _line; do
        _arr+=("$_line")
    done < "$_tmpfile"
    rm -f "$_tmpfile"
    eval "${_varname}=(\"\${_arr[@]}\")"
}

# Collect candidate files using find (no mapfile needed if we process inline).
violations_total=0
files_scanned=0
violation_lines=()

while IFS= read -r -d '' py_file; do
    # Exclude any path containing /tests/ or /.venv/ segment.
    if [[ "$py_file" == */tests/* ]]; then
        continue
    fi
    if [[ "$py_file" == */.venv/* ]]; then
        continue
    fi
    files_scanned=$((files_scanned + 1))

    # Run the AST checker on this file.
    file_output="$(python3 - "$py_file" <<'PYEOF'
import ast
import sys
from pathlib import Path

path = Path(sys.argv[1])
source = path.read_text()
try:
    tree = ast.parse(source)
except SyntaxError:
    sys.exit(0)

def is_subprocess_run(func):
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )

def is_urlopen(func):
    # urllib.request.urlopen(...)
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "urlopen"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "request"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "urllib"
    ):
        return True
    # from urllib.request import urlopen; urlopen(...)
    if isinstance(func, ast.Name) and func.id == "urlopen":
        return True
    return False

for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    func = node.func
    if not (is_subprocess_run(func) or is_urlopen(func)):
        continue
    has_timeout = any(
        kw.arg == "timeout" or kw.arg is None
        for kw in node.keywords
    )
    if not has_timeout:
        snippet = source.splitlines()[node.lineno - 1].strip()
        print(f"{path}:{node.lineno}: {snippet}")
PYEOF
)"

    if [[ -n "${file_output// /}" ]]; then
        while IFS= read -r vline; do
            [[ -z "$vline" ]] && continue
            violation_lines+=("$vline")
            violations_total=$((violations_total + 1))
        done <<< "$file_output"
    fi
done < <(find "$REPO_ROOT/scripts" -name "*.py" -print0 | sort -z)

if (( violations_total > 0 )); then
    echo "ERROR: network egress call(s) missing timeout= kwarg or **kwargs splat."
    echo ""
    echo "  Violations:"
    echo ""
    # Length-guard the iteration — see #4479. ``violation_lines+=``
    # only runs inside conditional branches, so the static check
    # treats the array as potentially empty even though
    # ``violations_total > 0`` guarantees at least one entry.
    if [ "${#violation_lines[@]}" -gt 0 ]; then
        for vline in "${violation_lines[@]}"; do
            echo "    $vline"
        done
    fi
    echo ""
    echo "  Fix: add timeout=<seconds> to each call. Choose a value appropriate"
    echo "  to the operation (e.g. timeout=30 for local git ops, timeout=120 for"
    echo "  network gh calls)."
    echo ""
    echo "  See #3213 (subprocess.run) and #3309 (urlopen) for the full audit and rationale."
    exit 1
fi

echo "check-subprocess-timeouts: ${files_scanned} file(s) scanned — all network egress calls have timeout= or **kwargs."
exit 0
