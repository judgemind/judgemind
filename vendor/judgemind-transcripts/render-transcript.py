#!/usr/bin/env python3
"""
Render Claude Code JSONL session transcripts to human-readable text.

Produces a readable conversation log showing:
  - User messages prefixed with ">>> USER:"
  - Assistant text output (the bulk of content, no prefix)
  - Tool calls and results shown compactly
  - Thinking blocks omitted (internal reasoning, not shown in CLI)

Usage:
    python3 render-transcript.py <input.jsonl> [output.txt]
    python3 render-transcript.py --all <transcripts-dir>
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path


def fmt_ts(iso: str) -> str:
    """Format ISO timestamp to local readable form."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return iso or ""


def truncate(text: str, limit: int = 2000) -> str:
    """Truncate long text with an indicator."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n  ... [{len(text) - limit} more chars truncated]"


def render_tool_result_content(content) -> str:
    """Render tool_result content (string or list of text/image blocks)."""
    if isinstance(content, str):
        return truncate(content)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(truncate(item.get("text", "")))
                elif item.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(f"[{item.get('type', '?')}]")
            else:
                parts.append(str(item)[:200])
        return "\n".join(parts)
    return str(content)[:200]


def render_session(jsonl_path: str) -> str:
    """Render a single JSONL transcript to readable text."""
    lines: list[str] = []
    tool_names: dict[str, str] = {}  # tool_use_id -> tool name
    session_start = None
    session_id = None
    model = None
    git_branch = None

    with open(jsonl_path) as f:
        records = []
        for raw in f:
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError:
                continue

    # Extract session metadata from first timestamped record
    for rec in records:
        if rec.get("timestamp") and not session_start:
            session_start = rec["timestamp"]
        if rec.get("sessionId") and not session_id:
            session_id = rec["sessionId"]
        if rec.get("gitBranch") and not git_branch:
            git_branch = rec["gitBranch"]
        if rec.get("type") == "assistant":
            msg = rec.get("message", {})
            if msg.get("model") and not model:
                model = msg["model"]
        if session_start and session_id and model:
            break

    # Header
    lines.append("=" * 72)
    lines.append(f"Session: {session_id or 'unknown'}")
    lines.append(f"Started: {fmt_ts(session_start) if session_start else 'unknown'}")
    if model:
        lines.append(f"Model:   {model}")
    if git_branch:
        lines.append(f"Branch:  {git_branch}")
    lines.append("=" * 72)
    lines.append("")

    for rec in records:
        rtype = rec.get("type")
        ts = rec.get("timestamp", "")

        # Skip non-conversation records
        if rtype in (
            "file-history-snapshot", "progress", "system",
            "queue-operation", "pr-link", "last-prompt", "custom-title",
        ):
            continue

        if rtype == "user":
            msg = rec.get("message", {})
            content = msg.get("content", "")

            if isinstance(content, str) and content.strip():
                # Direct user input
                lines.append(f">>> USER [{fmt_ts(ts)}]:")
                lines.append(content.strip())
                lines.append("")

            elif isinstance(content, list):
                user_texts = []
                tool_results = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = block.get("text", "").strip()
                        if text:
                            user_texts.append(text)
                    elif block.get("type") == "tool_result":
                        tool_results.append(block)

                # Show user text if present (e.g. "[Request interrupted by user]")
                for text in user_texts:
                    lines.append(f">>> USER [{fmt_ts(ts)}]:")
                    lines.append(text)
                    lines.append("")

                # Tool results omitted — the tool call itself is shown
                # in the assistant turn; results are verbose and not needed
                # for the human-readable audit trail.

        elif rtype == "assistant":
            msg = rec.get("message", {})
            content = msg.get("content", [])

            if isinstance(content, str):
                if content.strip():
                    lines.append(content.strip())
                    lines.append("")
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")

                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        lines.append(text)
                        lines.append("")

                elif btype == "thinking":
                    # Thinking is internal — not shown in CLI. Skip.
                    pass

                elif btype == "tool_use":
                    tool_id = block.get("id", "")
                    tool_name = block.get("name", "?")
                    tool_input = block.get("input", {})
                    tool_names[tool_id] = tool_name

                    # Render tool call compactly
                    summary = format_tool_call(tool_name, tool_input)
                    lines.append(f"  [{tool_name}] {summary}")
                    lines.append("")

    return "\n".join(lines)


def format_tool_call(name: str, inp: dict) -> str:
    """Format a tool call's input into a compact one-line summary."""
    if name == "Read":
        return inp.get("file_path", "")
    elif name == "Write":
        path = inp.get("file_path", "")
        content = inp.get("content", "")
        return f"{path} ({len(content)} chars)"
    elif name == "Edit":
        path = inp.get("file_path", "")
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        return f"{path} ({len(old)} -> {len(new)} chars)"
    elif name == "Bash":
        cmd = inp.get("command", "")
        desc = inp.get("description", "")
        if desc:
            return f"{desc}: {truncate(cmd, 200)}"
        return truncate(cmd, 200)
    elif name == "Glob":
        return f"{inp.get('pattern', '')} in {inp.get('path', '.')}"
    elif name == "Grep":
        pattern = inp.get("pattern", "")
        path = inp.get("path", ".")
        return f"/{pattern}/ in {path}"
    elif name == "Agent":
        desc = inp.get("description", "")
        stype = inp.get("subagent_type", "")
        return f"{stype}: {desc}" if stype else desc
    elif name == "Skill":
        return inp.get("skill", "")
    elif name in ("TaskCreate", "TaskUpdate"):
        desc = inp.get("description", inp.get("status", ""))
        return str(desc)[:100]
    else:
        # Generic: show keys and truncated values
        parts = []
        for k, v in inp.items():
            sv = str(v)
            if len(sv) > 80:
                sv = sv[:80] + "..."
            parts.append(f"{k}={sv}")
        return ", ".join(parts)[:300]


def needs_render(jsonl_path: Path, txt_path: Path) -> bool:
    """Check if a .txt needs to be (re-)rendered.

    Renders when: .txt is missing, or JSONL is newer than .txt (session grew).
    """
    if not txt_path.exists():
        return True
    return jsonl_path.stat().st_mtime > txt_path.stat().st_mtime


def render_all(transcripts_dir: str) -> int:
    """Render .txt for any JSONL that is new or has been updated."""
    transcripts_dir = Path(transcripts_dir)
    jsonl_files = sorted(transcripts_dir.glob("*.jsonl"))
    rendered = 0
    skipped = 0

    for jsonl_path in jsonl_files:
        txt_path = jsonl_path.with_suffix(".txt")
        if not needs_render(jsonl_path, txt_path):
            skipped += 1
            continue

        try:
            text = render_session(str(jsonl_path))
            txt_path.write_text(text, encoding="utf-8")
            rendered += 1
            print(f"  Rendered: {txt_path.name}")
        except Exception as e:
            print(f"  ERROR rendering {jsonl_path.name}: {e}")

    # Also render subagent transcripts
    subagents_dir = transcripts_dir / "subagents"
    if subagents_dir.exists():
        for session_dir in sorted(subagents_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            for jsonl_path in sorted(session_dir.glob("*.jsonl")):
                txt_path = jsonl_path.with_suffix(".txt")
                if not needs_render(jsonl_path, txt_path):
                    skipped += 1
                    continue
                try:
                    text = render_session(str(jsonl_path))
                    txt_path.write_text(text, encoding="utf-8")
                    rendered += 1
                    print(f"  Rendered: subagents/{session_dir.name}/{txt_path.name}")
                except Exception as e:
                    print(f"  ERROR rendering {jsonl_path.name}: {e}")

    print(f"\nDone. {rendered} rendered, {skipped} unchanged.")
    return rendered


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "--all":
        directory = sys.argv[2] if len(sys.argv) > 2 else str(Path.home() / "judgemind" / "transcripts")
        render_all(directory)
    else:
        jsonl_path = sys.argv[1]
        output_path = sys.argv[2] if len(sys.argv) > 2 else None
        text = render_session(jsonl_path)
        if output_path:
            Path(output_path).write_text(text, encoding="utf-8")
            print(f"Written to {output_path}")
        else:
            print(text)
