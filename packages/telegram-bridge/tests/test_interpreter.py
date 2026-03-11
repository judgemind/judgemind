"""Tests for the Claude-powered message interpreter."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from telegram_bridge.interpreter import (
    InterpretedMessage,
    RateLimiter,
    RateLimitError,
    _parse_response,
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
