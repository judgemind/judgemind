"""Integration tests for the Opus agent end-to-end flow.

These tests exercise the full ``interpret_message_with_tools()`` pipeline
including tool execution and multi-turn conversation handling.  Each test
uses a detailed mock sequence that mirrors real Anthropic API interactions
(tool_use stop reason, tool_use content blocks with IDs, tool_result
messages) to catch regressions in the conversation loop state management.

Scenarios covered:
1. Simple question answered without tools
2. Question requiring a file read (read_file tool)
3. Question requiring a shell command (run_shell_command tool)
4. Multi-tool conversation (read_file -> grep_files -> final answer)
5. Hitting the max tool rounds limit
6. Mixed tool-use and text blocks in a single response

See issue #891 for context.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from telegram_bridge.interpreter import (
    InterpretedMessage,
    interpret_message_with_tools,
)

# ── Helpers ────────────────────────────────────────────────────────────


def _text_block(text: str) -> MagicMock:
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool_use_block(
    tool_id: str,
    name: str,
    tool_input: dict[str, Any],
) -> MagicMock:
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = tool_input
    return block


def _api_response(
    *,
    content: list[MagicMock],
    stop_reason: str = "end_turn",
) -> MagicMock:
    """Create a mock Anthropic API response."""
    response = MagicMock()
    response.stop_reason = stop_reason
    response.content = content
    return response


def _json_text(reply: str, actions: list[dict[str, Any]] | None = None) -> str:
    """Build a JSON string matching the expected response format."""
    return json.dumps({"reply": reply, "actions": actions or []})


# ── Test cases ─────────────────────────────────────────────────────────


class TestOpusAgentSimpleQuestion:
    """Scenario 1: Simple question answered without any tool use."""

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_direct_answer_no_tools(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The agent answers a simple greeting without calling any tools."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        mock_client.messages.create.return_value = _api_response(
            content=[_text_block(_json_text("Hello! Judgemind is up and running."))],
        )

        result = interpret_message_with_tools(
            text="Hey, how's it going?",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert isinstance(result, InterpretedMessage)
        assert "Judgemind" in result.reply
        assert result.actions == []
        # Only one API call — no tool rounds.
        mock_client.messages.create.assert_called_once()

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_direct_answer_with_orchestrator_action(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The agent returns a pause action without needing tool access."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        mock_client.messages.create.return_value = _api_response(
            content=[
                _text_block(
                    _json_text(
                        "Pausing the orchestrator now.",
                        actions=[{"type": "pause"}],
                    )
                )
            ],
        )

        result = interpret_message_with_tools(
            text="pause everything",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert result.reply == "Pausing the orchestrator now."
        assert len(result.actions) == 1
        assert result.actions[0]["type"] == "pause"
        mock_client.messages.create.assert_called_once()


class TestOpusAgentFileRead:
    """Scenario 2: Question requiring a file read via the read_file tool."""

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_reads_file_and_answers(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The agent reads a file, then produces a final answer based on its contents."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Set up a real file so execute_tool actually reads it.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "judgemind-api"\nversion = "2.1.0"\n'
        )

        # Turn 1: model calls read_file.
        turn1 = _api_response(
            content=[
                _text_block("Let me check the project configuration."),
                _tool_use_block("toolu_01", "read_file", {"path": "pyproject.toml"}),
            ],
            stop_reason="tool_use",
        )

        # Turn 2: model provides final answer.
        turn2 = _api_response(
            content=[
                _text_block(_json_text("The API is at version 2.1.0 based on pyproject.toml."))
            ],
        )

        mock_client.messages.create.side_effect = [turn1, turn2]

        result = interpret_message_with_tools(
            text="What version is the API?",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert "2.1.0" in result.reply
        assert result.actions == []
        assert mock_client.messages.create.call_count == 2

        # Verify the tool result was passed back correctly.
        second_call_msgs = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        # Messages: user, assistant (tool use), user (tool result)
        assert len(second_call_msgs) == 3
        tool_result_msg = second_call_msgs[2]
        assert tool_result_msg["role"] == "user"
        assert tool_result_msg["content"][0]["type"] == "tool_result"
        assert tool_result_msg["content"][0]["tool_use_id"] == "toolu_01"
        # The tool result should contain the actual file content.
        assert "judgemind-api" in tool_result_msg["content"][0]["content"]
        assert "2.1.0" in tool_result_msg["content"][0]["content"]

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_file_not_found_handled_gracefully(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When the requested file does not exist, the tool returns an error
        string and the agent handles it gracefully."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Turn 1: model tries to read a nonexistent file.
        turn1 = _api_response(
            content=[
                _tool_use_block("toolu_02", "read_file", {"path": "missing.txt"}),
            ],
            stop_reason="tool_use",
        )

        # Turn 2: model acknowledges the file was not found.
        turn2 = _api_response(
            content=[_text_block(_json_text("That file does not exist in the repository."))],
        )

        mock_client.messages.create.side_effect = [turn1, turn2]

        result = interpret_message_with_tools(
            text="Show me missing.txt",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert "does not exist" in result.reply.lower()

        # The tool result should contain an error message.
        second_call_msgs = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        tool_result_content = second_call_msgs[2]["content"][0]["content"]
        assert "error" in tool_result_content.lower()


class TestOpusAgentShellCommand:
    """Scenario 3: Question requiring a shell command (run_shell_command tool)."""

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_runs_allowed_shell_command(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The agent runs an allowlisted git command and uses its output."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Initialize a git repo so git status works.
        import subprocess

        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "hello.py").write_text("print('hello')\n")

        # Turn 1: model calls run_shell_command with git status.
        turn1 = _api_response(
            content=[
                _tool_use_block("toolu_03", "run_shell_command", {"command": "git status"}),
            ],
            stop_reason="tool_use",
        )

        # Turn 2: final answer referencing the git status output.
        turn2 = _api_response(
            content=[
                _text_block(
                    _json_text(
                        "There is one untracked file: hello.py. The repo has no commits yet."
                    )
                )
            ],
        )

        mock_client.messages.create.side_effect = [turn1, turn2]

        result = interpret_message_with_tools(
            text="What's the git status?",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert "hello.py" in result.reply
        assert mock_client.messages.create.call_count == 2

        # Verify the tool result contains actual git status output.
        second_call_msgs = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        tool_result_content = second_call_msgs[2]["content"][0]["content"]
        assert "hello.py" in tool_result_content

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_blocked_shell_command_returns_error(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When the model tries a disallowed command, the tool returns an error."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Turn 1: model tries a blocked command.
        turn1 = _api_response(
            content=[
                _tool_use_block("toolu_04", "run_shell_command", {"command": "rm -rf /"}),
            ],
            stop_reason="tool_use",
        )

        # Turn 2: model explains the command was blocked.
        turn2 = _api_response(
            content=[_text_block(_json_text("That command is not allowed for security reasons."))],
        )

        mock_client.messages.create.side_effect = [turn1, turn2]

        interpret_message_with_tools(
            text="delete everything",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        # The tool result passed back to the model should mention the allowlist.
        second_call_msgs = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        tool_result_content = second_call_msgs[2]["content"][0]["content"]
        assert "not in the allowlist" in tool_result_content.lower()


class TestOpusAgentMultiToolRound:
    """Scenario 4: Multi-turn conversation with multiple tool calls."""

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_two_sequential_tool_calls(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The agent reads a file, then searches for a pattern, then answers."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Set up test files.
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text("class Scraper:\n    def run(self): pass\n")
        (src_dir / "utils.py").write_text("def helper(): pass\n")

        # Turn 1: model reads a file.
        turn1 = _api_response(
            content=[
                _text_block("Let me look at the source code."),
                _tool_use_block("toolu_05", "read_file", {"path": "src/main.py"}),
            ],
            stop_reason="tool_use",
        )

        # Turn 2: model searches for more context.
        turn2 = _api_response(
            content=[
                _text_block("I see a Scraper class. Let me check for related files."),
                _tool_use_block("toolu_06", "grep_files", {"pattern": "class.*:", "glob": "*.py"}),
            ],
            stop_reason="tool_use",
        )

        # Turn 3: final answer.
        turn3 = _api_response(
            content=[
                _text_block(
                    _json_text(
                        "The codebase has a Scraper class in src/main.py with a run() "
                        "method, and a helper utility in src/utils.py."
                    )
                )
            ],
        )

        mock_client.messages.create.side_effect = [turn1, turn2, turn3]

        result = interpret_message_with_tools(
            text="What classes exist in the codebase?",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert "Scraper" in result.reply
        assert mock_client.messages.create.call_count == 3

        # The messages list is mutated in place across rounds, so after all
        # rounds complete it has the full conversation history.  Since
        # call_args_list captures the list by reference, all entries point
        # to the final state: user, asst, result, asst, result = 5 messages.
        final_msgs = mock_client.messages.create.call_args_list[2].kwargs["messages"]
        assert len(final_msgs) == 5
        # Verify alternating roles across the full conversation.
        assert final_msgs[0]["role"] == "user"
        assert final_msgs[1]["role"] == "assistant"
        assert final_msgs[2]["role"] == "user"
        assert final_msgs[3]["role"] == "assistant"
        assert final_msgs[4]["role"] == "user"
        # First tool result should reference toolu_05 (read_file).
        assert final_msgs[2]["content"][0]["tool_use_id"] == "toolu_05"
        # Second tool result should reference toolu_06 (grep_files).
        assert final_msgs[4]["content"][0]["tool_use_id"] == "toolu_06"


class TestOpusAgentMaxRoundsLimit:
    """Scenario 5: Hitting the max tool rounds limit."""

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_stops_at_max_rounds_with_fallback(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When the model keeps calling tools beyond max_tool_rounds,
        the loop terminates and returns a fallback message."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Every response calls a tool — the agent never produces a final answer.
        endless_tool_response = _api_response(
            content=[
                _tool_use_block("toolu_loop", "read_file", {"path": "irrelevant.txt"}),
            ],
            stop_reason="tool_use",
        )

        mock_client.messages.create.return_value = endless_tool_response

        result = interpret_message_with_tools(
            text="Keep looking forever",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
            max_tool_rounds=3,
        )

        # Should stop after exactly 3 rounds.
        assert mock_client.messages.create.call_count == 3
        # Fallback message since no text block was produced.
        assert isinstance(result, InterpretedMessage)
        assert len(result.reply) > 0

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_stops_at_max_rounds_with_text_and_tool(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """When the final round has both text and tool_use blocks, the text
        is extracted even though the loop was exhausted."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Response has both text and tool_use — stop_reason is still tool_use.
        mixed_response = _api_response(
            content=[
                _text_block("I am still looking but here is what I found so far."),
                _tool_use_block("toolu_mix", "read_file", {"path": "something.txt"}),
            ],
            stop_reason="tool_use",
        )

        mock_client.messages.create.return_value = mixed_response

        result = interpret_message_with_tools(
            text="Find everything",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
            max_tool_rounds=2,
        )

        assert mock_client.messages.create.call_count == 2
        # The text from the last response should be used even though
        # the loop was exhausted — it falls through to the text extraction
        # logic which reads from the last `response` variable.
        # Since the text is not valid JSON, _parse_response treats it as raw text.
        assert "still looking" in result.reply.lower()


class TestOpusAgentMixedContentBlocks:
    """Scenario 6: Response with mixed text and tool_use blocks."""

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_text_and_tool_in_same_response(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The model can include text alongside tool calls in a single response.
        The tool call should be executed and results passed back."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        (tmp_path / "config.json").write_text('{"debug": true}\n')

        # Turn 1: text + tool_use together.
        turn1 = _api_response(
            content=[
                _text_block("Let me check the configuration."),
                _tool_use_block("toolu_07", "read_file", {"path": "config.json"}),
            ],
            stop_reason="tool_use",
        )

        # Turn 2: final answer.
        turn2 = _api_response(
            content=[_text_block(_json_text("Debug mode is enabled in config.json."))],
        )

        mock_client.messages.create.side_effect = [turn1, turn2]

        result = interpret_message_with_tools(
            text="Is debug mode on?",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert "debug" in result.reply.lower()
        assert mock_client.messages.create.call_count == 2

        # The assistant content passed back should include ALL blocks (text + tool_use).
        second_call_msgs = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        assistant_msg = second_call_msgs[1]
        assert assistant_msg["role"] == "assistant"
        # The content is the raw list from turn1.content (both blocks).
        assert len(assistant_msg["content"]) == 2

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_multiple_tool_calls_in_single_response(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The model requests two tool calls in a single response (parallel tool use).
        Both results should be returned."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        (tmp_path / "a.txt").write_text("file A contents\n")
        (tmp_path / "b.txt").write_text("file B contents\n")

        # Turn 1: two tool calls in one response.
        turn1 = _api_response(
            content=[
                _tool_use_block("toolu_08a", "read_file", {"path": "a.txt"}),
                _tool_use_block("toolu_08b", "read_file", {"path": "b.txt"}),
            ],
            stop_reason="tool_use",
        )

        # Turn 2: final answer using both file contents.
        turn2 = _api_response(
            content=[
                _text_block(
                    _json_text("File A says 'file A contents' and file B says 'file B contents'.")
                )
            ],
        )

        mock_client.messages.create.side_effect = [turn1, turn2]

        result = interpret_message_with_tools(
            text="Compare files a.txt and b.txt",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert "file A" in result.reply
        assert "file B" in result.reply

        # Both tool results should be in the follow-up message.
        second_call_msgs = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        tool_results = second_call_msgs[2]["content"]
        assert len(tool_results) == 2
        assert tool_results[0]["tool_use_id"] == "toolu_08a"
        assert tool_results[1]["tool_use_id"] == "toolu_08b"
        assert "file A contents" in tool_results[0]["content"]
        assert "file B contents" in tool_results[1]["content"]


class TestOpusAgentConversationState:
    """Verify conversation state management across the tool-use loop."""

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_system_prompt_consistent_across_rounds(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The system prompt and tools should be identical in every API call."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        (tmp_path / "test.txt").write_text("data\n")

        turn1 = _api_response(
            content=[_tool_use_block("toolu_09", "read_file", {"path": "test.txt"})],
            stop_reason="tool_use",
        )
        turn2 = _api_response(
            content=[_text_block(_json_text("Done."))],
        )

        mock_client.messages.create.side_effect = [turn1, turn2]

        interpret_message_with_tools(
            text="read test.txt",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        calls = mock_client.messages.create.call_args_list
        assert len(calls) == 2

        # System prompt should be the same in both calls.
        assert calls[0].kwargs["system"] == calls[1].kwargs["system"]
        # Tools should be the same in both calls.
        assert calls[0].kwargs["tools"] == calls[1].kwargs["tools"]
        # Model should be the same.
        assert calls[0].kwargs["model"] == calls[1].kwargs["model"]

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_tool_result_ids_match_tool_use_ids(
        self,
        mock_cls: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Each tool_result's tool_use_id must match the corresponding tool_use block's id."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        (tmp_path / "file.txt").write_text("content\n")

        unique_id = "toolu_unique_abc123"
        turn1 = _api_response(
            content=[_tool_use_block(unique_id, "read_file", {"path": "file.txt"})],
            stop_reason="tool_use",
        )
        turn2 = _api_response(
            content=[_text_block(_json_text("Got it."))],
        )

        mock_client.messages.create.side_effect = [turn1, turn2]

        interpret_message_with_tools(
            text="read file.txt",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        second_call_msgs = mock_client.messages.create.call_args_list[1].kwargs["messages"]
        tool_result = second_call_msgs[2]["content"][0]
        assert tool_result["tool_use_id"] == unique_id
