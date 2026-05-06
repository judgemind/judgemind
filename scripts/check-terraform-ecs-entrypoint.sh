#!/usr/bin/env bash
# check-terraform-ecs-entrypoint.sh -- Detect ECS task definitions whose
# container `command` starts with an interpreter (python, python3, bash,
# sh, node) but whose container does NOT specify an `entryPoint` override.
#
# Bug class (issue #4270, parent #4255):
#
#   The scraper image's Dockerfile sets
#       ENTRYPOINT ["python", "-m"]
#       CMD        ["framework"]
#   so the actual argv exec'd inside the container is
#       python -m <command...>
#   If a Terraform aws_ecs_task_definition declares
#       command = ["python3", "scripts/check-...py"]
#   without overriding entryPoint, the runtime command becomes
#       python -m python3 scripts/check-...py
#       -> ModuleNotFoundError: No module named 'python3'
#   and the task silently exits 1 every fire. Same root cause class as
#   #2840 (silent task-def drift).
#
# Usage:
#   scripts/check-terraform-ecs-entrypoint.sh            # scan infra/
#   scripts/check-terraform-ecs-entrypoint.sh --list     # print scanned
#                                                         files, exit 0
#
# Exit codes:
#   0 -- No violations found (or --list mode).
#   1 -- One or more unallowlisted violations found.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ALLOWLIST="$REPO_ROOT/scripts/check-terraform-ecs-entrypoint-allowlist.txt"

PYTHON="${PYTHON:-python3}"

# Directories to skip
EXCLUDE_DIRS=(
    ".git"
    ".terraform"
    ".venv"
    "node_modules"
    "__pycache__"
    "tests"
)

# Parse arguments
LIST_MODE=false
if [[ "${1:-}" == "--list" ]]; then
    LIST_MODE=true
fi

# Collect .tf files to scan
# Scan every main.tf under infra/terraform/modules/**/ and
# infra/terraform/environments/**/. Skip module test fixtures (under
# infra/terraform/modules/*/tests/) -- those are deliberately broken
# fixtures that exercise postcondition/policy rules.
TF_FILES=()

while IFS= read -r -d '' f; do
    skip=false
    for excl_dir in "${EXCLUDE_DIRS[@]}"; do
        if [[ "$f" == *"/$excl_dir/"* ]]; then
            skip=true
            break
        fi
    done
    if ! "$skip"; then
        TF_FILES+=("$f")
    fi
done < <(find "$REPO_ROOT/infra/terraform" -name "main.tf" -print0 2>/dev/null)

if [[ "$LIST_MODE" == true ]]; then
    for f in "${TF_FILES[@]}"; do
        echo "$f"
    done
    exit 0
fi

# Run the check
violations=0

for f in "${TF_FILES[@]}"; do
    if ! output=$("$PYTHON" "$REPO_ROOT/scripts/check_tf_ecs_entrypoint.py" "$f" "$ALLOWLIST" 2>&1); then
        echo "$output"
        violations=$((violations + 1))
    elif [[ -n "$output" ]]; then
        echo "$output"
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "ERROR: Found $violations file(s) with aws_ecs_task_definition resources"
    echo "       whose container command starts with an interpreter (python,"
    echo "       python3, bash, sh, node) without an entryPoint override."
    echo ""
    echo "       The scraper image's Dockerfile ENTRYPOINT is [\"python\", \"-m\"],"
    echo "       so a command starting with another interpreter produces a"
    echo "       silent runtime failure (e.g. python -m python3 scripts/foo.py"
    echo "       -> No module named python3). See issue #4270."
    echo ""
    echo "  Fix: add an explicit entryPoint to the container definition. Example:"
    echo ""
    echo "      container_definitions = jsonencode(["
    echo "        {"
    echo "          name       = \"my-task\""
    echo "          image      = \"...\""
    echo "          entryPoint = [\"python3\"]"
    echo "          command    = [\"scripts/foo.py\", \"--flag\"]"
    echo "        }"
    echo "      ])"
    echo ""
    echo "  Allowlist (only when the Dockerfile ENTRYPOINT is an argv-passthrough"
    echo "  shim — e.g. dispatcher-v3's [\"/bin/sh\", \"-c\", \"exec \\\"\$@\\\"\", \"--\"])"
    echo "  by adding a line to:"
    echo "    scripts/check-terraform-ecs-entrypoint-allowlist.txt"
    echo "  Format: <path>:<resource_name>:<container_name>  # issue #NNNN"
    echo ""
    exit 1
fi

echo "All clean -- no aws_ecs_task_definition resources are missing entryPoint overrides."
exit 0
