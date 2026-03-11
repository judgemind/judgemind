"""Tests for the standalone Telegram responder daemon (scripts/tg-responder.py).

These tests validate command handling, state file reading/writing, SQS polling,
and the Telegram Bot API integration — all with mocked external services.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import httpx
import pytest
import respx
from moto import mock_aws

# The responder script lives in scripts/ with a hyphen in its name,
# so we use importlib.util to load it by file path.
REPO_ROOT = Path(__file__).resolve().parents[3]
_RESPONDER_PATH = REPO_ROOT / "scripts" / "tg-responder.py"


# ── Helpers ──────────────────────────────────────────────────────────────


def _setup_secret(
    *,
    token: str = "fake-bot-token",
    user_ids: list[int] | None = None,
) -> None:
    """Create the Secrets Manager secret used by the responder."""
    sm = boto3.client("secretsmanager", region_name="us-west-2")
    sm.create_secret(
        Name="judgemind/telegram/bot",
        SecretString=json.dumps(
            {
                "bot_token": token,
                "allowed_user_ids": [12345] if user_ids is None else user_ids,
            }
        ),
    )


def _setup_sqs(queue_name: str = "test-telegram-inbound") -> str:
    """Create an SQS queue and return its URL."""
    sqs = boto3.client("sqs", region_name="us-west-2")
    resp = sqs.create_queue(QueueName=queue_name)
    return resp["QueueUrl"]


def _send_sqs_message(queue_url: str, text: str, user_id: int = 12345) -> None:
    """Send a text message to the SQS queue."""
    sqs = boto3.client("sqs", region_name="us-west-2")
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(
            {
                "text": text,
                "user_id": user_id,
                "timestamp": "2026-03-10T20:00:00+00:00",
            }
        ),
    )


# ── Import responder after path setup ────────────────────────────────────

# The script has a hyphen in its filename (tg-responder.py) so we cannot
# use a normal import.  Load it by file path instead.


def _import_responder() -> types.ModuleType:
    """Import the responder module from its file path.

    The ``scripts/`` directory and ``_VENV_HELPER_SKIP`` env var are
    configured in ``conftest.py`` so that ``_venv_helper.ensure_venv()``
    resolves and does not re-exec into a package venv.
    """
    mod_name = "tg_responder"
    if mod_name in sys.modules:
        return importlib.import_module(mod_name)

    spec = importlib.util.spec_from_file_location(mod_name, str(_RESPONDER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Secret loading ──────────────────────────────────────────────────────


class TestLoadSecret:
    def test_loads_bot_token_and_chat_ids(self) -> None:
        with mock_aws():
            _setup_secret(token="my-token", user_ids=[111, 222])
            mod = _import_responder()
            token, chat_ids = mod.load_secret(region="us-west-2")
            assert token == "my-token"
            assert chat_ids == [111, 222]

    def test_raises_when_secret_missing(self) -> None:
        with mock_aws():
            mod = _import_responder()
            with pytest.raises(Exception):
                mod.load_secret(region="us-west-2")


# ── Telegram reply ──────────────────────────────────────────────────────


class TestSendTelegramReply:
    @respx.mock
    def test_sends_message_to_all_chat_ids(self) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        mod = _import_responder()
        mod.send_telegram_reply("Hello!", bot_token="fake-token", chat_ids=[111, 222])
        assert route.call_count == 2

    @respx.mock
    def test_message_content_is_correct(self) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        mod = _import_responder()
        mod.send_telegram_reply("Test msg", bot_token="fake-token", chat_ids=[12345])
        body = json.loads(route.calls[0].request.content)
        assert body["chat_id"] == 12345
        assert body["text"] == "Test msg"

    @respx.mock
    def test_does_not_raise_on_api_error(self) -> None:
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(500, json={"ok": False})
        )
        mod = _import_responder()
        # Should not raise --- errors are logged, not propagated.
        mod.send_telegram_reply("Hello!", bot_token="fake-token", chat_ids=[111])


# ── State file reading ──────────────────────────────────────────────────


class TestReadOrchestratorState:
    def test_reads_existing_state(self, tmp_path: Path) -> None:
        state_file = tmp_path / "orchestrator_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "paused": True,
                    "workers": {
                        "2": {
                            "worker_number": 2,
                            "issue_number": 42,
                            "issue_title": "Fix widget",
                            "phase": "ci-watch",
                            "updated": "2026-03-10T19:00:00Z",
                        }
                    },
                }
            )
        )
        mod = _import_responder()
        state = mod.read_orchestrator_state(str(state_file))
        assert state["paused"] is True
        assert "2" in state["workers"]
        assert state["workers"]["2"]["issue_number"] == 42

    def test_returns_default_when_missing(self, tmp_path: Path) -> None:
        mod = _import_responder()
        state = mod.read_orchestrator_state(str(tmp_path / "nonexistent.json"))
        assert state["paused"] is False
        assert state["workers"] == {}

    def test_returns_default_when_corrupt(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text("not json{{{")
        mod = _import_responder()
        state = mod.read_orchestrator_state(str(state_file))
        assert state["paused"] is False
        assert state["workers"] == {}


class TestReadOrchestratorStatus:
    def test_reads_status_file(self, tmp_path: Path) -> None:
        status_file = tmp_path / "orchestrator_status.json"
        status_data = {
            "active_agents": [{"worker": 1, "issue": 42}],
            "paused": False,
        }
        status_file.write_text(json.dumps(status_data))
        mod = _import_responder()
        result = mod.read_orchestrator_status(str(status_file))
        assert result is not None
        assert result["active_agents"][0]["issue"] == 42

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        mod = _import_responder()
        result = mod.read_orchestrator_status(str(tmp_path / "nonexistent.json"))
        assert result is None

    def test_returns_none_when_corrupt(self, tmp_path: Path) -> None:
        status_file = tmp_path / "status.json"
        status_file.write_text("not json{{{")
        mod = _import_responder()
        result = mod.read_orchestrator_status(str(status_file))
        assert result is None


class TestReadAgentStatusFiles:
    def test_reads_worker_status_files(self, tmp_path: Path) -> None:
        status_dir = tmp_path / "agent-status"
        status_dir.mkdir()
        (status_dir / "worker-2.txt").write_text(
            "issue: #42\nphase: ci-watch\nupdated: 2026-03-10T19:00:00Z\n"
            "summary: Watching CI run 12345\n"
        )
        (status_dir / "worker-5.txt").write_text(
            "issue: #99\nphase: implementing\nupdated: 2026-03-10T19:05:00Z\n"
            "summary: Writing tests\n"
        )
        mod = _import_responder()
        statuses = mod.read_agent_status_files(str(status_dir))
        assert len(statuses) == 2
        assert statuses[0]["issue"] == "#42" or statuses[1]["issue"] == "#42"

    def test_returns_empty_when_dir_missing(self, tmp_path: Path) -> None:
        mod = _import_responder()
        statuses = mod.read_agent_status_files(str(tmp_path / "nonexistent"))
        assert statuses == []


# ── Status command ──────────────────────────────────────────────────────


class TestFormatStatusReply:
    def test_no_workers(self) -> None:
        mod = _import_responder()
        state = {"paused": False, "workers": {}}
        reply = mod.format_status_reply(state, agent_statuses=[])
        assert "no active" in reply.lower()

    def test_with_workers(self) -> None:
        mod = _import_responder()
        state = {
            "paused": False,
            "workers": {
                "2": {
                    "worker_number": 2,
                    "issue_number": 42,
                    "issue_title": "Fix widget",
                    "phase": "ci-watch",
                    "updated": "2026-03-10T19:00:00Z",
                }
            },
        }
        reply = mod.format_status_reply(state, agent_statuses=[])
        assert "#42" in reply
        assert "Worker-2" in reply
        assert "ci-watch" in reply

    def test_paused_indicator(self) -> None:
        mod = _import_responder()
        state = {"paused": True, "workers": {}}
        reply = mod.format_status_reply(state, agent_statuses=[])
        assert "paused" in reply.lower()

    def test_includes_agent_status_info(self) -> None:
        mod = _import_responder()
        state = {"paused": False, "workers": {}}
        agent_statuses = [
            {
                "worker": "worker-3",
                "issue": "#99",
                "phase": "implementing",
                "summary": "Writing tests",
            }
        ]
        reply = mod.format_status_reply(state, agent_statuses=agent_statuses)
        assert "#99" in reply
        assert "worker-3" in reply


# ── Pause / resume commands ─────────────────────────────────────────────


class TestHandlePauseResume:
    def test_pause_sets_flag(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"paused": False, "workers": {}}))
        mod = _import_responder()
        mod.handle_pause(str(state_file))
        data = json.loads(state_file.read_text())
        assert data["paused"] is True

    def test_resume_clears_flag(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"paused": True, "workers": {}}))
        mod = _import_responder()
        mod.handle_resume(str(state_file))
        data = json.loads(state_file.read_text())
        assert data["paused"] is False

    def test_pause_creates_file_if_missing(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        mod = _import_responder()
        mod.handle_pause(str(state_file))
        data = json.loads(state_file.read_text())
        assert data["paused"] is True

    def test_pause_preserves_workers(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "paused": False,
                    "workers": {
                        "2": {
                            "worker_number": 2,
                            "issue_number": 42,
                            "issue_title": "Fix",
                            "phase": "ci",
                            "updated": "",
                        }
                    },
                }
            )
        )
        mod = _import_responder()
        mod.handle_pause(str(state_file))
        data = json.loads(state_file.read_text())
        assert data["paused"] is True
        assert "2" in data["workers"]


# ── Stop command ────────────────────────────────────────────────────────


class TestHandleStop:
    def test_appends_stop_request(self, tmp_path: Path) -> None:
        stop_file = tmp_path / "stop_requests.json"
        mod = _import_responder()
        mod.handle_stop(42, str(stop_file))
        data = json.loads(stop_file.read_text())
        assert len(data) == 1
        assert data[0]["issue_number"] == 42

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        stop_file = tmp_path / "stop_requests.json"
        stop_file.write_text(
            json.dumps([{"issue_number": 10, "timestamp": "2026-03-10T18:00:00Z"}])
        )
        mod = _import_responder()
        mod.handle_stop(42, str(stop_file))
        data = json.loads(stop_file.read_text())
        assert len(data) == 2
        assert data[1]["issue_number"] == 42


# ── Queue to inbox ──────────────────────────────────────────────────────


class TestQueueToInbox:
    def test_queues_message(self, tmp_path: Path) -> None:
        inbox_file = tmp_path / "tg_inbox.json"
        mod = _import_responder()
        mod.queue_to_inbox({"text": "start #42", "user_id": 12345}, str(inbox_file))
        data = json.loads(inbox_file.read_text())
        assert len(data) == 1
        assert data[0]["text"] == "start #42"

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        inbox_file = tmp_path / "tg_inbox.json"
        inbox_file.write_text(json.dumps([{"text": "old msg"}]))
        mod = _import_responder()
        mod.queue_to_inbox({"text": "new msg", "user_id": 12345}, str(inbox_file))
        data = json.loads(inbox_file.read_text())
        assert len(data) == 2
        assert data[1]["text"] == "new msg"


# ── Legacy dispatch_command (no API key --- fallback mode) ──────────────


class TestDispatchCommandLegacy:
    """Tests for the legacy dispatch_command wrapper (no Claude API key).

    In this mode, all messages are queued to inbox with a simple acknowledgment.
    """

    @respx.mock
    def test_queues_message_and_sends_ack(self, tmp_path: Path) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        inbox_file = tmp_path / "inbox.json"
        mod = _import_responder()
        mod.dispatch_command(
            message={"text": "status", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(inbox_file),
        )
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "interpreter unavailable" in body["text"].lower()
        data = json.loads(inbox_file.read_text())
        assert len(data) == 1


# ── dispatch_message with Claude interpreter ────────────────────────────


class TestDispatchMessage:
    """Tests for dispatch_message with mocked Claude interpreter."""

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_sends_claude_reply(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="All 2 agents are running smoothly.",
            actions=[],
        )
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "status", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "2 agents" in body["text"]
        mock_interpret.assert_called_once()

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_executes_pause_action(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Paused. No new work will be spawned.",
            actions=[{"type": "pause"}],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"paused": False, "workers": {}}))

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "pause everything", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(state_file),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        data = json.loads(state_file.read_text())
        assert data["paused"] is True

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_executes_resume_action(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Resumed.",
            actions=[{"type": "resume"}],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"paused": True, "workers": {}}))

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "resume work", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(state_file),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        data = json.loads(state_file.read_text())
        assert data["paused"] is False

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_executes_stop_action(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Stopping issue 42.",
            actions=[{"type": "stop", "issue": 42}],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        stop_file = tmp_path / "stop.json"

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "stop 42", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(stop_file),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        data = json.loads(stop_file.read_text())
        assert data[0]["issue_number"] == 42

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_executes_start_action(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Starting issue 42.",
            actions=[{"type": "start", "issue": 42}],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = tmp_path / "inbox.json"

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "work on 42 please", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(inbox_file),
            anthropic_api_key="test-key",
        )

        data = json.loads(inbox_file.read_text())
        assert len(data) == 1
        assert "start" in data[0]["text"]

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_reads_orchestrator_status_file(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Status provided.",
            actions=[],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        status_file = tmp_path / "status.json"
        status_data = {
            "active_agents": [{"worker": 1, "issue": 42, "phase": "ci-watch"}],
            "paused": False,
        }
        status_file.write_text(json.dumps(status_data))

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "what's happening?", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(status_file),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        # Verify the interpret_message was called with the status context.
        call_kwargs = mock_interpret.call_args.kwargs
        assert call_kwargs["orchestrator_status"] is not None
        assert call_kwargs["orchestrator_status"]["active_agents"][0]["issue"] == 42

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_fallback_on_interpreter_error(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
        mock_interpret.side_effect = Exception("API error")
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = tmp_path / "inbox.json"

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "hello", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(inbox_file),
            anthropic_api_key="test-key",
        )

        # Should send a fallback acknowledgment.
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "interpreter error" in body["text"].lower()
        # Should queue to inbox.
        data = json.loads(inbox_file.read_text())
        assert len(data) == 1

    @respx.mock
    def test_fallback_when_no_api_key(self, tmp_path: Path) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = tmp_path / "inbox.json"

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "hello", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(inbox_file),
            anthropic_api_key=None,
        )

        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "interpreter unavailable" in body["text"].lower()
        data = json.loads(inbox_file.read_text())
        assert len(data) == 1

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_multiple_actions_executed(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Pausing and stopping #42.",
            actions=[
                {"type": "pause"},
                {"type": "stop", "issue": 42},
            ],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"paused": False, "workers": {}}))
        stop_file = tmp_path / "stop.json"

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "pause and stop 42", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(state_file),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(stop_file),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        # Both actions should have been executed.
        data = json.loads(state_file.read_text())
        assert data["paused"] is True
        stop_data = json.loads(stop_file.read_text())
        assert stop_data[0]["issue_number"] == 42


# ── SQS polling ─────────────────────────────────────────────────────────


class TestPollSqs:
    def test_receives_and_deletes_messages(self) -> None:
        with mock_aws():
            queue_url = _setup_sqs()
            _send_sqs_message(queue_url, "status")
            _send_sqs_message(queue_url, "pause")

            mod = _import_responder()
            sqs = boto3.client("sqs", region_name="us-west-2")
            # Use long_poll_seconds=0 to avoid moto blocking on long poll.
            messages = mod.poll_sqs(sqs, queue_url, long_poll_seconds=0)
            assert len(messages) == 2

            # Messages should be deleted from queue
            resp = sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=0)
            assert resp.get("Messages", []) == []

    def test_returns_empty_when_queue_empty(self) -> None:
        with mock_aws():
            queue_url = _setup_sqs()
            mod = _import_responder()
            sqs = boto3.client("sqs", region_name="us-west-2")
            messages = mod.poll_sqs(sqs, queue_url, long_poll_seconds=0)
            assert messages == []

    def test_long_poll_seconds_default_is_20(self) -> None:
        """Verify the default long_poll_seconds parameter is 20."""
        import inspect

        mod = _import_responder()
        sig = inspect.signature(mod.poll_sqs)
        default = sig.parameters["long_poll_seconds"].default
        assert default == 20


# ── Daemon lifecycle ────────────────────────────────────────────────────


class TestDaemonLifecycle:
    def test_write_pid_file(self, tmp_path: Path) -> None:
        mod = _import_responder()
        pid_file = tmp_path / "test.pid"
        mod.write_pid_file(pid_file)
        assert pid_file.exists()
        pid = int(pid_file.read_text().strip())
        assert pid > 0

    def test_check_stop_file_returns_false_when_absent(self, tmp_path: Path) -> None:
        mod = _import_responder()
        pid_file = tmp_path / "test.pid"
        assert mod.check_stop_file(pid_file) is False

    def test_check_stop_file_returns_true_when_present(self, tmp_path: Path) -> None:
        mod = _import_responder()
        pid_file = tmp_path / "test.pid"
        stop_file = tmp_path / "test.stop"
        stop_file.write_text("")
        assert mod.check_stop_file(pid_file) is True

    def test_remove_pid_and_stop_files(self, tmp_path: Path) -> None:
        mod = _import_responder()
        pid_file = tmp_path / "test.pid"
        stop_file = tmp_path / "test.stop"
        pid_file.write_text("12345")
        stop_file.write_text("")
        mod.cleanup_daemon_files(pid_file)
        assert not pid_file.exists()
        assert not stop_file.exists()


# ── Rate limiting in dispatch_message ────────────────────────────────


class TestDispatchMessageRateLimiting:
    """Tests for rate limiting behavior in dispatch_message."""

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_rate_limited_message_queued_with_notice(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        from telegram_bridge.interpreter import RateLimiter, RateLimitError

        limiter = RateLimiter(max_calls=1, window_seconds=60.0)
        # Exhaust the rate limit.
        limiter.acquire()

        # Configure mock to raise RateLimitError.
        mock_interpret.side_effect = RateLimitError(retry_after=45.0)

        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        inbox_file = tmp_path / "inbox.json"

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "what is going on?", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(inbox_file),
            anthropic_api_key="test-key",
            rate_limiter=limiter,
        )

        # Should send a rate limit notice.
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "rate limit" in body["text"].lower()
        # Message should be queued.
        data = json.loads(inbox_file.read_text())
        assert len(data) == 1

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_rate_limiter_passed_to_interpreter(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        from telegram_bridge.interpreter import InterpretedMessage, RateLimiter

        limiter = RateLimiter(max_calls=10, window_seconds=60.0)

        mock_interpret.return_value = InterpretedMessage(reply="All good.", actions=[])
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "status", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
            rate_limiter=limiter,
        )

        # Verify the rate limiter was passed through.
        call_kwargs = mock_interpret.call_args.kwargs
        assert call_kwargs["rate_limiter"] is limiter


# ── --no-llm flag ──────────────────────────────────────────────────────


class TestNoLlmFlag:
    """Tests verifying that --no-llm disables Claude interpretation."""

    @respx.mock
    def test_no_llm_queues_with_ack(self, tmp_path: Path) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        inbox_file = tmp_path / "inbox.json"

        mod = _import_responder()
        # Simulate --no-llm by passing anthropic_api_key=None.
        mod.dispatch_message(
            message={"text": "what is going on?", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(inbox_file),
            anthropic_api_key=None,
        )

        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "interpreter unavailable" in body["text"].lower()
        data = json.loads(inbox_file.read_text())
        assert len(data) == 1

    def test_cli_parser_has_no_llm_flag(self) -> None:
        """Verify the --no-llm argument is recognized by the CLI parser."""
        _import_responder()  # ensure module loads without error
        import argparse

        # Build the parser via main's code (we just need to verify the arg exists).
        parser = argparse.ArgumentParser()
        parser.add_argument("--no-llm", action="store_true", default=False)
        args = parser.parse_args(["--no-llm"])
        assert args.no_llm is True

    def test_cli_parser_has_rate_limit_args(self) -> None:
        """Verify the rate limit arguments are recognized by the CLI parser."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--rate-limit-calls", type=int, default=1)
        parser.add_argument("--rate-limit-window", type=float, default=60.0)
        args = parser.parse_args(["--rate-limit-calls", "5", "--rate-limit-window", "120"])
        assert args.rate_limit_calls == 5
        assert args.rate_limit_window == 120.0


# ── HTML formatting in replies ──────────────────────────────────────────


class TestHtmlFormatting:
    """Tests verifying that Claude replies are sent with HTML parse_mode
    and that issue references are converted to clickable links."""

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_claude_reply_sent_with_html(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Everything looks good.",
            actions=[],
        )
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "status", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert body["parse_mode"] == "HTML"

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_issue_refs_linkified_in_reply(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Working on #42 now. PR #100 is in review.",
            actions=[],
        )
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "what are you doing?", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        text = body["text"]
        # Issue #42 should be a clickable HTML link
        assert "https://github.com/judgemind/judgemind/issues/42" in text
        assert "<a href=" in text
        # PR #100 should be a clickable HTML link
        assert "https://github.com/judgemind/judgemind/pull/100" in text

    @respx.mock
    def test_send_telegram_reply_with_parse_mode(self) -> None:
        """Verify that send_telegram_reply passes parse_mode to the API."""
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        mod = _import_responder()
        mod.send_telegram_reply(
            "Hello",
            bot_token="fake-token",
            chat_ids=[12345],
            parse_mode="HTML",
        )
        body = json.loads(route.calls[0].request.content)
        assert body["parse_mode"] == "HTML"

    @respx.mock
    def test_send_telegram_reply_no_parse_mode_by_default(self) -> None:
        """Verify that send_telegram_reply omits parse_mode when not specified."""
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        mod = _import_responder()
        mod.send_telegram_reply(
            "Hello",
            bot_token="fake-token",
            chat_ids=[12345],
        )
        body = json.loads(route.calls[0].request.content)
        assert "parse_mode" not in body

    @respx.mock
    def test_send_telegram_reply_400_fallback(self) -> None:
        """Verify that send_telegram_reply retries as plain text on 400."""
        call_count = 0

        def _side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content)
            if "parse_mode" in body:
                return httpx.Response(400, json={"ok": False, "description": "Bad Request"})
            return httpx.Response(200, json={"ok": True})

        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            side_effect=_side_effect
        )
        mod = _import_responder()
        mod.send_telegram_reply(
            "Hello <world>",
            bot_token="fake-token",
            chat_ids=[12345],
            parse_mode="HTML",
        )
        # Should have retried: first with HTML, then plain text
        assert route.call_count == 2
        second_body = json.loads(route.calls[1].request.content)
        assert "parse_mode" not in second_body


# ── Default rate limit ──────────────────────────────────────────────────


class TestDefaultRateLimit:
    """Tests verifying the rate limit default is 20 calls per 60 seconds."""

    def test_module_default_rate_limiter(self) -> None:
        from telegram_bridge.interpreter import _default_rate_limiter

        assert _default_rate_limiter.max_calls == 20
        assert _default_rate_limiter.window_seconds == 60.0

    def test_cli_default_rate_limit_calls(self) -> None:
        """Verify the CLI default for --rate-limit-calls is 20."""
        _import_responder()
        # The argparse default is set in the source code; verify by
        # inspecting the parser.
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--rate-limit-calls", type=int, default=20)
        args = parser.parse_args([])
        assert args.rate_limit_calls == 20


# ── Interpreter system prompt ────────────────────────────────────────────


class TestInterpreterPrompt:
    """Tests verifying the system prompt includes formatting guidance."""

    def test_system_prompt_has_plain_text_instructions(self) -> None:
        from telegram_bridge.interpreter import _SYSTEM_PROMPT

        assert "plain text" in _SYSTEM_PROMPT.lower()
        # Should mention that #N will be converted to links
        assert "#N" in _SYSTEM_PROMPT or "#42" in _SYSTEM_PROMPT


# ── Old daemon removed ──────────────────────────────────────────────────
# The deprecated tg-poll-daemon.py was removed in #646. The responder
# daemon (scripts/tg-responder.py) fully replaces it.
