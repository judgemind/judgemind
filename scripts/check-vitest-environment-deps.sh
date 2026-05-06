#!/usr/bin/env bash
# check-vitest-environment-deps.sh — Verify vitest test-environment packages are direct devDeps.
#
# permanent: true
#
# When a package's vitest.config.{ts,js,mjs} declares `environment: 'jsdom'`
# (or 'happy-dom', 'edge-runtime', etc.), the corresponding npm package MUST
# be listed as a direct `devDependency` (or `dependency`) in the same
# package's package.json. Vitest's standard module resolution does not look
# inside transitive node_modules subtrees, so a transitive presence (e.g.
# pulled in via `isomorphic-dompurify` -> `jsdom`) silently fails on fresh
# worktrees as:
#
#     MISSING DEPENDENCY  Cannot find dependency 'jsdom'
#
# This is the regression class fixed in #4088 (jsdom missing from
# packages/web/package.json devDependencies despite being declared as the
# test environment).
#
# Scope:
#   - Walks every packages/*/vitest.config.{ts,js,mjs}.
#   - Extracts the literal value of `environment:` via regex (the config
#     shape is consistent across the repo — full TS AST not required).
#   - Skips configs where environment is absent or set to 'node' (which is
#     vitest's default and requires no extra package).
#   - Verifies the referenced package appears in the same package's
#     package.json `dependencies` or `devDependencies`.
#
# Out of scope (intentional):
#   - Root or non-packages/* directories (no vitest configs live there).
#   - Version-range satisfaction (npm + lockfile already enforce that).
#
# Usage:
#   scripts/check-vitest-environment-deps.sh             # exits 0 if clean, 1 if violations
#   scripts/check-vitest-environment-deps.sh [pkgs-dir]  # scan a specific packages-root
#
# Exit codes:
#   0 — Every vitest config's declared environment package is a direct dep.
#   1 — One or more configs reference an environment package not in the
#       same package's package.json.
#   2 — Usage error (bad arg, missing file, etc.).
#
# Ref: https://github.com/judgemind/judgemind/issues/4106

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKGS_DIR="${1:-$REPO_ROOT/packages}"

if [[ ! -d "$PKGS_DIR" ]]; then
    echo "ERROR: packages directory not found: $PKGS_DIR" >&2
    exit 2
fi

# Vitest's default environment — needs no extra package.
DEFAULT_ENV="node"

# Regex pulls a single-line literal: environment: 'jsdom' / "happy-dom" / etc.
# Captures the package-like identifier inside the quotes. Allows hyphens.
ENV_REGEX="environment:[[:space:]]*['\"]([A-Za-z_][A-Za-z0-9_-]*)['\"]"

violations=0
checked=0

for cfg in "$PKGS_DIR"/*/vitest.config.ts "$PKGS_DIR"/*/vitest.config.js "$PKGS_DIR"/*/vitest.config.mjs; do
    [[ -f "$cfg" ]] || continue

    pkg_dir="$(dirname "$cfg")"
    pkg_name="$(basename "$pkg_dir")"
    pkg_json="$pkg_dir/package.json"

    # Pull the environment literal — first match wins (configs only declare it once).
    env_value=""
    if [[ $(grep -cE "$ENV_REGEX" "$cfg" 2>/dev/null || true) -gt 0 ]]; then
        # shellcheck disable=SC2001
        env_value="$(grep -oE "$ENV_REGEX" "$cfg" | head -n1 | sed -E "s/.*['\"]([A-Za-z_][A-Za-z0-9_-]*)['\"].*/\1/")"
    fi

    # Skip configs without environment, or with the default 'node'.
    if [[ -z "$env_value" || "$env_value" == "$DEFAULT_ENV" ]]; then
        continue
    fi

    checked=$((checked + 1))

    if [[ ! -f "$pkg_json" ]]; then
        echo "ERROR: $cfg declares environment '$env_value' but no package.json found at $pkg_json" >&2
        violations=$((violations + 1))
        continue
    fi

    # Use python3 to safely parse package.json — pure shell JSON parsing is brittle.
    set +e
    python3 - "$pkg_json" "$env_value" <<'PYEOF' 2>/dev/null
import json
import sys
from pathlib import Path

pkg_json_path = Path(sys.argv[1])
needed = sys.argv[2]

try:
    data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    print(f"  parse error: {exc}", file=sys.stderr)
    sys.exit(2)

deps = data.get("dependencies", {}) or {}
dev_deps = data.get("devDependencies", {}) or {}

if needed in deps or needed in dev_deps:
    sys.exit(0)
sys.exit(1)
PYEOF
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
        :  # dep present — no action
    elif [[ $rc -eq 1 ]]; then
        if [[ $violations -eq 0 ]]; then
            echo "ERROR: vitest environment package(s) missing from same-package package.json:" >&2
            echo "" >&2
        fi
        echo "  packages/$pkg_name/vitest.config.* declares environment '$env_value'" >&2
        echo "  but '$env_value' is not in $pkg_json (dependencies or devDependencies)." >&2
        echo "" >&2
        echo "  Fix: cd $pkg_dir && npm install --save-dev $env_value" >&2
        echo "" >&2
        violations=$((violations + 1))
    else
        echo "ERROR: failed to parse $pkg_json (python3 exit $rc)" >&2
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo "Found $violations violation(s) across $checked vitest config(s) with non-default environment." >&2
    echo "" >&2
    echo "  This guard exists because Vitest's module resolution cannot find" >&2
    echo "  test-environment packages that are only present transitively. See" >&2
    echo "  https://github.com/judgemind/judgemind/issues/4088 for the canonical" >&2
    echo "  incident." >&2
    exit 1
fi

echo "All clean — checked $checked vitest config(s) with non-default environment."
exit 0
