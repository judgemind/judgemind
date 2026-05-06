#!/usr/bin/env python3
# venv: none
# permanent: true
"""
check_tf_ecs_entrypoint.py — Detect ECS task definitions whose container
`command` starts with an interpreter (python, python3, bash, sh, node) but
whose container does NOT specify an `entryPoint` override.

The bug class this guards against (issue #4270, parent #4255):

The scraper image's Dockerfile sets

    ENTRYPOINT ["python", "-m"]
    CMD        ["framework"]

so the actual argv exec'd inside the container is

    python -m <command...>

If a Terraform `aws_ecs_task_definition` declares

    command = ["python3", "scripts/check-...py"]

without overriding `entryPoint`, the runtime command becomes

    python -m python3 scripts/check-...py
    -> ModuleNotFoundError: No module named 'python3'

and the task silently exits 1 every fire. This was the root cause of the
silent breakage of `judgemind-zero-record-check-dev` and the field-
population-audit task def (fixed in #4260).

The defensive rule enforced by this script: any container in
`infra/terraform/modules/**/main.tf` whose `command` starts with an
interpreter name (python, python3, bash, sh, node) MUST also declare an
`entryPoint` override on the same container. Containers that legitimately
rely on a Dockerfile ENTRYPOINT that is itself an argv-passthrough shim
(e.g. dispatcher-v3's `["/bin/sh", "-c", "exec \"$@\"", "--"]`) can be
allowlisted by adding a line to
`scripts/check-terraform-ecs-entrypoint-allowlist.txt`.

Called by check-terraform-ecs-entrypoint.sh for each .tf file.

Usage:
    python3 scripts/check_tf_ecs_entrypoint.py <path/to/file.tf> [<allowlist_file>]

Exit codes:
    0 — No violations found (or all violations are in the allowlist).
    1 — One or more unallowlisted violations found.

Output:
    Lines of the form:
        <path>:<line>: aws_ecs_task_definition "<name>" container "<container>"
            command starts with interpreter "<interpreter>" without entryPoint override

The allowlist format is:
    <path>:<resource_name>:<container_name>  # issue #NNNN
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Interpreters that, if used as the first argv token, indicate the
# container is implicitly relying on the Dockerfile ENTRYPOINT to provide
# the actual interpreter — exactly the #4270 bug shape.
INTERPRETERS = frozenset({"python", "python3", "bash", "sh", "node"})


def load_allowlist(allowlist_path: str | None) -> set[str]:
    """Load allowlist entries, stripping comments and blank lines.

    Each entry is of the form: <path>:<resource_name>:<container_name>
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


# Regex to find the start of an aws_ecs_task_definition resource block.
# Must match at the start of a line (HCL resource declarations are
# top-level), so we use re.MULTILINE-style `^`.
TASK_DEF_HEADER_RE = re.compile(
    r'^resource\s+"aws_ecs_task_definition"\s+"([^"]+)"\s*\{', re.MULTILINE
)

# Regex to find the start of jsonencode([ -- the container_definitions encoder
JSONENCODE_START_RE = re.compile(r'\bcontainer_definitions\s*=\s*jsonencode\(')


def _extract_balanced_list(text: str, start: int) -> tuple[str, int] | None:
    """Given text and a position pointing at '[', return (list_str, end_pos+1).

    Walks the text, balancing [] and respecting "..." string boundaries.
    Returns None if no balanced ] is found.
    """
    if start >= len(text) or text[start] != "[":
        return None
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        ch = text[i]
        if ch == '"' and (i == 0 or text[i - 1] != "\\"):
            in_string = not in_string
        elif not in_string:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1], i + 1
        i += 1
    return None


def _scan_containers(
    body_text: str, body_start_line: int
) -> list[tuple[int, str, str | None, list[str], list[str] | None]]:
    """Walk a container_definitions body and yield container records.

    Returns a list of (line_number, container_name, command_first_arg,
    command_list, entrypoint_list) for each container block found inside
    the body. line_number is 1-based, relative to the original file.
    """
    # Each container is a `{ ... }` block inside the surrounding `[...]`
    # array. We walk top-level `{` boundaries.
    results: list[tuple[int, str, str | None, list[str], list[str] | None]] = []

    depth = 0
    in_string = False
    i = 0
    block_start: int | None = None
    while i < len(body_text):
        ch = body_text[i]
        if ch == '"' and (i == 0 or body_text[i - 1] != "\\"):
            in_string = not in_string
            i += 1
            continue
        if not in_string:
            if ch == "{":
                if depth == 0:
                    block_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and block_start is not None:
                    block_text = body_text[block_start : i + 1]
                    record = _parse_container_block(
                        block_text, body_text, body_start_line, block_start
                    )
                    if record is not None:
                        results.append(record)
                    block_start = None
        i += 1
    return results


def _parse_container_block(
    block_text: str, body_text: str, body_start_line: int, block_offset: int
) -> tuple[int, str, str | None, list[str], list[str] | None] | None:
    """Parse a single container `{...}` block.

    Returns (line_number, container_name, command_first_arg, command_list,
    entrypoint_list) or None if the block has no `command` key.

    line_number is the 1-based line of the `command` assignment within the
    original file.
    """
    # Find name = "..."
    name_match = re.search(r'name\s*=\s*"([^"]+)"', block_text)
    container_name = name_match.group(1) if name_match else "<unnamed>"

    # Find command = [...]  -- balanced bracket walk.
    command_list: list[str] | None = None
    entrypoint_list: list[str] | None = None
    command_offset_in_block: int | None = None

    for key_re, target_attr in (
        (re.compile(r"\bcommand\s*=\s*"), "command"),
        (re.compile(r"\bentryPoint\s*=\s*"), "entryPoint"),
    ):
        for m in key_re.finditer(block_text):
            after = m.end()
            # Skip whitespace / newlines
            j = after
            while j < len(block_text) and block_text[j] in " \t\r\n":
                j += 1
            if j >= len(block_text) or block_text[j] != "[":
                continue
            extracted = _extract_balanced_list(block_text, j)
            if extracted is None:
                continue
            list_str, _end = extracted
            try:
                parsed = json.loads(list_str)
            except (json.JSONDecodeError, ValueError):
                # Not a flat string literal list. We don't try to evaluate
                # variable references — those legitimately can't be checked
                # statically.
                continue
            if not isinstance(parsed, list) or not all(
                isinstance(x, str) for x in parsed
            ):
                continue
            if target_attr == "command":
                command_list = parsed
                command_offset_in_block = m.start()
            elif target_attr == "entryPoint":
                entrypoint_list = parsed
            break  # First match within the block wins

    if command_list is None:
        return None

    # Compute the 1-based line number of the `command =` assignment in the
    # original file. body_start_line is the 1-based line where the body
    # begins (inside jsonencode([). block_offset is the 0-based offset of
    # this block's `{` within body_text. command_offset_in_block is the
    # 0-based offset of the `command` keyword within the block.
    if command_offset_in_block is not None:
        absolute_offset = block_offset + command_offset_in_block
    else:
        absolute_offset = block_offset
    line_in_body = body_text[:absolute_offset].count("\n") + 1
    line_number = body_start_line + line_in_body - 1

    first_arg = command_list[0] if command_list else None
    return line_number, container_name, first_arg, command_list, entrypoint_list


def check_file(
    tf_path: str, allowlist: set[str]
) -> list[tuple[int, str, str, str]]:
    """Return a list of (line_number, resource_name, container_name, interpreter) violations."""
    text = Path(tf_path).read_text(encoding="utf-8")

    violations: list[tuple[int, str, str, str]] = []

    # Walk the file, find each `resource "aws_ecs_task_definition"` block,
    # and inspect its container_definitions = jsonencode([...]) body.
    pos = 0
    while pos < len(text):
        m = TASK_DEF_HEADER_RE.search(text, pos)
        if m is None:
            break
        resource_name = m.group(1)
        # Find matching `}` for this resource block (balanced).
        # Start tracking depth from the `{` after the header.
        block_start = text.find("{", m.start())
        if block_start == -1:
            break
        depth = 0
        in_string = False
        i = block_start
        block_end = -1
        while i < len(text):
            ch = text[i]
            if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        block_end = i
                        break
            i += 1
        if block_end == -1:
            break
        resource_block = text[block_start : block_end + 1]

        # Find the `container_definitions = jsonencode([` in this block.
        je_match = JSONENCODE_START_RE.search(resource_block)
        if je_match is None:
            pos = block_end + 1
            continue

        # Walk forward to the `[` after `jsonencode(`
        bracket_pos = resource_block.find("[", je_match.end())
        if bracket_pos == -1:
            pos = block_end + 1
            continue

        extracted = _extract_balanced_list(resource_block, bracket_pos)
        if extracted is None:
            pos = block_end + 1
            continue
        body_with_brackets, _ = extracted
        # Strip outer [ ... ]
        body_text = body_with_brackets[1:-1]

        # Compute the 1-based line in the original file where body_text begins
        body_offset_in_resource = bracket_pos + 1
        body_offset_in_file = block_start + body_offset_in_resource
        body_start_line = text[:body_offset_in_file].count("\n") + 1

        for record in _scan_containers(body_text, body_start_line):
            line_no, container_name, first_arg, _command_list, entrypoint_list = (
                record
            )
            if first_arg is None:
                continue
            if first_arg not in INTERPRETERS:
                continue
            if entrypoint_list is not None:
                # Has an explicit entryPoint override — safe.
                continue

            # Check allowlist
            tf_path_str = str(tf_path)
            in_allowlist = False
            for entry in allowlist:
                parts = entry.split(":")
                if len(parts) < 3:
                    continue
                entry_path = parts[0]
                entry_resource = parts[1]
                entry_container = parts[2]
                if (
                    entry_resource == resource_name
                    and entry_container == container_name
                    and (
                        tf_path_str == entry_path
                        or tf_path_str.endswith("/" + entry_path)
                    )
                ):
                    in_allowlist = True
                    break
            if not in_allowlist:
                violations.append(
                    (line_no, resource_name, container_name, first_arg)
                )

        pos = block_end + 1

    return violations


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file.tf> [allowlist_file]", file=sys.stderr)
        return 2

    tf_path = sys.argv[1]
    allowlist_path = sys.argv[2] if len(sys.argv) > 2 else None

    allowlist = load_allowlist(allowlist_path)
    violations = check_file(tf_path, allowlist)

    for lineno, resource_name, container_name, interpreter in violations:
        print(
            f'{tf_path}:{lineno}: aws_ecs_task_definition "{resource_name}" '
            f'container "{container_name}" command starts with interpreter '
            f'"{interpreter}" without entryPoint override'
        )

    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
