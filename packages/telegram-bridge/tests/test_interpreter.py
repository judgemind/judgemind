"""Tests for the Claude-powered message interpreter."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from telegram_bridge.interpreter import (
    _SYSTEM_PROMPT,
    ALLOWED_PRIORITIES,
    KNOWN_ACTION_TYPES,
    InterpretedMessage,
    RateLimiter,
    RateLimitError,
    _parse_response,
    _validate_action,
    build_orchestrator_status,
    interpret_message,
)

# ── _parse_response() ───────────────────────────────────────────────────


class TestParseResponse:
    def test_valid_json(self) -> None:
        result = _parse_response('{"reply": "hello", "actions": []}')
        assert result["reply"] == "hello"
        assert result["actions"] == []

    def test_json_in_code_fence(self) -> None:
        text = '```json\n{"reply": "hi", "actions": []}\n```'
        result = _parse_response(text)
        assert result["reply"] == "hi"
        assert result["actions"] == []

    def test_json_in_plain_code_fence(self) -> None:
        text = '```\n{"reply": "hi", "actions": []}\n```'
        result = _parse_response(text)
        assert result["reply"] == "hi"

    def test_invalid_json_returns_raw_text(self) -> None:
        result = _parse_response("not valid json at all")
        assert result["reply"] == "not valid json at all"
        assert result["actions"] == []

    def test_actions_with_start(self) -> None:
        text = json.dumps(
            {
                "reply": "Starting issue 42.",
                "actions": [{"type": "start", "issue": 42}],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 1
        assert result["actions"][0]["type"] == "start"
        assert result["actions"][0]["issue"] == 42

    def test_actions_with_file_issue(self) -> None:
        text = json.dumps(
            {
                "reply": "Filing that issue.",
                "actions": [
                    {
                        "type": "file_issue",
                        "description": "OC scraper timing out",
                        "priority": "p2",
                        "labels": ["area/scraping"],
                    }
                ],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 1
        assert result["actions"][0]["type"] == "file_issue"
        assert result["actions"][0]["description"] == "OC scraper timing out"
        assert result["actions"][0]["priority"] == "p2"
        assert result["actions"][0]["labels"] == ["area/scraping"]

    def test_actions_with_discuss(self) -> None:
        text = json.dumps(
            {
                "reply": "Forwarding to orchestrator.",
                "actions": [
                    {
                        "type": "discuss",
                        "message": "Should we use Redis for caching?",
                    }
                ],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 1
        assert result["actions"][0]["type"] == "discuss"
        assert result["actions"][0]["message"] == "Should we use Redis for caching?"

    def test_actions_with_do(self) -> None:
        text = json.dumps(
            {
                "reply": "Checking CI now.",
                "actions": [
                    {
                        "type": "do",
                        "instruction": "Check if PR #738 CI passed and merge it",
                    }
                ],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 1
        assert result["actions"][0]["type"] == "do"
        assert result["actions"][0]["instruction"] == "Check if PR #738 CI passed and merge it"

    def test_actions_with_multiple(self) -> None:
        text = json.dumps(
            {
                "reply": "OK.",
                "actions": [
                    {"type": "pause"},
                    {"type": "stop", "issue": 99},
                ],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 2

    def test_invalid_actions_filtered(self) -> None:
        text = json.dumps(
            {
                "reply": "OK.",
                "actions": [
                    {"type": "pause"},
                    "not a dict",
                    {"no_type_key": True},
                ],
            }
        )
        result = _parse_response(text)
        # Only the valid action (with "type" key) should remain.
        assert len(result["actions"]) == 1
        assert result["actions"][0]["type"] == "pause"

    def test_non_dict_json(self) -> None:
        result = _parse_response('"just a string"')
        assert result["reply"] == "just a string"
        assert result["actions"] == []


# ── build_orchestrator_status() ──────────────────────────────────────────


class TestBuildOrchestratorStatus:
    def test_defaults(self) -> None:
        status = build_orchestrator_status()
        assert status["active_agents"] == []
        assert status["open_prs"] == []
        assert status["recently_completed"] == []
        assert status["queue"] == []
        assert status["paused"] is False
        assert status["stopped_issues"] == []
        assert "updated_at" in status

    def test_with_data(self) -> None:
        agents = [{"worker": 1, "issue": 42, "phase": "ci-watch"}]
        prs = [{"number": 100, "ci_status": "green"}]
        status = build_orchestrator_status(
            active_agents=agents,
            open_prs=prs,
            paused=True,
            stopped_issues=[99],
        )
        assert status["active_agents"] == agents
        assert status["open_prs"] == prs
        assert status["paused"] is True
        assert status["stopped_issues"] == [99]
        assert "updated_at" in status

    def test_updated_at_is_iso_format(self) -> None:
        import datetime

        status = build_orchestrator_status()
        # Should be parseable as an ISO-8601 datetime.
        parsed = datetime.datetime.fromisoformat(status["updated_at"])
        assert parsed.tzinfo is not None or "T" in status["updated_at"]


# ── interpret_message() ─────────────────────────────────────────────────


def _make_mock_response(text: str) -> MagicMock:
    """Create a mock Anthropic API response."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


class TestInterpretMessage:
    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_basic_reply(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps({"reply": "All systems running.", "actions": []})
        )

        result = interpret_message(text="status", api_key="test-key", rate_limiter=None)

        assert isinstance(result, InterpretedMessage)
        assert result.reply == "All systems running."
        assert result.actions == []
        mock_client.messages.create.assert_called_once()

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_with_actions(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps(
                {
                    "reply": "Starting issue 42.",
                    "actions": [{"type": "start", "issue": 42}],
                }
            )
        )

        result = interpret_message(text="please work on 42", api_key="test-key", rate_limiter=None)

        assert result.reply == "Starting issue 42."
        assert len(result.actions) == 1
        assert result.actions[0]["type"] == "start"
        assert result.actions[0]["issue"] == 42

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_with_orchestrator_status(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps({"reply": "2 agents running.", "actions": []})
        )

        status = build_orchestrator_status(
            active_agents=[
                {"worker": 1, "issue": 42},
                {"worker": 2, "issue": 99},
            ]
        )

        interpret_message(
            text="how many agents?",
            orchestrator_status=status,
            api_key="test-key",
            rate_limiter=None,
        )

        # Verify the orchestrator status was passed in the user message.
        call_args = mock_client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "orchestrator_status.json" in user_msg.lower() or "Orchestrator Status" in user_msg

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_api_key_passed(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps({"reply": "ok", "actions": []})
        )

        interpret_message(text="hi", api_key="my-secret-key", rate_limiter=None)

        mock_anthropic_cls.assert_called_once_with(api_key="my-secret-key")

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_code_fence_response(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # Model wraps response in code fences.
        mock_client.messages.create.return_value = _make_mock_response(
            '```json\n{"reply": "Paused.", "actions": [{"type": "pause"}]}\n```'
        )

        result = interpret_message(text="pause everything", api_key="test-key", rate_limiter=None)

        assert result.reply == "Paused."
        assert len(result.actions) == 1
        assert result.actions[0]["type"] == "pause"

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_non_json_response_handled(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # Model returns plain text instead of JSON.
        mock_client.messages.create.return_value = _make_mock_response(
            "I'm not sure what you mean."
        )

        result = interpret_message(text="blah", api_key="test-key", rate_limiter=None)

        # Should gracefully handle non-JSON by using raw text as reply.
        assert "not sure" in result.reply.lower()
        assert result.actions == []

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_uses_haiku_model_by_default(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps({"reply": "ok", "actions": []})
        )

        interpret_message(text="hi", api_key="test-key", rate_limiter=None)

        call_args = mock_client.messages.create.call_args
        assert "haiku" in call_args.kwargs["model"]

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_custom_model(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps({"reply": "ok", "actions": []})
        )

        interpret_message(
            text="hi", api_key="test-key", model="claude-sonnet-4-20250514", rate_limiter=None
        )

        call_args = mock_client.messages.create.call_args
        assert call_args.kwargs["model"] == "claude-sonnet-4-20250514"

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_rate_limiter_blocks_excessive_calls(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps({"reply": "ok", "actions": []})
        )

        limiter = RateLimiter(max_calls=1, window_seconds=60.0)

        # First call should succeed.
        interpret_message(text="hi", api_key="test-key", rate_limiter=limiter)

        # Second call should be rate-limited.
        with pytest.raises(RateLimitError):
            interpret_message(text="hi again", api_key="test-key", rate_limiter=limiter)

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_no_rate_limiter(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps({"reply": "ok", "actions": []})
        )

        # Both calls should succeed with no rate limiter.
        interpret_message(text="hi", api_key="test-key", rate_limiter=None)
        interpret_message(text="hi again", api_key="test-key", rate_limiter=None)

        assert mock_client.messages.create.call_count == 2


# ── RateLimiter ────────────────────────────────────────────────────────


class TestRateLimiter:
    def test_allows_within_limit(self) -> None:
        limiter = RateLimiter(max_calls=3, window_seconds=60.0)
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()
        # All three should succeed.

    def test_blocks_over_limit(self) -> None:
        limiter = RateLimiter(max_calls=2, window_seconds=60.0)
        limiter.acquire()
        limiter.acquire()
        with pytest.raises(RateLimitError) as exc_info:
            limiter.acquire()
        assert exc_info.value.retry_after > 0

    def test_window_expiry(self) -> None:
        limiter = RateLimiter(max_calls=1, window_seconds=0.1)
        limiter.acquire()
        # Wait for the window to expire.
        time.sleep(0.15)
        # Should succeed after window expires.
        limiter.acquire()

    def test_reset_clears_timestamps(self) -> None:
        limiter = RateLimiter(max_calls=1, window_seconds=60.0)
        limiter.acquire()
        limiter.reset()
        # Should succeed after reset.
        limiter.acquire()

    def test_retry_after_is_positive(self) -> None:
        limiter = RateLimiter(max_calls=1, window_seconds=30.0)
        limiter.acquire()
        with pytest.raises(RateLimitError) as exc_info:
            limiter.acquire()
        assert 0 < exc_info.value.retry_after <= 30.0


# ── _SYSTEM_PROMPT content ────────────────────────────────────────────


class TestSystemPromptContent:
    """Verify the system prompt includes the tightened forwarding criteria."""

    def test_has_decision_tree_section(self) -> None:
        assert "Deciding when to forward vs. answer directly" in _SYSTEM_PROMPT

    def test_prioritizes_status_context_answering(self) -> None:
        # The decision tree should instruct answering from status context first.
        assert "Answerable from the orchestrator status context?" in _SYSTEM_PROMPT

    def test_status_answer_comes_before_discuss(self) -> None:
        # The "answer from status" rule must appear before the "discuss" rule
        # to ensure the model checks status-answerable questions first.
        status_pos = _SYSTEM_PROMPT.index("Answerable from the orchestrator status context?")
        discuss_pos = _SYSTEM_PROMPT.index("Requires codebase access?")
        assert status_pos < discuss_pos

    def test_has_direct_reply_examples(self) -> None:
        # The prompt should include examples of questions answerable from status.
        assert "How many workers are active?" in _SYSTEM_PROMPT
        assert "What's the queue look like?" in _SYSTEM_PROMPT

    def test_has_discuss_examples(self) -> None:
        # The prompt should include examples of questions requiring codebase access.
        assert "Why is the OC scraper failing?" in _SYSTEM_PROMPT

    def test_has_do_examples(self) -> None:
        # The prompt should include examples of actions for the orchestrator.
        assert "Merge PR #750" in _SYSTEM_PROMPT

    def test_instructs_empty_actions_for_status_queries(self) -> None:
        # The prompt should explicitly say to use an empty actions array
        # for status-answerable questions.
        assert "empty `actions` array" in _SYSTEM_PROMPT

    def test_warns_against_forwarding_status_queries(self) -> None:
        # The prompt should explicitly warn against using discuss/do for
        # status-answerable questions.
        assert 'Do NOT forward these as "discuss" or "do"' in _SYSTEM_PROMPT


# ── System prompt passed to API ───────────────────────────────────────


class TestSystemPromptPassedToApi:
    """Verify the updated system prompt is actually sent to the Claude API."""

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_system_prompt_includes_decision_tree(self, mock_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps({"reply": "ok", "actions": []})
        )

        interpret_message(text="hi", api_key="test-key", rate_limiter=None)

        call_args = mock_client.messages.create.call_args
        system_prompt = call_args.kwargs["system"]
        assert "Deciding when to forward vs. answer directly" in system_prompt
        assert "Answerable from the orchestrator status context?" in system_prompt


# ── _validate_action() ──────────────────────────────────────────────────


class TestValidateAction:
    """Tests for individual action validation."""

    def test_valid_start_action(self) -> None:
        result = _validate_action({"type": "start", "issue": 42})
        assert result is not None
        assert result["type"] == "start"
        assert result["issue"] == 42

    def test_valid_stop_action(self) -> None:
        result = _validate_action({"type": "stop", "issue": 99})
        assert result is not None
        assert result["type"] == "stop"
        assert result["issue"] == 99

    def test_valid_pause_action(self) -> None:
        result = _validate_action({"type": "pause"})
        assert result is not None
        assert result["type"] == "pause"

    def test_valid_resume_action(self) -> None:
        result = _validate_action({"type": "resume"})
        assert result is not None
        assert result["type"] == "resume"

    def test_valid_file_issue_action(self) -> None:
        result = _validate_action(
            {"type": "file_issue", "description": "Bug report", "priority": "p1"}
        )
        assert result is not None
        assert result["type"] == "file_issue"
        assert result["description"] == "Bug report"
        assert result["priority"] == "p1"

    def test_valid_discuss_action(self) -> None:
        result = _validate_action({"type": "discuss", "message": "Architecture question"})
        assert result is not None
        assert result["message"] == "Architecture question"

    def test_valid_do_action(self) -> None:
        result = _validate_action({"type": "do", "instruction": "Check CI on PR #738"})
        assert result is not None
        assert result["instruction"] == "Check CI on PR #738"

    # ── Unknown / invalid types ──

    def test_unknown_type_dropped(self) -> None:
        assert _validate_action({"type": "explode"}) is None

    def test_non_dict_dropped(self) -> None:
        assert _validate_action("not a dict") is None

    def test_missing_type_dropped(self) -> None:
        assert _validate_action({"issue": 42}) is None

    def test_non_string_type_dropped(self) -> None:
        assert _validate_action({"type": 123}) is None

    # ── Missing required fields ──

    def test_start_without_issue_dropped(self) -> None:
        assert _validate_action({"type": "start"}) is None

    def test_stop_without_issue_dropped(self) -> None:
        assert _validate_action({"type": "stop"}) is None

    def test_file_issue_without_description_dropped(self) -> None:
        assert _validate_action({"type": "file_issue", "priority": "p2"}) is None

    def test_discuss_without_message_dropped(self) -> None:
        assert _validate_action({"type": "discuss"}) is None

    def test_do_without_instruction_dropped(self) -> None:
        assert _validate_action({"type": "do"}) is None

    # ── Wrong field types ──

    def test_start_with_string_issue_coerced(self) -> None:
        result = _validate_action({"type": "start", "issue": "42"})
        assert result is not None
        assert result["issue"] == 42

    def test_start_with_non_numeric_string_dropped(self) -> None:
        assert _validate_action({"type": "start", "issue": "not-a-number"}) is None

    def test_start_with_list_issue_dropped(self) -> None:
        assert _validate_action({"type": "start", "issue": [42]}) is None

    def test_file_issue_with_int_description_dropped(self) -> None:
        assert _validate_action({"type": "file_issue", "description": 123}) is None

    # ── Priority validation ──

    def test_file_issue_invalid_priority_normalized(self) -> None:
        result = _validate_action(
            {"type": "file_issue", "description": "Bug", "priority": "critical"}
        )
        assert result is not None
        assert result["priority"] == "p2"

    def test_file_issue_p0_normalized(self) -> None:
        """p0 is human-only and should be normalized to p2."""
        result = _validate_action({"type": "file_issue", "description": "Bug", "priority": "p0"})
        assert result is not None
        assert result["priority"] == "p2"

    def test_file_issue_missing_priority_defaults_to_p2(self) -> None:
        result = _validate_action({"type": "file_issue", "description": "Bug"})
        assert result is not None
        assert result["priority"] == "p2"

    def test_file_issue_valid_priorities_accepted(self) -> None:
        for p in ALLOWED_PRIORITIES:
            result = _validate_action({"type": "file_issue", "description": "Bug", "priority": p})
            assert result is not None
            assert result["priority"] == p

    # ── Optional fields ──

    def test_file_issue_with_labels(self) -> None:
        result = _validate_action(
            {
                "type": "file_issue",
                "description": "Bug",
                "priority": "p2",
                "labels": ["area/scraping"],
            }
        )
        assert result is not None
        assert result["labels"] == ["area/scraping"]

    def test_file_issue_labels_wrong_type_dropped(self) -> None:
        result = _validate_action(
            {
                "type": "file_issue",
                "description": "Bug",
                "priority": "p2",
                "labels": "area/scraping",
            }
        )
        assert result is None


# ── Schema-aware _parse_response() ──────────────────────────────────────


class TestParseResponseSchemaValidation:
    """Tests for _parse_response() with the new schema validation."""

    def test_unknown_action_type_filtered(self) -> None:
        text = json.dumps(
            {
                "reply": "OK.",
                "actions": [
                    {"type": "pause"},
                    {"type": "unknown_action"},
                ],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 1
        assert result["actions"][0]["type"] == "pause"

    def test_start_missing_issue_filtered(self) -> None:
        text = json.dumps(
            {
                "reply": "Starting...",
                "actions": [{"type": "start"}],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 0

    def test_stop_missing_issue_filtered(self) -> None:
        text = json.dumps(
            {
                "reply": "Stopping...",
                "actions": [{"type": "stop"}],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 0

    def test_file_issue_priority_normalized(self) -> None:
        text = json.dumps(
            {
                "reply": "Filing...",
                "actions": [{"type": "file_issue", "description": "Bug", "priority": "urgent"}],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 1
        assert result["actions"][0]["priority"] == "p2"

    def test_string_issue_coerced_to_int(self) -> None:
        text = json.dumps(
            {
                "reply": "Starting...",
                "actions": [{"type": "start", "issue": "42"}],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 1
        assert result["actions"][0]["issue"] == 42

    def test_multiple_actions_mixed_validity(self) -> None:
        text = json.dumps(
            {
                "reply": "OK.",
                "actions": [
                    {"type": "pause"},
                    {"type": "start"},  # missing issue - dropped
                    {"type": "stop", "issue": 99},
                    {"type": "banana"},  # unknown type - dropped
                    "not a dict",  # not a dict - dropped
                ],
            }
        )
        result = _parse_response(text)
        assert len(result["actions"]) == 2
        assert result["actions"][0]["type"] == "pause"
        assert result["actions"][1]["type"] == "stop"

    def test_all_known_types_present(self) -> None:
        """Verify the KNOWN_ACTION_TYPES set matches expected types."""
        expected = {"start", "stop", "pause", "resume", "file_issue", "discuss", "do"}
        assert KNOWN_ACTION_TYPES == expected
