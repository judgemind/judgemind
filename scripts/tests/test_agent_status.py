"""Tests for agent_status module."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_status import (
    AgentSummary,
    ToolCall,
    extract_tool_input_summary,
    format_duration,
    format_summary,
    parse_ndjson_file,
    truncate_string,
)


class TestTruncateString:
    """Tests for truncate_string."""

    def test_short_string_unchanged(self) -> None:
        assert truncate_string("hello", 80) == "hello"

    def test_long_string_truncated(self) -> None:
        result = truncate_string("a" * 100, 80)
        assert len(result) == 80
        assert result.endswith("\u2026")

    def test_newlines_replaced(self) -> None:
        assert truncate_string("line1\nline2\nline3", 80) == "line1 line2 line3"

    def test_whitespace_stripped(self) -> None:
        assert truncate_string("  hello  ", 80) == "hello"


class TestFormatDuration:
    """Tests for format_duration."""

    def test_seconds(self) -> None:
        assert format_duration(45) == "45s"

    def test_minutes(self) -> None:
        assert format_duration(125) == "2m05s"

    def test_hours(self) -> None:
        assert format_duration(3725) == "1h02m05s"


class TestExtractToolInputSummary:
    """Tests for extract_tool_input_summary."""

    def test_bash_command(self) -> None:
        result = extract_tool_input_summary("Bash", {"command": "git status"})
        assert result == "git status"

    def test_read_file(self) -> None:
        result = extract_tool_input_summary(
            "Read", {"file_path": "/path/to/some/file.py"}
        )
        assert "file.py" in result

    def test_grep_pattern(self) -> None:
        result = extract_tool_input_summary("Grep", {"pattern": "TODO", "path": "/src"})
        assert "TODO" in result
        assert "/src" in result

    def test_glob_pattern(self) -> None:
        result = extract_tool_input_summary("Glob", {"pattern": "**/*.py"})
        assert result == "**/*.py"

    def test_skill(self) -> None:
        result = extract_tool_input_summary("Skill", {"skill": "task", "args": "#42"})
        assert "task" in result
        assert "#42" in result

    def test_unknown_tool(self) -> None:
        result = extract_tool_input_summary("Custom", {"key": "value"})
        assert "key=value" in result

    def test_empty_input(self) -> None:
        result = extract_tool_input_summary("Custom", {})
        assert result == ""


class TestParseNdjsonFile:
    """Tests for parse_ndjson_file."""

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.output"
        f.write_text("")
        summary = parse_ndjson_file(f)
        assert summary.agent_id == "empty"
        assert len(summary.tool_calls) == 0

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "malformed.output"
        f.write_text("not json\n{broken json\n")
        summary = parse_ndjson_file(f)
        assert len(summary.tool_calls) == 0

    def test_tool_use_extracted(self, tmp_path: Path) -> None:
        """Test that tool_use blocks are parsed into ToolCall objects."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-03-09T12:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_001",
                            "name": "Bash",
                            "input": {"command": "git status"},
                        }
                    ],
                },
            },
            {
                "type": "tool_result",
                "timestamp": "2026-03-09T12:00:05Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_001",
                            "is_error": False,
                            "content": "On branch main",
                        }
                    ],
                },
            },
        ]
        f = tmp_path / "agent123.output"
        f.write_text("\n".join(json.dumps(e) for e in events) + "\n")

        summary = parse_ndjson_file(f)
        assert summary.agent_id == "agent123"
        assert len(summary.tool_calls) == 1
        assert summary.tool_calls[0].name == "Bash"
        assert summary.tool_calls[0].input_summary == "git status"
        assert summary.tool_calls[0].status == "success"

    def test_tool_error_captured(self, tmp_path: Path) -> None:
        """Test that tool errors are recorded."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-03-09T12:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_002",
                            "name": "Bash",
                            "input": {"command": "rm -rf /"},
                        }
                    ],
                },
            },
            {
                "type": "tool_result",
                "timestamp": "2026-03-09T12:00:01Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_002",
                            "is_error": True,
                            "content": "Permission denied",
                        }
                    ],
                },
            },
        ]
        f = tmp_path / "agent_err.output"
        f.write_text("\n".join(json.dumps(e) for e in events) + "\n")

        summary = parse_ndjson_file(f)
        assert len(summary.tool_calls) == 1
        assert summary.tool_calls[0].status == "error"
        assert "Permission denied" in summary.tool_calls[0].error_message

    def test_multiple_tool_calls(self, tmp_path: Path) -> None:
        """Test parsing multiple sequential tool calls."""
        events = []
        for i in range(3):
            events.append(
                {
                    "type": "assistant",
                    "timestamp": f"2026-03-09T12:0{i}:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"tu_{i:03d}",
                                "name": "Read",
                                "input": {"file_path": f"/path/file{i}.py"},
                            }
                        ],
                    },
                }
            )
            events.append(
                {
                    "type": "tool_result",
                    "timestamp": f"2026-03-09T12:0{i}:01Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"tu_{i:03d}",
                                "is_error": False,
                                "content": f"file content {i}",
                            }
                        ],
                    },
                }
            )

        f = tmp_path / "multi.output"
        f.write_text("\n".join(json.dumps(e) for e in events) + "\n")

        summary = parse_ndjson_file(f)
        assert len(summary.tool_calls) == 3
        assert all(tc.status == "success" for tc in summary.tool_calls)

    def test_duration_computed(self, tmp_path: Path) -> None:
        """Test that duration is computed from first to last timestamp."""
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-03-09T12:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_a",
                            "name": "Bash",
                            "input": {"command": "echo start"},
                        }
                    ],
                },
            },
            {
                "type": "tool_result",
                "timestamp": "2026-03-09T12:05:30Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_a",
                            "is_error": False,
                            "content": "start",
                        }
                    ],
                },
            },
        ]
        f = tmp_path / "duration.output"
        f.write_text("\n".join(json.dumps(e) for e in events) + "\n")

        summary = parse_ndjson_file(f)
        assert summary.duration_seconds == 330.0  # 5m30s

    def test_token_usage_accumulated(self, tmp_path: Path) -> None:
        """Test that token usage is summed across events."""
        events = [
            {"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 50}},
            {"type": "usage", "usage": {"input_tokens": 200, "output_tokens": 30}},
        ]
        f = tmp_path / "tokens.output"
        f.write_text("\n".join(json.dumps(e) for e in events) + "\n")

        summary = parse_ndjson_file(f)
        assert summary.total_input_tokens == 300
        assert summary.total_output_tokens == 80

    def test_deduplicates_tool_use_ids(self, tmp_path: Path) -> None:
        """Test that duplicate tool_use IDs are not double-counted."""
        event = {
            "type": "assistant",
            "timestamp": "2026-03-09T12:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_dup",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ],
            },
        }
        # Same event appears twice (e.g., in incremental NDJSON)
        f = tmp_path / "dedup.output"
        f.write_text(json.dumps(event) + "\n" + json.dumps(event) + "\n")

        summary = parse_ndjson_file(f)
        assert len(summary.tool_calls) == 1


class TestFormatSummary:
    """Tests for format_summary."""

    def test_basic_format(self) -> None:
        summary = AgentSummary(
            agent_id="abc123",
            is_running=True,
            duration_seconds=272,
            task_description="/task #42",
            tool_calls=[
                ToolCall(
                    timestamp="2026-03-09T12:00:00Z",
                    name="Bash",
                    input_summary="git status",
                    status="success",
                ),
            ],
            total_input_tokens=1000,
            total_output_tokens=500,
            total_tool_calls=1,
        )
        output = format_summary(summary)
        assert "abc123" in output
        assert "running" in output
        assert "4m32s" in output
        assert "/task #42" in output
        assert "Bash" in output
        assert "git status" in output
        assert "\u2713" in output
        assert "1,000 input" in output

    def test_completed_status(self) -> None:
        summary = AgentSummary(
            agent_id="xyz",
            is_running=False,
            duration_seconds=60,
        )
        output = format_summary(summary)
        assert "completed" in output

    def test_last_n_limits_output(self) -> None:
        calls = [ToolCall(name=f"Tool{i}", status="success") for i in range(10)]
        summary = AgentSummary(
            agent_id="test",
            tool_calls=calls,
            total_tool_calls=10,
        )
        output = format_summary(summary, last_n=3)
        assert "last 3 of 10" in output
        # Should show Tool7, Tool8, Tool9 (last 3)
        assert "Tool7" in output
        assert "Tool8" in output
        assert "Tool9" in output
        assert "Tool0" not in output

    def test_full_flag(self) -> None:
        calls = [ToolCall(name=f"Tool{i}", status="success") for i in range(10)]
        summary = AgentSummary(
            agent_id="test",
            tool_calls=calls,
            total_tool_calls=10,
        )
        output = format_summary(summary, show_all=True)
        assert "All actions (10)" in output
        assert "Tool0" in output
        assert "Tool9" in output

    def test_error_indicators(self) -> None:
        summary = AgentSummary(
            agent_id="test",
            tool_calls=[
                ToolCall(
                    name="Bash", status="error", error_message="Permission denied"
                ),
            ],
            errors=["Bash: Permission denied"],
            total_tool_calls=1,
        )
        output = format_summary(summary)
        assert "\u2717" in output
        assert "Permission denied" in output

    def test_running_indicator(self) -> None:
        summary = AgentSummary(
            agent_id="test",
            tool_calls=[
                ToolCall(name="Bash", status="running"),
            ],
            total_tool_calls=1,
        )
        output = format_summary(summary)
        assert "\u23f3" in output

    def test_no_tool_calls(self) -> None:
        summary = AgentSummary(agent_id="test")
        output = format_summary(summary)
        assert "No tool calls found" in output

    def test_hook_errors_shown(self) -> None:
        summary = AgentSummary(
            agent_id="test",
            errors=["Bash: Permission has been denied for this command"],
            tool_calls=[
                ToolCall(
                    name="Bash",
                    status="error",
                    error_message="Permission has been denied for this command",
                ),
            ],
            total_tool_calls=1,
        )
        output = format_summary(summary)
        assert "Blocked/hook errors" in output
