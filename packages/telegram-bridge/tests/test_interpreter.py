"""Tests for the Claude-powered message interpreter."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from telegram_bridge.interpreter import (
    _OPUS_MAX_TOKENS,
    _OPUS_SYSTEM_PROMPT,
    _SYSTEM_PROMPT,
    ALLOWED_PRIORITIES,
    KNOWN_ACTION_TYPES,
    OPUS_MODEL,
    InterpretedMessage,
    RateLimiter,
    RateLimitError,
    _client_cache,
    _extract_json_object,
    _parse_response,
    _validate_action,
    build_dispatcher_status,
    clear_client_cache,
    get_client,
    interpret_message,
    interpret_message_with_tools,
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


# ── build_dispatcher_status() ──────────────────────────────────────────


class TestBuildOrchestratorStatus:
    def test_defaults(self) -> None:
        status = build_dispatcher_status()
        assert status["active_agents"] == []
        assert status["open_prs"] == []
        assert status["recently_completed"] == []
        assert status["queue"] == []
        assert status["paused"] is False
        assert status["stopped_issues"] == []
        assert status["prs_since_last_audit"] == 0
        assert status["session_number"] == 0
        assert "updated_at" in status

    def test_with_data(self) -> None:
        agents = [{"worker": 1, "issue": 42, "phase": "ci-watch"}]
        prs = [{"number": 100, "ci_status": "green"}]
        status = build_dispatcher_status(
            active_agents=agents,
            open_prs=prs,
            paused=True,
            stopped_issues=[99],
            prs_since_last_audit=15,
            session_number=3,
        )
        assert status["active_agents"] == agents
        assert status["open_prs"] == prs
        assert status["paused"] is True
        assert status["stopped_issues"] == [99]
        assert status["prs_since_last_audit"] == 15
        assert status["session_number"] == 3
        assert "updated_at" in status

    def test_updated_at_is_iso_format(self) -> None:
        import datetime

        status = build_dispatcher_status()
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

        status = build_dispatcher_status(
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
    def test_client_reused_across_calls(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps({"reply": "ok", "actions": []})
        )

        interpret_message(text="hi", api_key="test-key", rate_limiter=None)
        interpret_message(text="hi again", api_key="test-key", rate_limiter=None)

        # Client constructor should only be called once — reused from cache.
        mock_anthropic_cls.assert_called_once_with(api_key="test-key")
        assert mock_client.messages.create.call_count == 2

    def test_explicit_client_bypasses_cache(self) -> None:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps({"reply": "ok", "actions": []})
        )

        interpret_message(text="hi", client=mock_client, rate_limiter=None)

        mock_client.messages.create.assert_called_once()
        # Cache should remain empty — explicit client is not cached.
        assert len(_client_cache) == 0

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


# ── get_client() / clear_client_cache() ────────────────────────────────


class TestGetClient:
    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_returns_cached_client(self, mock_anthropic_cls: MagicMock) -> None:
        client1 = get_client(api_key="key-a")
        client2 = get_client(api_key="key-a")
        assert client1 is client2
        mock_anthropic_cls.assert_called_once_with(api_key="key-a")

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_different_keys_get_different_clients(self, mock_anthropic_cls: MagicMock) -> None:
        get_client(api_key="key-a")
        get_client(api_key="key-b")
        # Two distinct clients should be created — one per key.
        assert mock_anthropic_cls.call_count == 2
        # Verify both keys were used.
        calls = mock_anthropic_cls.call_args_list
        assert calls[0].kwargs["api_key"] == "key-a"
        assert calls[1].kwargs["api_key"] == "key-b"

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_none_key_uses_env_default(self, mock_anthropic_cls: MagicMock) -> None:
        get_client(api_key=None)
        mock_anthropic_cls.assert_called_once_with(api_key=None)

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_clear_cache_removes_all_clients(self, mock_anthropic_cls: MagicMock) -> None:
        get_client(api_key="key-a")
        get_client(api_key="key-b")
        assert len(_client_cache) == 2

        clear_client_cache()
        assert len(_client_cache) == 0

        # Next call should create a new client.
        get_client(api_key="key-a")
        assert mock_anthropic_cls.call_count == 3  # 2 original + 1 new


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
        discuss_pos = _SYSTEM_PROMPT.index("Requires codebase access")
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

    def test_has_confidence_threshold(self) -> None:
        # The prompt should include a confidence threshold section.
        assert "Confidence threshold" in _SYSTEM_PROMPT
        assert "HIGH CONFIDENCE" in _SYSTEM_PROMPT

    def test_never_ask_for_clarification(self) -> None:
        # The prompt should explicitly forbid asking for clarification.
        assert "NEVER ask the user for clarification" in _SYSTEM_PROMPT

    def test_never_say_i_dont_know(self) -> None:
        # The prompt should explicitly forbid "I don't know" responses.
        assert 'NEVER say "I don\'t know"' in _SYSTEM_PROMPT

    def test_never_guess(self) -> None:
        # The prompt should explicitly forbid guessing.
        assert "NEVER guess" in _SYSTEM_PROMPT

    def test_forward_when_uncertain(self) -> None:
        # The prompt should instruct forwarding when uncertain.
        prompt_lower = _SYSTEM_PROMPT.lower()
        assert "when uncertain" in prompt_lower or "when in doubt" in prompt_lower

    def test_confidence_before_decision_tree(self) -> None:
        # The confidence threshold section should appear before the decision tree
        # to set the mindset before individual rules.
        confidence_pos = _SYSTEM_PROMPT.index("Confidence threshold")
        decision_pos = _SYSTEM_PROMPT.index("Deciding when to forward vs. answer directly")
        assert confidence_pos < decision_pos

    def test_no_ask_clarification_guideline_removed(self) -> None:
        # The old guideline about asking for clarification should be gone.
        assert "ask for clarification in the reply" not in _SYSTEM_PROMPT


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


# ── Opus interpreter constants ─────────────────────────────────────────


class TestOpusConstants:
    def test_opus_model_is_opus(self) -> None:
        assert "opus" in OPUS_MODEL.lower()

    def test_opus_max_tokens_is_larger(self) -> None:
        assert _OPUS_MAX_TOKENS >= 2048

    def test_opus_system_prompt_has_tool_instructions(self) -> None:
        assert "read-only" in _OPUS_SYSTEM_PROMPT.lower() or "Read" in _OPUS_SYSTEM_PROMPT
        assert "tool" in _OPUS_SYSTEM_PROMPT.lower()

    def test_opus_system_prompt_has_action_format(self) -> None:
        """The Opus prompt should still describe the action JSON format."""
        assert '"actions"' in _OPUS_SYSTEM_PROMPT
        assert "start" in _OPUS_SYSTEM_PROMPT
        assert "pause" in _OPUS_SYSTEM_PROMPT

    def test_opus_system_prompt_has_formatting_rules(self) -> None:
        assert "plain text" in _OPUS_SYSTEM_PROMPT.lower()


# ── interpret_message_with_tools() ─────────────────────────────────────


def _make_tool_use_block(tool_id: str, name: str, tool_input: dict[str, object]) -> MagicMock:
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = tool_input
    return block


def _make_text_block(text: str) -> MagicMock:
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


class TestInterpretMessageWithTools:
    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_simple_reply_no_tools(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """Opus agent can answer without using any tools."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Response that ends immediately (no tool use).
        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [
            _make_text_block(
                json.dumps({"reply": "The project is a legal research platform.", "actions": []})
            )
        ]
        mock_client.messages.create.return_value = response

        result = interpret_message_with_tools(
            text="What is this project?",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert isinstance(result, InterpretedMessage)
        assert "legal research" in result.reply.lower()
        assert result.actions == []
        mock_client.messages.create.assert_called_once()

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_tool_use_then_answer(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """Opus agent uses a tool then provides a final answer."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Create a test file for the tool to read.
        (tmp_path / "README.md").write_text("# Judgemind\nA legal research platform.\n")

        # First response: tool use.
        tool_response = MagicMock()
        tool_response.stop_reason = "tool_use"
        tool_response.content = [_make_tool_use_block("call_1", "read_file", {"path": "README.md"})]

        # Second response: final answer.
        final_response = MagicMock()
        final_response.stop_reason = "end_turn"
        final_response.content = [
            _make_text_block(
                json.dumps(
                    {
                        "reply": "The README says Judgemind is a legal research platform.",
                        "actions": [],
                    }
                )
            )
        ]

        mock_client.messages.create.side_effect = [tool_response, final_response]

        result = interpret_message_with_tools(
            text="What does the README say?",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert "legal research" in result.reply.lower()
        assert result.actions == []
        # Two API calls: one for tool use, one for final answer.
        assert mock_client.messages.create.call_count == 2

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_tool_results_passed_back(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """Verify tool results are correctly passed back in the conversation."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        (tmp_path / "test.py").write_text("def hello(): pass\n")

        tool_response = MagicMock()
        tool_response.stop_reason = "tool_use"
        tool_response.content = [_make_tool_use_block("call_1", "read_file", {"path": "test.py"})]

        final_response = MagicMock()
        final_response.stop_reason = "end_turn"
        final_response.content = [
            _make_text_block(json.dumps({"reply": "Found hello function.", "actions": []}))
        ]

        mock_client.messages.create.side_effect = [tool_response, final_response]

        interpret_message_with_tools(
            text="show test.py",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        # Check the second call includes tool results.
        second_call = mock_client.messages.create.call_args_list[1]
        messages = second_call.kwargs["messages"]
        # Should be: user, assistant (tool use), user (tool result)
        assert len(messages) == 3
        assert messages[2]["role"] == "user"
        # Tool result should contain the file content.
        tool_result_content = messages[2]["content"]
        assert len(tool_result_content) == 1
        assert tool_result_content[0]["type"] == "tool_result"
        assert "hello" in tool_result_content[0]["content"]

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_actions_still_forwarded(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """Opus agent can still return orchestrator actions."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [
            _make_text_block(
                json.dumps(
                    {
                        "reply": "Pausing the orchestrator.",
                        "actions": [{"type": "pause"}],
                    }
                )
            )
        ]
        mock_client.messages.create.return_value = response

        result = interpret_message_with_tools(
            text="pause",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert len(result.actions) == 1
        assert result.actions[0]["type"] == "pause"

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_rate_limiter_applied(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """Rate limiter blocks excessive calls."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [_make_text_block(json.dumps({"reply": "ok", "actions": []}))]
        mock_client.messages.create.return_value = response

        limiter = RateLimiter(max_calls=1, window_seconds=60.0)

        interpret_message_with_tools(
            text="hi",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=limiter,
        )

        with pytest.raises(RateLimitError):
            interpret_message_with_tools(
                text="hi again",
                repo_root=tmp_path,
                api_key="test-key",
                rate_limiter=limiter,
            )

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_prompt_caching_enabled(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """System prompt uses cache_control for prompt caching."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [_make_text_block(json.dumps({"reply": "ok", "actions": []}))]
        mock_client.messages.create.return_value = response

        interpret_message_with_tools(
            text="hi",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        call_args = mock_client.messages.create.call_args
        system_blocks = call_args.kwargs["system"]
        assert isinstance(system_blocks, list)
        assert len(system_blocks) >= 1
        assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_tools_passed_to_api(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """Tool definitions are passed to the API call."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [_make_text_block(json.dumps({"reply": "ok", "actions": []}))]
        mock_client.messages.create.return_value = response

        interpret_message_with_tools(
            text="hi",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        call_args = mock_client.messages.create.call_args
        tools = call_args.kwargs["tools"]
        assert isinstance(tools, list)
        tool_names = {t["name"] for t in tools}
        assert "read_file" in tool_names
        assert "grep_files" in tool_names
        assert "run_shell_command" in tool_names

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_max_tool_rounds_respected(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """Agent stops after max_tool_rounds even if model keeps calling tools."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        # Every response is a tool use — should stop after max rounds.
        tool_response = MagicMock()
        tool_response.stop_reason = "tool_use"
        tool_response.content = [_make_tool_use_block("call_x", "read_file", {"path": "nope.txt"})]

        # After max rounds, the last response is used as-is.
        # Since it has no text, we get a fallback.
        mock_client.messages.create.return_value = tool_response

        result = interpret_message_with_tools(
            text="loop forever",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
            max_tool_rounds=3,
        )

        # Should have called the API 3 times (max rounds).
        assert mock_client.messages.create.call_count == 3
        # Should get a fallback reply since no text block was produced.
        assert "couldn't formulate" in result.reply.lower() or "noted" in result.reply.lower()

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_uses_opus_model_by_default(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """Default model is Opus."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [_make_text_block(json.dumps({"reply": "ok", "actions": []}))]
        mock_client.messages.create.return_value = response

        interpret_message_with_tools(
            text="hi",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        call_args = mock_client.messages.create.call_args
        assert "opus" in call_args.kwargs["model"].lower()

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_orchestrator_status_in_user_message(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """Orchestrator status is included in the user message."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [_make_text_block(json.dumps({"reply": "2 agents.", "actions": []}))]
        mock_client.messages.create.return_value = response

        status = build_dispatcher_status(active_agents=[{"worker": 1}, {"worker": 2}])

        interpret_message_with_tools(
            text="how many agents?",
            repo_root=tmp_path,
            orchestrator_status=status,
            api_key="test-key",
            rate_limiter=None,
        )

        call_args = mock_client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "Orchestrator Status" in user_msg

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_no_text_in_response_gives_fallback(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """If no text block in response, return a fallback message."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = []  # No content blocks at all.
        mock_client.messages.create.return_value = response

        result = interpret_message_with_tools(
            text="hello",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert "couldn't formulate" in result.reply.lower() or "noted" in result.reply.lower()
        assert result.actions == []

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_multiple_text_blocks_json_in_last(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """When the model outputs preamble in one text block and JSON in another,
        the JSON block should be used as the reply (not the concatenation)."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [
            _make_text_block("Let me think about this and provide an answer:"),
            _make_text_block(
                json.dumps(
                    {
                        "reply": "The answer is 42.",
                        "actions": [],
                    }
                )
            ),
        ]
        mock_client.messages.create.return_value = response

        result = interpret_message_with_tools(
            text="What is the answer?",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert result.reply == "The answer is 42."
        assert result.actions == []

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_preamble_then_json_single_block(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """When the model outputs preamble text followed by JSON in a single
        text block, the JSON should be extracted."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        json_part = json.dumps({"reply": "Here is the info.", "actions": []})
        combined = f"Now I have a clear understanding. Let me provide the answer:\n{json_part}"
        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [_make_text_block(combined)]
        mock_client.messages.create.return_value = response

        result = interpret_message_with_tools(
            text="Tell me about this",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        assert result.reply == "Here is the info."
        assert result.actions == []

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_raw_json_never_leaked_as_reply(self, mock_cls: MagicMock, tmp_path: Path) -> None:
        """The raw JSON response should never be sent as-is to the user."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        json_str = json.dumps(
            {"reply": "I'll file an issue about the truncated responses.", "actions": []}
        )
        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [_make_text_block(json_str)]
        mock_client.messages.create.return_value = response

        result = interpret_message_with_tools(
            text="File a bug",
            repo_root=tmp_path,
            api_key="test-key",
            rate_limiter=None,
        )

        # The reply should be the extracted text, not raw JSON.
        assert result.reply == "I'll file an issue about the truncated responses."
        assert "{" not in result.reply
        assert "actions" not in result.reply


# ── _extract_json_object() ─────────────────────────────────────────────


class TestExtractJsonObject:
    def test_plain_json(self) -> None:
        text = '{"reply": "hello", "actions": []}'
        result = _extract_json_object(text)
        assert result is not None
        assert result["reply"] == "hello"

    def test_json_with_preamble(self) -> None:
        text = 'Here is the answer:\n{"reply": "hello", "actions": []}'
        result = _extract_json_object(text)
        assert result is not None
        assert result["reply"] == "hello"

    def test_json_with_trailing_text(self) -> None:
        text = '{"reply": "hello", "actions": []}\nSome trailing text'
        result = _extract_json_object(text)
        assert result is not None
        assert result["reply"] == "hello"

    def test_json_with_nested_braces(self) -> None:
        text = '{"reply": "use {braces} carefully", "actions": []}'
        result = _extract_json_object(text)
        assert result is not None
        assert result["reply"] == "use {braces} carefully"

    def test_json_with_escaped_quotes(self) -> None:
        text = '{"reply": "He said \\"hello\\"", "actions": []}'
        result = _extract_json_object(text)
        assert result is not None
        assert 'He said "hello"' in result["reply"]

    def test_no_json_returns_none(self) -> None:
        assert _extract_json_object("just plain text") is None

    def test_empty_string_returns_none(self) -> None:
        assert _extract_json_object("") is None

    def test_incomplete_json_returns_none(self) -> None:
        assert _extract_json_object('{"reply": "hello"') is None

    def test_array_not_returned(self) -> None:
        """Arrays are not dicts and should not be returned."""
        assert _extract_json_object("[1, 2, 3]") is None

    def test_preamble_with_actions(self) -> None:
        text = (
            "Let me provide the answer:\n"
            '{"reply": "Starting #42.", "actions": [{"type": "start", "issue": 42}]}'
        )
        result = _extract_json_object(text)
        assert result is not None
        assert result["reply"] == "Starting #42."
        assert len(result["actions"]) == 1
        assert result["actions"][0]["type"] == "start"


# ── _parse_response() with preamble ───────────────────────────────────


class TestParseResponseWithPreamble:
    def test_preamble_before_json(self) -> None:
        """JSON embedded after preamble text should be extracted."""
        text = 'Now let me provide the answer:\n{"reply": "42", "actions": []}'
        result = _parse_response(text)
        assert result["reply"] == "42"

    def test_preamble_with_code_fence(self) -> None:
        """Code-fenced JSON should still work (original behavior)."""
        text = '```json\n{"reply": "hi", "actions": []}\n```'
        result = _parse_response(text)
        assert result["reply"] == "hi"

    def test_plain_json_still_works(self) -> None:
        text = '{"reply": "hello", "actions": []}'
        result = _parse_response(text)
        assert result["reply"] == "hello"

    def test_no_json_falls_back_to_raw_text(self) -> None:
        text = "Just a plain text response with no JSON at all."
        result = _parse_response(text)
        assert result["reply"] == text
        assert result["actions"] == []

    def test_json_without_reply_key_falls_back(self) -> None:
        """JSON that doesn't have a 'reply' key should fall back to raw text."""
        text = 'Some preamble\n{"data": "no reply key here"}'
        result = _parse_response(text)
        # Falls back because embedded JSON lacks "reply" key.
        assert result["actions"] == []
