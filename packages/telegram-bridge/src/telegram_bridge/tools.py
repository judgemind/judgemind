"""Read-only tool definitions and execution for the Opus responder agent.

Provides a set of sandboxed, read-only tools that the Claude Opus interpreter
can use to answer substantive questions about the Judgemind codebase, GitHub
state, and database contents.

All tools are strictly read-only — no file modifications, no git pushes, no
destructive commands.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Maximum bytes of output returned from any single tool execution.
MAX_OUTPUT_BYTES: int = 30_000

#: Maximum number of lines returned from file reads.
MAX_READ_LINES: int = 500

#: Timeout for shell commands in seconds.
SHELL_TIMEOUT_SECONDS: int = 30

# ── Allowlisted shell commands ─────────────────────────────────────────

#: Prefixes that are allowed in ``run_shell_command``.  Each entry is a
#: tuple of argument tokens that the command must start with.
ALLOWED_COMMAND_PREFIXES: list[tuple[str, ...]] = [
    ("gh", "issue", "list"),
    ("gh", "issue", "view"),
    ("gh", "pr", "list"),
    ("gh", "pr", "view"),
    ("gh", "pr", "checks"),
    ("gh", "run", "list"),
    ("gh", "run", "view"),
    ("git", "log"),
    ("git", "diff"),
    ("git", "show"),
    ("git", "status"),
    ("git", "branch"),
    ("scripts/dev-db-query.sh",),
]


def is_command_allowed(args: list[str]) -> bool:
    """Check whether *args* matches one of the allowlisted command prefixes.

    Args:
        args: The command split into tokens (e.g. ``["gh", "issue", "list"]``).

    Returns:
        ``True`` if the command is allowed.
    """
    for prefix in ALLOWED_COMMAND_PREFIXES:
        if len(args) >= len(prefix) and tuple(args[: len(prefix)]) == prefix:
            return True
    return False


# ── Tool schemas (Anthropic tool_use format) ───────────────────────────

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file from the repository. "
            "Returns the first 500 lines. Use offset to read further into "
            "large files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path from the repository root "
                        "(e.g. 'packages/telegram-bridge/src/telegram_bridge/interpreter.py')."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (0-based). Default 0.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max lines to return. Default and max {MAX_READ_LINES}.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep_files",
        "description": (
            "Search for a regex pattern across files in the repository. "
            "Returns matching lines with file paths and line numbers. "
            "Use the glob parameter to filter by file pattern."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for.",
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Optional glob to filter files (e.g. '*.py', 'packages/api/**/*.ts'). "
                        "Defaults to all files."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matching lines to return. Default 50.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "glob_files",
        "description": (
            "List files matching a glob pattern relative to the repository root. "
            "Useful for discovering file structure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Glob pattern (e.g. 'packages/*/pyproject.toml', '**/*scraper*.py')."
                    ),
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_shell_command",
        "description": (
            "Run an allowlisted read-only shell command and return its output. "
            "Allowed commands: gh issue/pr list/view, git log/diff/show/status/branch, "
            "scripts/dev-db-query.sh. No destructive commands."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The shell command to run (e.g. 'gh issue view 42 "
                        "--repo judgemind/judgemind --json title,body')."
                    ),
                },
            },
            "required": ["command"],
        },
    },
]


# ── Tool execution ─────────────────────────────────────────────────────


def _truncate(text: str, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + "\n\n... [output truncated]"


def execute_read_file(
    *,
    repo_root: Path,
    path: str,
    offset: int = 0,
    limit: int = MAX_READ_LINES,
) -> str:
    """Read a file from the repository.

    Args:
        repo_root: Absolute path to the repository root.
        path: Relative path from repo root.
        offset: Line offset (0-based).
        limit: Max lines to return.

    Returns:
        File contents with line numbers, or an error message.
    """
    limit = min(limit, MAX_READ_LINES)
    resolved = (repo_root / path).resolve()

    # Security: prevent path traversal outside the repo.
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return f"Error: path '{path}' resolves outside the repository."

    if not resolved.is_file():
        return f"Error: '{path}' is not a file or does not exist."

    try:
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"Error reading file: {exc}"

    total = len(lines)
    selected = lines[offset : offset + limit]
    numbered = [f"{i + offset + 1:>6}  {line}" for i, line in enumerate(selected)]
    header = f"# {path} (lines {offset + 1}-{offset + len(selected)} of {total})\n"
    return _truncate(header + "\n".join(numbered))


def execute_grep_files(
    *,
    repo_root: Path,
    pattern: str,
    glob: str | None = None,
    max_results: int = 50,
) -> str:
    """Search for a regex pattern across files.

    Uses ``grep -rn`` under the hood for simplicity and portability.

    Args:
        repo_root: Absolute path to the repository root.
        pattern: Regex pattern.
        glob: Optional file glob filter.
        max_results: Max matching lines.

    Returns:
        Matching lines with file paths and line numbers.
    """
    max_results = min(max_results, 200)
    cmd = [
        "grep",
        "-rn",
        "--include",
        glob or "*",
        "-E",
        pattern,
        ".",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "Error: search timed out."
    except OSError as exc:
        return f"Error running grep: {exc}"

    lines = result.stdout.splitlines()[:max_results]
    if not lines:
        return "No matches found."
    output = "\n".join(lines)
    if len(result.stdout.splitlines()) > max_results:
        remaining = len(result.stdout.splitlines()) - max_results
        output += f"\n\n... [{remaining} more matches truncated]"
    return _truncate(output)


_GLOB_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache"}
)


def execute_glob_files(
    *,
    repo_root: Path,
    pattern: str,
) -> str:
    """List files matching a glob pattern.

    Uses ``Path.glob()`` directly instead of enumerating all files,
    and skips common large directories that are never interesting.

    Args:
        repo_root: Absolute path to the repository root.
        pattern: Glob pattern relative to repo root.

    Returns:
        Newline-separated list of matching file paths.
    """
    matches: list[str] = []
    for p in sorted(repo_root.glob(pattern)):
        if not p.is_file():
            continue
        # Skip files inside excluded directories.
        rel = p.relative_to(repo_root)
        if _GLOB_EXCLUDED_DIRS.intersection(rel.parts):
            continue
        matches.append(str(rel))
        if len(matches) >= 200:
            break

    if not matches:
        return "No files matched the pattern."
    output = "\n".join(matches)
    return _truncate(output)


def execute_shell_command(
    *,
    repo_root: Path,
    command: str,
) -> str:
    """Run an allowlisted shell command.

    Args:
        repo_root: Absolute path to the repository root (used as cwd).
        command: The command string to execute.

    Returns:
        Command stdout/stderr, or an error message if blocked.
    """
    import shlex

    try:
        args = shlex.split(command)
    except ValueError as exc:
        return f"Error parsing command: {exc}"

    if not args:
        return "Error: empty command."

    if not is_command_allowed(args):
        return (
            f"Error: command '{args[0]}' is not in the allowlist. "
            f"Allowed: gh issue/pr list/view, git log/diff/show/status/branch, "
            f"scripts/dev-db-query.sh"
        )

    # Set up environment — inherit PATH so gh/git are found.
    env = dict(os.environ)

    try:
        result = subprocess.run(
            args,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out."
    except OSError as exc:
        return f"Error running command: {exc}"

    output = result.stdout
    if result.stderr:
        output += f"\n--- stderr ---\n{result.stderr}"
    if result.returncode != 0:
        output += f"\n[exit code: {result.returncode}]"
    return _truncate(output)


def execute_tool(
    *,
    repo_root: Path,
    tool_name: str,
    tool_input: dict[str, Any],
) -> str:
    """Dispatch a tool call to the appropriate executor.

    Args:
        repo_root: Absolute path to the repository root.
        tool_name: Name of the tool to execute.
        tool_input: Tool input parameters.

    Returns:
        Tool output as a string.
    """
    if tool_name == "read_file":
        return execute_read_file(
            repo_root=repo_root,
            path=tool_input.get("path", ""),
            offset=tool_input.get("offset", 0),
            limit=tool_input.get("limit", MAX_READ_LINES),
        )
    elif tool_name == "grep_files":
        return execute_grep_files(
            repo_root=repo_root,
            pattern=tool_input.get("pattern", ""),
            glob=tool_input.get("glob"),
            max_results=tool_input.get("max_results", 50),
        )
    elif tool_name == "glob_files":
        return execute_glob_files(
            repo_root=repo_root,
            pattern=tool_input.get("pattern", ""),
        )
    elif tool_name == "run_shell_command":
        return execute_shell_command(
            repo_root=repo_root,
            command=tool_input.get("command", ""),
        )
    else:
        return f"Error: unknown tool '{tool_name}'."
