"""Tier 1 integration tests — validate the full interpreter-to-payload pipeline.

These tests run in CI on every telegram-bridge change (~5 s, no external
dependencies).  They exercise the interpreter + formatting + validation chain
end-to-end using mocked AWS and Anthropic services, verifying that the payloads
produced would be accepted by the Telegram Bot API.

See issue #737 for the two-tier testing design.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from telegram_bridge.formatting import linkify_github_refs
from telegram_bridge.interpreter import (
    build_dispatcher_status,
    interpret_message,
)
from telegram_bridge.validation import (
    validate_github_links,
    validate_html_round_trip,
    validate_telegram_payload,
)

# ── Payload validation helpers ───────────────────────────────────────────


class TestValidateTelegramPayload:
    """Unit tests for the shared payload validation function."""

    def test_valid_html_payload(self) -> None:
        payload = {
            "chat_id": 12345,
            "text": "Hello &amp; welcome!",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        result = validate_telegram_payload(payload)
        assert result.valid is True
        assert result.errors == []

    def test_missing_chat_id(self) -> None:
        payload = {"text": "hello"}
        result = validate_telegram_payload(payload)
        assert result.valid is False
        assert any("chat_id" in e for e in result.errors)

    def test_missing_text(self) -> None:
        payload = {"chat_id": 12345}
        result = validate_telegram_payload(payload)
        assert result.valid is False
        assert any("text" in e for e in result.errors)

    def test_empty_text(self) -> None:
        payload = {"chat_id": 12345, "text": ""}
        result = validate_telegram_payload(payload)
        assert result.valid is False

    def test_unescaped_angle_brackets_in_html(self) -> None:
        payload = {
            "chat_id": 12345,
            "text": "Hello <world>",
            "parse_mode": "HTML",
        }
        result = validate_telegram_payload(payload)
        assert result.valid is False
        assert any("angle bracket" in e.lower() for e in result.errors)

    def test_unescaped_ampersand_in_html(self) -> None:
        payload = {
            "chat_id": 12345,
            "text": "A & B",
            "parse_mode": "HTML",
        }
        result = validate_telegram_payload(payload)
        assert result.valid is False
        assert any("&" in e for e in result.errors)

    def test_escaped_entities_pass(self) -> None:
        payload = {
            "chat_id": 12345,
            "text": "A &amp; B &lt;done&gt;",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        result = validate_telegram_payload(payload)
        assert result.valid is True

    def test_allowed_html_tags_pass(self) -> None:
        payload = {
            "chat_id": 12345,
            "text": '<b>Bold</b> and <a href="https://example.com">link</a>',
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        result = validate_telegram_payload(payload)
        assert result.valid is True

    def test_no_parse_mode_skips_html_validation(self) -> None:
        payload = {
            "chat_id": 12345,
            "text": "Raw <stuff> & things",
            "disable_web_page_preview": True,
        }
        result = validate_telegram_payload(payload)
        assert result.valid is True

    def test_link_preview_warning(self) -> None:
        payload = {
            "chat_id": 12345,
            "text": "hello",
        }
        result = validate_telegram_payload(payload)
        assert result.valid is True
        assert any("link preview" in w.lower() for w in result.warnings)


class TestValidateGithubLinks:
    def test_finds_issue_links(self) -> None:
        text = '<a href="https://github.com/judgemind/judgemind/issues/42">#42</a>'
        assert validate_github_links(text) == [42]

    def test_finds_pr_links(self) -> None:
        text = '<a href="https://github.com/judgemind/judgemind/pull/100">PR #100</a>'
        assert validate_github_links(text) == [100]

    def test_finds_multiple(self) -> None:
        text = (
            '<a href="https://github.com/judgemind/judgemind/issues/1">#1</a> and '
            '<a href="https://github.com/judgemind/judgemind/pull/2">PR #2</a>'
        )
        assert validate_github_links(text) == [1, 2]

    def test_no_links(self) -> None:
        assert validate_github_links("plain text") == []


class TestValidateHtmlRoundTrip:
    def test_plain_text_passes(self) -> None:
        result = validate_html_round_trip("hello", "hello")
        assert result.valid is True

    def test_issue_ref_linkified(self) -> None:
        raw = "Fixed #42 today"
        formatted = linkify_github_refs(raw)
        result = validate_html_round_trip(raw, formatted)
        assert result.valid is True
        assert not result.warnings

    def test_unlinkified_ref_warns(self) -> None:
        raw = "Fixed #42 today"
        formatted = "Fixed #42 today"  # Not linkified
        result = validate_html_round_trip(raw, formatted)
        assert any("#42" in w for w in result.warnings)

    def test_special_chars_escaped(self) -> None:
        raw = "A < B & C > D"
        formatted = linkify_github_refs(raw)
        result = validate_html_round_trip(raw, formatted)
        assert result.valid is True


# ── Tier 1: Full interpreter-to-payload pipeline ────────────────────────


def _make_mock_response(text: str) -> MagicMock:
    """Create a mock Anthropic API response."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


class TestInterpreterToPayloadPipeline:
    """End-to-end integration: interpret a message, format the reply,
    build a sendMessage payload, and validate it would succeed."""

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_status_reply_produces_valid_payload(self, mock_cls: MagicMock) -> None:
        """A 'status' message with orchestrator context should produce a valid payload."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps(
                {
                    "reply": "2 agents are running. Worker-1 is on #42 (ci-watch) "
                    "and worker-2 is on #99 (implementing).",
                    "actions": [],
                }
            )
        )

        status = build_dispatcher_status(
            active_agents=[
                {"worker": 1, "issue": 42, "phase": "ci-watch"},
                {"worker": 2, "issue": 99, "phase": "implementing"},
            ],
            paused=False,
        )

        result = interpret_message(
            text="status",
            orchestrator_status=status,
            api_key="test-key",
            rate_limiter=None,
        )

        # Format the reply as a Telegram payload.
        formatted = linkify_github_refs(result.reply)
        payload = {
            "chat_id": 12345,
            "text": formatted,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        validation = validate_telegram_payload(payload)
        assert validation.valid is True, f"Validation errors: {validation.errors}"

        # The reply mentions issue numbers — verify they are linkified.
        linked = validate_github_links(formatted)
        assert 42 in linked
        assert 99 in linked

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_start_action_produces_valid_payload(self, mock_cls: MagicMock) -> None:
        """A 'start #42' message should produce a valid payload with a linked issue."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps(
                {
                    "reply": "Acknowledged, starting work on #42.",
                    "actions": [{"type": "start", "issue": 42}],
                }
            )
        )

        result = interpret_message(
            text="start #42",
            api_key="test-key",
            rate_limiter=None,
        )

        formatted = linkify_github_refs(result.reply)
        payload = {
            "chat_id": 12345,
            "text": formatted,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        validation = validate_telegram_payload(payload)
        assert validation.valid is True, f"Validation errors: {validation.errors}"

        # Issue #42 should be linkified.
        assert 42 in validate_github_links(formatted)

        # Action should be correctly parsed.
        assert len(result.actions) == 1
        assert result.actions[0]["type"] == "start"
        assert result.actions[0]["issue"] == 42

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_reply_with_special_chars_is_safe(self, mock_cls: MagicMock) -> None:
        """Reply containing <, >, & should be escaped in the formatted output."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps(
                {
                    "reply": "The condition A < B & C > D applies to issue #10.",
                    "actions": [],
                }
            )
        )

        result = interpret_message(
            text="explain the condition",
            api_key="test-key",
            rate_limiter=None,
        )

        formatted = linkify_github_refs(result.reply)
        payload = {
            "chat_id": 12345,
            "text": formatted,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        validation = validate_telegram_payload(payload)
        assert validation.valid is True, f"Validation errors: {validation.errors}"

        # Special chars should be escaped.
        assert "&lt;" in formatted
        assert "&amp;" in formatted
        assert "&gt;" in formatted

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_pause_resume_payloads_valid(self, mock_cls: MagicMock) -> None:
        """Pause and resume replies should produce valid payloads."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        for action_type, reply_text in [
            ("pause", "Orchestrator paused. No new work will be spawned."),
            ("resume", "Orchestrator resumed. Will spawn issues normally."),
        ]:
            mock_client.messages.create.return_value = _make_mock_response(
                json.dumps(
                    {
                        "reply": reply_text,
                        "actions": [{"type": action_type}],
                    }
                )
            )

            result = interpret_message(
                text=action_type,
                api_key="test-key",
                rate_limiter=None,
            )

            formatted = linkify_github_refs(result.reply)
            payload = {
                "chat_id": 12345,
                "text": formatted,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }

            validation = validate_telegram_payload(payload)
            assert validation.valid is True, (
                f"Validation errors for {action_type}: {validation.errors}"
            )

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_multiple_issue_refs_all_linkified(self, mock_cls: MagicMock) -> None:
        """A reply referencing multiple issues should linkify all of them."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps(
                {
                    "reply": "Working on #42, #99, and PR #100 is ready to merge.",
                    "actions": [],
                }
            )
        )

        result = interpret_message(
            text="what's happening?",
            api_key="test-key",
            rate_limiter=None,
        )

        formatted = linkify_github_refs(result.reply)
        linked = validate_github_links(formatted)
        assert 42 in linked
        assert 99 in linked
        assert 100 in linked

        # The full round-trip should also be valid.
        round_trip = validate_html_round_trip(result.reply, formatted)
        assert round_trip.valid is True

    @patch("telegram_bridge.interpreter.anthropic.Anthropic")
    def test_malformed_json_response_still_produces_valid_payload(
        self, mock_cls: MagicMock
    ) -> None:
        """When the interpreter returns non-JSON, the fallback reply should still
        be safe for Telegram HTML."""
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "I'm not sure what you mean. Please try again."
        )

        result = interpret_message(
            text="asdf",
            api_key="test-key",
            rate_limiter=None,
        )

        formatted = linkify_github_refs(result.reply)
        payload = {
            "chat_id": 12345,
            "text": formatted,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        validation = validate_telegram_payload(payload)
        assert validation.valid is True, f"Validation errors: {validation.errors}"


# ── Tier 1: Status card formatting validation ───────────────────────────


class TestStatusCardValidation:
    """Validate that status cards produced by the formatting module are
    safe for Telegram HTML parse mode."""

    def test_complete_status_card(self) -> None:
        from telegram_bridge.formatting import format_status_card

        card = format_status_card(task="#476", state="complete", details="PR #482 merged.")
        payload = {
            "chat_id": 12345,
            "text": card,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        result = validate_telegram_payload(payload)
        assert result.valid is True, f"Validation errors: {result.errors}"

        # Issue and PR should be linked.
        linked = validate_github_links(card)
        assert 476 in linked
        assert 482 in linked

    def test_failed_status_card(self) -> None:
        from telegram_bridge.formatting import format_status_card

        card = format_status_card(task="#99", state="failed", details="CI red on #99.")
        payload = {
            "chat_id": 12345,
            "text": card,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        result = validate_telegram_payload(payload)
        assert result.valid is True, f"Validation errors: {result.errors}"

    def test_status_card_with_special_chars(self) -> None:
        from telegram_bridge.formatting import format_status_card

        card = format_status_card(
            task="#10",
            state="in_progress",
            details="Building A & B for <client>",
        )
        payload = {
            "chat_id": 12345,
            "text": card,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        result = validate_telegram_payload(payload)
        assert result.valid is True, f"Validation errors: {result.errors}"
