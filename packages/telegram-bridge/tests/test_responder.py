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
    """Import the responder module from its file path."""
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
        # Should not raise — errors are logged, not propagated.
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


# ── Full dispatch ───────────────────────────────────────────────────────


class TestDispatchCommand:
    @respx.mock
    def test_status_command(self, tmp_path: Path) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"paused": False, "workers": {}}))
        mod = _import_responder()
        mod.dispatch_command(
            message={"text": "status", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(state_file),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
        )
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "no active" in body["text"].lower()

    @respx.mock
    def test_pause_command(self, tmp_path: Path) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"paused": False, "workers": {}}))
        mod = _import_responder()
        mod.dispatch_command(
            message={"text": "pause", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(state_file),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
        )
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "paused" in body["text"].lower()
        # Verify state was updated
        data = json.loads(state_file.read_text())
        assert data["paused"] is True

    @respx.mock
    def test_resume_command(self, tmp_path: Path) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"paused": True, "workers": {}}))
        mod = _import_responder()
        mod.dispatch_command(
            message={"text": "resume", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(state_file),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
        )
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "resumed" in body["text"].lower()
        data = json.loads(state_file.read_text())
        assert data["paused"] is False

    @respx.mock
    def test_stop_command(self, tmp_path: Path) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        stop_file = tmp_path / "stop.json"
        mod = _import_responder()
        mod.dispatch_command(
            message={"text": "stop #42", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(stop_file),
            inbox_file=str(tmp_path / "inbox.json"),
        )
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "#42" in body["text"]
        data = json.loads(stop_file.read_text())
        assert data[0]["issue_number"] == 42

    @respx.mock
    def test_start_command_queued(self, tmp_path: Path) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        inbox_file = tmp_path / "inbox.json"
        mod = _import_responder()
        mod.dispatch_command(
            message={"text": "start #42", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(inbox_file),
        )
        # Should send acknowledgment
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "#42" in body["text"]
        # Should queue to inbox
        data = json.loads(inbox_file.read_text())
        assert len(data) == 1

    @respx.mock
    def test_free_text_queued(self, tmp_path: Path) -> None:
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        inbox_file = tmp_path / "inbox.json"
        mod = _import_responder()
        mod.dispatch_command(
            message={"text": "how are the scrapers?", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(inbox_file),
        )
        assert route.call_count == 1
        data = json.loads(inbox_file.read_text())
        assert len(data) == 1
        assert data[0]["text"] == "how are the scrapers?"


# ── SQS polling ─────────────────────────────────────────────────────────


class TestPollSqs:
    def test_receives_and_deletes_messages(self) -> None:
        with mock_aws():
            queue_url = _setup_sqs()
            _send_sqs_message(queue_url, "status")
            _send_sqs_message(queue_url, "pause")

            mod = _import_responder()
            sqs = boto3.client("sqs", region_name="us-west-2")
            messages = mod.poll_sqs(sqs, queue_url)
            assert len(messages) == 2

            # Messages should be deleted from queue
            resp = sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=0)
            assert resp.get("Messages", []) == []

    def test_returns_empty_when_queue_empty(self) -> None:
        with mock_aws():
            queue_url = _setup_sqs()
            mod = _import_responder()
            sqs = boto3.client("sqs", region_name="us-west-2")
            messages = mod.poll_sqs(sqs, queue_url)
            assert messages == []


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


# ── Deprecation notice on old daemon ────────────────────────────────────


class TestOldDaemonDeprecation:
    def test_old_daemon_has_deprecation_notice(self) -> None:
        old_daemon = REPO_ROOT / "scripts" / "tg-poll-daemon.py"
        content = old_daemon.read_text()
        assert "deprecated" in content.lower() or "DEPRECATED" in content
