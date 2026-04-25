#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check_tf_empty_resource.py — Detect IAM resources with compact([var.*]) Resource
lists that lack a top-level count or for_each guard.

Called by check-terraform-empty-resource-risk.sh for each .tf file.

Usage:
    python3 scripts/check_tf_empty_resource.py <path/to/file.tf> [<allowlist_file>]

Exit codes:
    0 — No violations found (or all violations are in the allowlist).
    1 — One or more unallowlisted violations found.

Output:
    Lines of the form:
        <path>:<line>: <resource_type> "<resource_name>" missing count guard for
            compact([var.*]) Resource list
"""

import re
import sys
from pathlib import Path


def load_allowlist(allowlist_path: str | None) -> set[str]:
    """Load allowlist entries, stripping comments and blank lines.

    Each entry is of the form: <path>:<resource_type>:<resource_name>
    Trailing comments (# ...) are stripped.
    """
    if not allowlist_path:
        return set()
    p = Path(allowlist_path)
    if not p.is_file():
        return set()
    entries: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip inline comment
        line = line.split("#")[0].strip()
        if line:
            entries.add(line)
    return entries


def check_file(
    tf_path: str, allowlist: set[str]
) -> list[tuple[int, str, str]]:
    """Return a list of (line_number, resource_type, resource_name) violations.

    A violation is a resource block that:
      - Contains a `Resource = compact([...])` (at any depth within the block)
        where every argument inside compact() is a `var.<identifier>`
        (with optional trailing comma), AND
      - Does NOT have a top-level `count =` or `for_each =` at depth 1
        (directly inside the resource block, not nested further).
    """
    text = Path(tf_path).read_text(encoding="utf-8")
    lines = text.splitlines()

    # Pattern to match the opening of a resource block:
    #   resource "aws_iam_role_policy" "name" {
    resource_header_re = re.compile(
        r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{'
    )

    # Pattern for top-level count / for_each (at depth 1 inside resource block)
    count_re = re.compile(r'^\s*count\s*=')
    for_each_re = re.compile(r'^\s*for_each\s*=')

    # Pattern: Resource = compact([  — the start of the problematic pattern
    # We scan for this at any depth within the resource block.
    resource_compact_start_re = re.compile(
        r'\bResource\s*=\s*compact\(\['
    )

    violations: list[tuple[int, str, str]] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        m = resource_header_re.match(line.strip())
        if not m:
            i += 1
            continue

        resource_type = m.group(1)
        resource_name = m.group(2)

        # Walk the resource block, tracking brace depth.
        # depth starts at 1 because we just consumed the opening {
        depth = 1
        i += 1

        has_count_guard = False
        compact_resource_lineno: int | None = None  # 1-based line of first match
        compact_args_text = ""
        collecting_compact = False
        compact_bracket_depth = 0  # bracket depth inside compact([...])

        while i < len(lines) and depth > 0:
            ln = lines[i]
            stripped = ln.strip()

            # Increment depth BEFORE processing this line's content for
            # count/for_each checks (so we know the depth at this line).
            # We do NOT want to count braces from the current line before
            # deciding if something is depth-1.
            current_depth = depth

            # Count braces to track depth changes
            depth += stripped.count("{") - stripped.count("}")

            if depth == 0:
                # This line closes the resource block
                i += 1
                break

            # Check for count/for_each at depth 1 (direct children of resource block)
            if current_depth == 1:
                if count_re.match(ln) or for_each_re.match(ln):
                    has_count_guard = True

            # Detect Resource = compact([  at ANY depth within the resource block
            if not collecting_compact and compact_resource_lineno is None:
                mc = resource_compact_start_re.search(ln)
                if mc:
                    compact_resource_lineno = i + 1  # 1-based
                    # Collect from the [ after compact(
                    bracket_pos = ln.index("[", mc.start())
                    compact_args_text = ln[bracket_pos + 1:]
                    collecting_compact = True
                    compact_bracket_depth = 1
                    # Check if compact() closes on the same line
                    for ch in compact_args_text:
                        if ch == "[":
                            compact_bracket_depth += 1
                        elif ch == "]":
                            compact_bracket_depth -= 1
                            if compact_bracket_depth == 0:
                                collecting_compact = False
                                break
            elif collecting_compact:
                # Continue collecting lines of the compact([...]) argument
                compact_args_text += " " + stripped
                for ch in stripped:
                    if ch == "[":
                        compact_bracket_depth += 1
                    elif ch == "]":
                        compact_bracket_depth -= 1
                        if compact_bracket_depth == 0:
                            collecting_compact = False
                            break

            i += 1

        # Evaluate this resource
        if compact_resource_lineno is not None:
            # Check that every argument inside compact([...]) is var.<identifier>
            # Strip trailing ] and optional ) from the collected text
            args_raw = compact_args_text
            # Remove everything from the closing ] onwards
            close_bracket = args_raw.rfind("]")
            if close_bracket != -1:
                args_raw = args_raw[:close_bracket]

            # Split by comma, trim whitespace
            arg_tokens = [t.strip() for t in args_raw.split(",") if t.strip()]

            # Every token must be var.<identifier>
            var_only_re = re.compile(r'^var\.[a-zA-Z_][a-zA-Z0-9_]*$')
            all_var = all(var_only_re.match(tok) for tok in arg_tokens)

            if all_var and arg_tokens and not has_count_guard:
                violations.append(
                    (compact_resource_lineno, resource_type, resource_name)
                )

    # Filter against allowlist
    # Allowlist entries may use either absolute paths or paths relative to
    # the repo root (e.g. "infra/terraform/modules/api-service/main.tf:...").
    # We match if the entry path equals the absolute path or is a suffix of it
    # (preceded by a path separator).
    tf_path_str = str(tf_path)
    filtered: list[tuple[int, str, str]] = []
    for lineno, rtype, rname in violations:
        in_allowlist = False
        for entry in allowlist:
            parts = entry.split(":")
            if len(parts) < 3:
                continue
            entry_path = parts[0]
            entry_rtype = parts[1]
            entry_rname = parts[2]
            if entry_rtype != rtype or entry_rname != rname:
                continue
            # Match if the allowlist path is a suffix of the scanned path
            if tf_path_str == entry_path or tf_path_str.endswith("/" + entry_path):
                in_allowlist = True
                break
        if not in_allowlist:
            filtered.append((lineno, rtype, rname))

    return filtered


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file.tf> [allowlist_file]", file=sys.stderr)
        return 2

    tf_path = sys.argv[1]
    allowlist_path = sys.argv[2] if len(sys.argv) > 2 else None

    allowlist = load_allowlist(allowlist_path)
    violations = check_file(tf_path, allowlist)

    for lineno, rtype, rname in violations:
        print(
            f"{tf_path}:{lineno}: {rtype} \"{rname}\" "
            f"missing count guard for compact([var.*]) Resource list"
        )

    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
