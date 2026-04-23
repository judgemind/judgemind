#!/usr/bin/env python3
"""Parse Claude Code Agent NDJSON output and display a human-readable summary.

Agent output files (under /private/tmp/claude-*/.../tasks/<agent-id>.output)
are raw NDJSON — each line is a full message event with the entire conversation
context, tool inputs/outputs, and metadata. This script extracts the useful
signal and presents it compactly.

Usage:
    python3 scripts/agent_status.py <agent-id-or-path> [--last N] [--full]
"""
# permanent: true

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ToolCall:
    """A single tool invocation extracted from the NDJSON stream."""

    timestamp: str = ""
    name: str = ""
    input_summary: str = ""
    status: str = "unknown"  # success, error, running, unknown
    error_message: str = ""


@dataclass
class AgentSummary:
    """Aggregated summary of an agent's activity."""

    agent_id: str = ""
    file_path: str = ""
    is_running: bool = False
    duration_seconds: float = 0.0
    task_description: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tool_calls: int = 0
    errors: list[str] = field(default_factory=list)
    first_timestamp: str = ""
    last_timestamp: str = ""


def find_output_file(agent_id: str) -> Path | None:
    """Search for an agent output file by agent ID.

    Looks in common Claude Code temp directories for output files
    matching the given agent ID.
    """
    # Search common locations for Claude Code agent output
    search_dirs = [
        Path("/private/tmp"),
        Path("/tmp"),
        Path.home() / ".claude",
    ]

    for base_dir in search_dirs:
        if not base_dir.exists():
            continue
        # Look for claude-* directories
        for claude_dir in sorted(base_dir.glob("claude-*"), reverse=True):
            tasks_dir = claude_dir / "tasks"
            if not tasks_dir.exists():
                # Also try nested structure
                for sub in claude_dir.iterdir():
                    if sub.is_dir():
                        tasks_sub = sub / "tasks"
                        if tasks_sub.exists():
                            output_file = tasks_sub / f"{agent_id}.output"
                            if output_file.exists():
                                return output_file
                continue
            output_file = tasks_dir / f"{agent_id}.output"
            if output_file.exists():
                return output_file

    # Also try recursive glob as a fallback
    for base_dir in search_dirs:
        if not base_dir.exists():
            continue
        for match in base_dir.glob(f"**/tasks/{agent_id}.output"):
            return match

    return None


def is_file_being_written(path: Path) -> bool:
    """Heuristic: check if the file is still being written to.

    Compares file size at two points 0.5s apart.
    """
    try:
        size1 = path.stat().st_size
        time.sleep(0.3)
        size2 = path.stat().st_size
        return size2 > size1
    except OSError:
        return False


def truncate_string(s: str, max_len: int = 80) -> str:
    """Truncate a string to max_len characters, adding ellipsis if needed."""
    s = s.replace("\n", " ").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "\u2026"


def extract_tool_input_summary(tool_name: str, tool_input: dict) -> str:
    """Create a concise summary of tool input based on tool type."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return truncate_string(cmd, 70)
    elif tool_name == "Read":
        path = tool_input.get("file_path", "")
        # Show just the filename or last 2 path components
        parts = path.rsplit("/", 2)
        short = "/".join(parts[-2:]) if len(parts) >= 2 else path
        return short
    elif tool_name == "Write":
        path = tool_input.get("file_path", "")
        parts = path.rsplit("/", 2)
        short = "/".join(parts[-2:]) if len(parts) >= 2 else path
        return short
    elif tool_name == "Edit":
        path = tool_input.get("file_path", "")
        parts = path.rsplit("/", 2)
        short = "/".join(parts[-2:]) if len(parts) >= 2 else path
        return short
    elif tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", ".")
        return truncate_string(f'"{pattern}" in {path}', 70)
    elif tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        return truncate_string(pattern, 70)
    elif tool_name == "Skill":
        skill = tool_input.get("skill", "")
        args = tool_input.get("args", "")
        return truncate_string(f"{skill} {args}".strip(), 70)
    elif tool_name == "Agent":
        prompt = tool_input.get("prompt", "")
        return truncate_string(prompt, 70)
    else:
        # Generic: show first key-value pair
        for k, v in tool_input.items():
            return truncate_string(f"{k}={v}", 70)
        return ""


def parse_timestamp(ts_str: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string."""
    if not ts_str:
        return None
    try:
        # Handle various ISO formats
        ts_str = ts_str.rstrip("Z")
        if "+" in ts_str or ts_str.count("-") > 2:
            return datetime.fromisoformat(ts_str)
        return datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins:02d}m{secs:02d}s"


def read_tail_lines(path: Path, max_lines: int = 200) -> list[str]:
    """Read lines from the end of a file efficiently.

    For large NDJSON files, we don't need to read everything - just the
    tail. We read chunks from the end and split into lines.
    """
    file_size = path.stat().st_size
    if file_size == 0:
        return []

    # For small files, just read everything
    if file_size < 1_000_000:  # < 1 MB
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()

    # For large files, read from the end in chunks
    # NDJSON lines from Claude can be very large (10s of KB each)
    # so read a generous chunk
    chunk_size = min(file_size, max_lines * 100_000)  # ~100KB per line estimate

    lines: list[str] = []
    with open(path, "rb") as f:
        f.seek(max(0, file_size - chunk_size))
        # Skip partial first line (unless we're at the beginning)
        if f.tell() > 0:
            f.readline()
        data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()

    return lines[-max_lines:]


def parse_ndjson_file(path: Path, max_events: int = 200) -> AgentSummary:
    """Parse an NDJSON agent output file into an AgentSummary.

    The NDJSON format from Claude Code contains message events. Each line
    is a JSON object representing a conversation event. We extract tool
    calls, timestamps, token usage, and errors.
    """
    summary = AgentSummary()
    summary.file_path = str(path)

    # Extract agent ID from filename
    summary.agent_id = path.stem.replace(".output", "")

    # Check if agent is still running
    summary.is_running = is_file_being_written(path)

    lines = read_tail_lines(path, max_events)

    all_tool_calls: list[ToolCall] = []
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    seen_tool_use_ids: set[str] = set()

    for line in lines:
        line = line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Truncated line (agent still writing) — skip
            continue

        # Extract timestamp
        ts_str = ""
        if isinstance(event, dict):
            ts_str = event.get("timestamp", "") or event.get("ts", "")

        ts = parse_timestamp(ts_str)
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        # Different NDJSON event formats from Claude Code
        if not isinstance(event, dict):
            continue

        event_type = event.get("type", "")

        # Look for the task/prompt in initial messages
        if event_type == "system" and not summary.task_description:
            msg = event.get("message", "")
            if msg:
                summary.task_description = truncate_string(msg, 100)

        # Extract tool use from content blocks
        # Format 1: message with content array containing tool_use blocks
        message = event.get("message", event)
        if isinstance(message, dict):
            role = message.get("role", "")
            content = message.get("content", [])

            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue

                    block_type = block.get("type", "")

                    # Tool use request
                    if block_type == "tool_use":
                        tool_id = block.get("id", "")
                        if tool_id in seen_tool_use_ids:
                            continue
                        seen_tool_use_ids.add(tool_id)

                        tc = ToolCall()
                        tc.timestamp = ts_str
                        tc.name = block.get("name", "unknown")
                        tool_input = block.get("input", {})
                        if isinstance(tool_input, dict):
                            tc.input_summary = extract_tool_input_summary(
                                tc.name, tool_input
                            )
                        tc.status = "running"  # Until we see the result
                        all_tool_calls.append(tc)

                    # Tool result
                    elif block_type == "tool_result":
                        is_error = block.get("is_error", False)

                        # Find the matching tool call and update its status
                        for tc in reversed(all_tool_calls):
                            if tc.status == "running":
                                if is_error:
                                    tc.status = "error"
                                    error_content = block.get("content", "")
                                    if isinstance(error_content, list):
                                        for ec in error_content:
                                            if isinstance(ec, dict):
                                                error_content = ec.get("text", "")
                                                break
                                    if isinstance(error_content, str):
                                        tc.error_message = truncate_string(
                                            error_content, 100
                                        )
                                        summary.errors.append(
                                            f"{tc.name}: {tc.error_message}"
                                        )
                                else:
                                    tc.status = "success"
                                break

            # Also check for text content that mentions the task
            if role == "user" and not summary.task_description:
                if isinstance(content, str):
                    summary.task_description = truncate_string(content, 100)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text and len(text) > 10:
                                summary.task_description = truncate_string(text, 100)
                                break

        # Extract token usage
        usage = event.get("usage", {})
        if isinstance(usage, dict):
            summary.total_input_tokens += usage.get("input_tokens", 0)
            summary.total_output_tokens += usage.get("output_tokens", 0)

    # Compute duration
    if first_ts and last_ts:
        summary.duration_seconds = (last_ts - first_ts).total_seconds()
        summary.first_timestamp = first_ts.strftime("%H:%M:%S")
        summary.last_timestamp = last_ts.strftime("%H:%M:%S")

    summary.tool_calls = all_tool_calls
    summary.total_tool_calls = len(all_tool_calls)

    return summary


def format_summary(
    summary: AgentSummary, last_n: int = 5, show_all: bool = False
) -> str:
    """Format an AgentSummary into a human-readable string."""
    lines: list[str] = []

    # Header
    status = "running" if summary.is_running else "completed"
    duration = (
        format_duration(summary.duration_seconds)
        if summary.duration_seconds > 0
        else "unknown"
    )
    lines.append(f"Agent: {summary.agent_id} ({status}, {duration})")

    if summary.task_description:
        lines.append(f"Task: {summary.task_description}")

    lines.append("")

    # Recent actions
    tool_calls = summary.tool_calls
    if not tool_calls:
        lines.append("No tool calls found.")
    else:
        if show_all:
            display_calls = tool_calls
            lines.append(f"All actions ({len(tool_calls)}):")
        else:
            display_calls = tool_calls[-last_n:]
            if len(tool_calls) > last_n:
                lines.append(f"Recent actions (last {last_n} of {len(tool_calls)}):")
            else:
                lines.append("Recent actions:")

        for tc in display_calls:
            # Status indicator
            if tc.status == "success":
                indicator = "\u2713"
            elif tc.status == "error":
                indicator = "\u2717"
            elif tc.status == "running":
                indicator = "\u23f3"
            else:
                indicator = "?"

            # Format timestamp
            ts_display = ""
            if tc.timestamp:
                ts_dt = parse_timestamp(tc.timestamp)
                if ts_dt:
                    ts_display = f"[{ts_dt.strftime('%H:%M:%S')}] "

            # Build action line
            input_display = f": {tc.input_summary}" if tc.input_summary else ""
            action_line = f"  {ts_display}{tc.name}{input_display}"
            # Pad and add indicator
            action_line = f"{action_line:<78} {indicator}"
            lines.append(action_line)

            if tc.status == "error" and tc.error_message:
                lines.append(f"    ERROR: {tc.error_message}")

    # Errors summary (deduplicated from tool call errors)
    hook_errors = [
        e for e in summary.errors if "hook" in e.lower() or "denied" in e.lower()
    ]
    if hook_errors:
        lines.append("")
        lines.append("Blocked/hook errors:")
        for err in hook_errors[-3:]:
            lines.append(f"  ! {err}")

    # Token usage
    if summary.total_input_tokens > 0 or summary.total_output_tokens > 0:
        lines.append("")
        lines.append(
            f"Tokens: {summary.total_input_tokens:,} input / "
            f"{summary.total_output_tokens:,} output / "
            f"{summary.total_tool_calls} tool calls"
        )

    return "\n".join(lines)


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Show a human-readable summary of a Claude Code agent's activity.",
        usage="scripts/agent-status.sh <agent-id-or-path> [--last N] [--full]",
    )
    parser.add_argument(
        "target",
        help="Agent ID or path to the .output NDJSON file",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=5,
        dest="last_n",
        help="Number of recent actions to show (default: 5)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Show all actions instead of just the last N",
    )
    args = parser.parse_args()

    # Resolve target to a file path
    target = args.target
    target_path = Path(target)

    if target_path.is_file():
        output_file = target_path
    else:
        # Treat as agent ID and search for the output file
        output_file = find_output_file(target)
        if output_file is None:
            print(
                f"Error: could not find output file for agent '{target}'",
                file=sys.stderr,
            )
            print(
                "Searched in /private/tmp/claude-*/tasks/ and ~/.claude/",
                file=sys.stderr,
            )
            print("", file=sys.stderr)
            print(
                "Tip: pass the full path to the .output file instead.", file=sys.stderr
            )
            sys.exit(1)

    if not output_file.exists():
        print(f"Error: file not found: {output_file}", file=sys.stderr)
        sys.exit(1)

    # Parse and display
    summary = parse_ndjson_file(output_file, max_events=500 if args.full else 200)
    output = format_summary(summary, last_n=args.last_n, show_all=args.full)
    print(output)


if __name__ == "__main__":
    main()
