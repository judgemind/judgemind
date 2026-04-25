#!/usr/bin/env bash
# check-no-nonascii-tf-descriptions.sh -- Fail if any Terraform `description`
# argument in `infra/terraform/**/*.tf` contains a non-ASCII byte.
#
# Why this check exists
# ---------------------
# AWS rejects non-ASCII characters in `description` fields for several
# resource types (security groups, IAM roles, IAM policies, etc.) at apply
# time. This is a class of bug that:
#   - Passes `terraform validate` (the AWS provider does not validate
#     description content client-side).
#   - Passes `terraform plan` if the resource does not exist yet (the
#     AWS API is only called at apply time for create operations).
#   - Fails `terraform apply` with an opaque
#     "Character sets beyond ASCII are not supported" or
#     "Member must satisfy regular expression pattern: [\t\n\r -~]*" error.
#
# Three consecutive `terraform.yml / Apply dev` runs on `main` were broken
# by an em-dash (U+2014) in two `description` fields in
# `infra/terraform/modules/scraper-zero-record-check/main.tf` (issue #3321).
# This check prevents that class of failure structurally.
#
# Rule
# ----
# Every `description = "..."` argument under `infra/terraform/**/*.tf` must
# be strict 7-bit ASCII (bytes 0x09, 0x0A, 0x0D, or 0x20-0x7E). Any byte
# outside that set is a violation.
#
# Usage
# -----
#   scripts/check-no-nonascii-tf-descriptions.sh
#
# Exit codes
# ----------
#   0 -- No violations found.
#   1 -- At least one description argument contains a non-ASCII byte.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${REPO_ROOT}/infra/terraform"

if [[ ! -d "$TF_DIR" ]]; then
    echo "check-no-nonascii-tf-descriptions: no infra/terraform/ -- nothing to check."
    exit 0
fi

# Use Python for a precise byte-level scan. BSD grep on macOS lacks
# Perl-compatible regex (`-P`), so a portable shell-only solution is
# fragile. Python is available everywhere we run CI (GHA and operator
# laptops).
python_output="$(python3 - "$TF_DIR" <<'PYEOF'
import re
import sys
from pathlib import Path

tf_root = Path(sys.argv[1])

# Match any line where the first non-whitespace token is `description`
# followed by `=`. Captures both top-level resource arguments and nested
# block arguments (e.g. inside an `egress` block).
DESC_RE = re.compile(r"^[ \t]*description[ \t]*=")

violations = []
for tf_file in sorted(tf_root.rglob("*.tf")):
    # Read as bytes so we can detect any byte > 0x7E without locale
    # interference.
    raw = tf_file.read_bytes()
    # Split on universal newlines so line numbers match editor display.
    lines = raw.split(b"\n")
    for lineno, line in enumerate(lines, start=1):
        try:
            decoded = line.decode("utf-8")
        except UnicodeDecodeError:
            # Non-UTF-8 bytes anywhere on the line -- definite violation
            # if the line is a description argument.
            decoded = line.decode("latin-1")
        if not DESC_RE.match(decoded):
            continue
        # Allowed bytes: tab (0x09), CR (0x0D), and printable ASCII
        # (0x20-0x7E). LF would have been split out already.
        bad_bytes = [b for b in line if b not in (0x09, 0x0D) and not (0x20 <= b <= 0x7E)]
        if bad_bytes:
            rel = tf_file.relative_to(tf_root.parent)
            # Show the offending line, replacing non-ASCII bytes with
            # `<U+XXXX>` markers for clarity.
            display = decoded.encode("ascii", errors="backslashreplace").decode("ascii")
            print(f"{rel}:{lineno}: {display.rstrip()}")
            violations.append((rel, lineno))

sys.exit(1 if violations else 0)
PYEOF
)" && rc=0 || rc=$?

if (( rc == 0 )); then
    exit 0
fi

echo "ERROR: non-ASCII byte(s) found in Terraform 'description' argument(s)."
echo ""
echo "  AWS rejects non-ASCII characters in description fields for several"
echo "  resource types (security groups, IAM roles, IAM policies, etc.) at"
echo "  apply time. terraform plan does NOT catch this -- the AWS API is"
echo "  only called when the resource is actually created."
echo ""
echo "  Fix: replace the non-ASCII byte(s) with 7-bit ASCII equivalents."
echo "  Common offender: em-dash U+2014 ('--' or '-' is the safe replacement)."
echo ""
echo "  See issue #3321 for context."
echo ""
echo "  Violating lines:"
echo ""
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    echo "    $entry"
done <<< "$python_output"
echo ""
exit 1
