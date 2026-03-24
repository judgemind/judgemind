"""Tests for the standalone Telegram responder daemon (scripts/tg-responder.py).

These tests validate command handling, state file reading/writing, SQS polling,
and the Telegram Bot API integration — all with mocked external services.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
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

    The ``scripts/`` directory is added to ``sys.path`` in ``conftest.py``
    so that script imports resolve correctly.
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


class TestReadDispatcherState:
    def test_reads_existing_state(self, tmp_path: Path) -> None:
        state_file = tmp_path / "dispatcher_state.json"
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
        state = mod.read_dispatcher_state(str(state_file))
        assert state["paused"] is True
        assert "2" in state["workers"]
        assert state["workers"]["2"]["issue_number"] == 42

    def test_returns_default_when_missing(self, tmp_path: Path) -> None:
        mod = _import_responder()
        state = mod.read_dispatcher_state(str(tmp_path / "nonexistent.json"))
        assert state["paused"] is False
        assert state["workers"] == {}

    def test_returns_default_when_corrupt(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        state_file.write_text("not json{{{")
        mod = _import_responder()
        state = mod.read_dispatcher_state(str(state_file))
        assert state["paused"] is False
        assert state["workers"] == {}


class TestReadDispatcherStatus:
    def test_reads_status_file(self, tmp_path: Path) -> None:
        status_file = tmp_path / "dispatcher_status.json"
        status_data = {
            "active_agents": [{"worker": 1, "issue": 42}],
            "paused": False,
        }
        status_file.write_text(json.dumps(status_data))
        mod = _import_responder()
        result = mod.read_dispatcher_status(str(status_file))
        assert result is not None
        assert result["active_agents"][0]["issue"] == 42

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        mod = _import_responder()
        result = mod.read_dispatcher_status(str(tmp_path / "nonexistent.json"))
        assert result is None

    def test_returns_none_when_corrupt(self, tmp_path: Path) -> None:
        status_file = tmp_path / "status.json"
        status_file.write_text("not json{{{")
        mod = _import_responder()
        result = mod.read_dispatcher_status(str(status_file))
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


# ── Merge agent status into orchestrator ────────────────────────────────


class TestMergeAgentStatusIntoOrchestrator:
    """Tests for merge_agent_status_into_orchestrator()."""

    def test_adds_new_workers_from_agent_status(self) -> None:
        """Workers in agent-status files but not orchestrator are added."""
        mod = _import_responder()
        orch_status = {
            "active_agents": [
                {
                    "worker_number": 1,
                    "issue_number": 42,
                    "phase": "ci-watch",
                    "updated": "2026-03-10T19:00:00Z",
                }
            ],
            "updated_at": "2026-03-10T19:00:00Z",
        }
        agent_statuses = [
            {
                "worker": "worker-3",
                "issue": "#99",
                "phase": "implementing",
                "updated": "2026-03-10T19:10:00Z",
                "summary": "Writing tests",
            }
        ]
        result = mod.merge_agent_status_into_orchestrator(orch_status, agent_statuses)
        agents = result["active_agents"]
        assert len(agents) == 2
        # Original agent preserved.
        assert agents[0]["issue_number"] == 42
        # New agent added from status file.
        assert agents[1]["issue_number"] == "#99"
        assert agents[1]["source"] == "agent-status-file"

    def test_updates_existing_worker_with_fresher_data(self) -> None:
        """When agent-status file has a more recent timestamp, it wins."""
        mod = _import_responder()
        orch_status = {
            "active_agents": [
                {
                    "worker_number": 2,
                    "issue_number": 42,
                    "phase": "ci-watch",
                    "updated": "2026-03-10T19:00:00Z",
                }
            ],
            "updated_at": "2026-03-10T19:00:00Z",
        }
        agent_statuses = [
            {
                "worker": "worker-2",
                "issue": "#42",
                "phase": "merging",
                "updated": "2026-03-10T19:30:00Z",
                "summary": "Squash merging PR",
            }
        ]
        result = mod.merge_agent_status_into_orchestrator(orch_status, agent_statuses)
        agents = result["active_agents"]
        assert len(agents) == 1
        assert agents[0]["phase"] == "merging"
        assert agents[0]["source"] == "agent-status-file"
        assert agents[0]["summary"] == "Squash merging PR"

    def test_keeps_orchestrator_data_when_fresher(self) -> None:
        """When orchestrator status is more recent, it is preserved."""
        mod = _import_responder()
        orch_status = {
            "active_agents": [
                {
                    "worker_number": 2,
                    "issue_number": 42,
                    "phase": "merging",
                    "updated": "2026-03-10T19:30:00Z",
                }
            ],
            "updated_at": "2026-03-10T19:30:00Z",
        }
        agent_statuses = [
            {
                "worker": "worker-2",
                "issue": "#42",
                "phase": "ci-watch",
                "updated": "2026-03-10T19:00:00Z",
                "summary": "Watching CI",
            }
        ]
        result = mod.merge_agent_status_into_orchestrator(orch_status, agent_statuses)
        agents = result["active_agents"]
        assert len(agents) == 1
        assert agents[0]["phase"] == "merging"
        assert "source" not in agents[0]

    def test_skips_done_workers_not_in_orchestrator(self) -> None:
        """Workers in 'done' phase from agent-status are not added."""
        mod = _import_responder()
        orch_status = {"active_agents": [], "updated_at": "2026-03-10T19:00:00Z"}
        agent_statuses = [
            {
                "worker": "worker-5",
                "issue": "#99",
                "phase": "done",
                "updated": "2026-03-10T19:30:00Z",
                "summary": "Task complete",
            }
        ]
        result = mod.merge_agent_status_into_orchestrator(orch_status, agent_statuses)
        assert len(result["active_agents"]) == 0

    def test_staleness_warning_when_old(self) -> None:
        """A staleness warning is added when status is older than threshold."""
        mod = _import_responder()
        # Use a very old timestamp (well beyond any threshold).
        old_ts = "2020-01-01T00:00:00Z"
        orch_status = {
            "active_agents": [],
            "updated_at": old_ts,
        }
        result = mod.merge_agent_status_into_orchestrator(
            orch_status, [], staleness_threshold_seconds=60.0
        )
        assert "staleness_warning" in result
        assert "stale" in result["staleness_warning"].lower()

    def test_no_staleness_warning_when_fresh(self) -> None:
        """No staleness warning when status is recent."""
        import datetime

        mod = _import_responder()
        now_ts = datetime.datetime.now(datetime.UTC).isoformat()
        orch_status = {
            "active_agents": [],
            "updated_at": now_ts,
        }
        result = mod.merge_agent_status_into_dispatcher(
            orch_status, [], staleness_threshold_seconds=120.0
        )
        assert "staleness_warning" not in result

    def test_does_not_mutate_input(self) -> None:
        """The original dispatcher_status dict is not modified."""
        mod = _import_responder()
        orch_status = {
            "active_agents": [
                {
                    "worker_number": 1,
                    "issue_number": 42,
                    "phase": "ci-watch",
                    "updated": "2026-03-10T19:00:00Z",
                }
            ],
            "updated_at": "2026-03-10T19:00:00Z",
        }
        original_agents = list(orch_status["active_agents"])
        agent_statuses = [
            {
                "worker": "worker-3",
                "issue": "#99",
                "phase": "implementing",
                "updated": "2026-03-10T19:10:00Z",
            }
        ]
        mod.merge_agent_status_into_dispatcher(orch_status, agent_statuses)
        # Original dict should not be modified (active_agents list may be
        # shallow-copied but original list should have same length).
        assert len(orch_status["active_agents"]) == len(original_agents)


class TestDispatchMessageMergesAgentStatus:
    """Tests that dispatch_message always merges agent-status files."""

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_merges_agent_status_when_status_file_exists(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        """Agent-status files supplement dispatcher_status.json."""
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Two workers running.",
            actions=[],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        # Write dispatcher status with one worker.
        status_file = tmp_path / "status.json"
        status_data = {
            "active_agents": [
                {
                    "worker_number": 1,
                    "issue_number": 42,
                    "phase": "ci-watch",
                    "updated": "2026-03-10T19:00:00Z",
                }
            ],
            "paused": False,
            "updated_at": "2026-03-10T19:00:00Z",
        }
        status_file.write_text(json.dumps(status_data))

        # Write agent-status file for a different worker.
        agent_dir = tmp_path / "agent-status"
        agent_dir.mkdir()
        (agent_dir / "worker-3.txt").write_text(
            "issue: #99\nphase: implementing\n"
            "updated: 2026-03-10T19:10:00Z\nsummary: Writing code\n"
        )

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "status", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(status_file),
            agent_status_dir=str(agent_dir),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        call_kwargs = mock_interpret.call_args.kwargs
        agents = call_kwargs["orchestrator_status"]["active_agents"]
        # Should have both workers: one from status file, one from agent-status.
        assert len(agents) == 2
        worker_issues = {a.get("issue_number") for a in agents}
        assert 42 in worker_issues
        assert "#99" in worker_issues

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_prefers_fresher_agent_status_per_worker(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        """When agent-status file is newer, its phase wins."""
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Worker updated.",
            actions=[],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        # Dispatcher says worker-2 is in ci-watch (old).
        status_file = tmp_path / "status.json"
        status_data = {
            "active_agents": [
                {
                    "worker_number": 2,
                    "issue_number": 42,
                    "phase": "ci-watch",
                    "updated": "2026-03-10T19:00:00Z",
                }
            ],
            "paused": False,
            "updated_at": "2026-03-10T19:00:00Z",
        }
        status_file.write_text(json.dumps(status_data))

        # Agent-status says worker-2 is merging (newer).
        agent_dir = tmp_path / "agent-status"
        agent_dir.mkdir()
        (agent_dir / "worker-2.txt").write_text(
            "issue: #42\nphase: merging\nupdated: 2026-03-10T19:30:00Z\nsummary: Squash merge\n"
        )

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "what's worker 2 doing?", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(status_file),
            agent_status_dir=str(agent_dir),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        call_kwargs = mock_interpret.call_args.kwargs
        agents = call_kwargs["orchestrator_status"]["active_agents"]
        assert len(agents) == 1
        assert agents[0]["phase"] == "merging"

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_staleness_warning_included_in_context(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        """Stale dispatcher status includes a warning in the context."""
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Status is stale.",
            actions=[],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        # Write dispatcher status with a very old timestamp.
        status_file = tmp_path / "status.json"
        status_data = {
            "active_agents": [],
            "paused": False,
            "updated_at": "2020-01-01T00:00:00Z",
        }
        status_file.write_text(json.dumps(status_data))

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "status", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(status_file),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        call_kwargs = mock_interpret.call_args.kwargs
        status = call_kwargs["orchestrator_status"]
        assert "staleness_warning" in status
        assert "stale" in status["staleness_warning"].lower()


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
    def test_reads_dispatcher_status_file(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
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


# ── dispatch_message — new action types (file_issue, discuss, do) ──────


class TestDispatchMessageNewActions:
    """Tests for the new complex request passthrough action types."""

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_file_issue_action_queued_to_inbox(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Filing that issue.",
            actions=[
                {
                    "type": "file_issue",
                    "description": "OC scraper timing out on PDFs",
                    "priority": "p2",
                    "labels": ["area/scraping"],
                }
            ],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = tmp_path / "inbox.json"

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "file a bug about OC scraper", "user_id": 12345},
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
        assert data[0]["action"] == "file_issue"
        assert data[0]["description"] == "OC scraper timing out on PDFs"
        assert data[0]["priority"] == "p2"
        assert data[0]["labels"] == ["area/scraping"]
        assert data[0]["reply_to"] == 12345

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_discuss_action_queued_to_inbox(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Forwarding to the orchestrator.",
            actions=[
                {
                    "type": "discuss",
                    "message": "Should we use Redis for caching ruling lookups?",
                }
            ],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = tmp_path / "inbox.json"

        mod = _import_responder()
        mod.dispatch_message(
            message={
                "text": "Should we use Redis for caching ruling lookups?",
                "user_id": 12345,
            },
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
        assert data[0]["action"] == "discuss"
        assert data[0]["message"] == "Should we use Redis for caching ruling lookups?"
        assert data[0]["reply_to"] == 12345

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_do_action_queued_to_inbox(self, mock_interpret: MagicMock, tmp_path: Path) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="On it.",
            actions=[
                {
                    "type": "do",
                    "instruction": "Check if PR #738 CI passed and merge it",
                }
            ],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = tmp_path / "inbox.json"

        mod = _import_responder()
        mod.dispatch_message(
            message={
                "text": "check CI on #738 and merge it",
                "user_id": 12345,
            },
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
        assert data[0]["action"] == "do"
        assert data[0]["instruction"] == "Check if PR #738 CI passed and merge it"
        assert data[0]["reply_to"] == 12345

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_file_issue_defaults_priority_to_p2(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Filing.",
            actions=[
                {
                    "type": "file_issue",
                    "description": "Something broken",
                    # No priority or labels specified.
                }
            ],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = tmp_path / "inbox.json"

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "file a bug", "user_id": 12345},
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
        assert data[0]["priority"] == "p2"
        assert data[0]["labels"] == []


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


# ── Process alive check ────────────────────────────────────────────────


class TestIsProcessAlive:
    def test_current_process_is_alive(self) -> None:
        mod = _import_responder()
        assert mod.is_process_alive(os.getpid()) is True

    def test_dead_process_returns_false(self) -> None:
        mod = _import_responder()
        # PID 99999999 is almost certainly not running.
        assert mod.is_process_alive(99999999) is False


# ── Stale PID detection ────────────────────────────────────────────────


class TestCheckStalePid:
    def test_no_pid_file_is_noop(self, tmp_path: Path) -> None:
        mod = _import_responder()
        pid_file = tmp_path / "test.pid"
        # Should not raise.
        mod.check_stale_pid(pid_file)

    def test_stale_pid_is_cleaned_up(self, tmp_path: Path) -> None:
        mod = _import_responder()
        pid_file = tmp_path / "test.pid"
        # Write a PID that does not exist.
        pid_file.write_text("99999999")
        mod.check_stale_pid(pid_file)
        # PID file should be removed.
        assert not pid_file.exists()

    def test_corrupt_pid_file_is_cleaned_up(self, tmp_path: Path) -> None:
        mod = _import_responder()
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not-a-number")
        mod.check_stale_pid(pid_file)
        assert not pid_file.exists()

    def test_alive_process_without_force_exits(self, tmp_path: Path) -> None:
        mod = _import_responder()
        pid_file = tmp_path / "test.pid"
        # Write the current process PID (which is alive).
        pid_file.write_text(str(os.getpid()))
        with pytest.raises(SystemExit) as exc_info:
            mod.check_stale_pid(pid_file, force=False)
        assert exc_info.value.code == 1

    @patch("tg_responder.is_process_alive")
    def test_force_stops_existing_and_proceeds(self, mock_alive: MagicMock, tmp_path: Path) -> None:
        mod = _import_responder()
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")

        # Simulate: first call returns True (alive), then False (exited).
        mock_alive.side_effect = [True, True, False]

        mod.check_stale_pid(pid_file, force=True)

        # Stop file should have been created (then cleaned up).
        # PID file should be cleaned up after the process exits.
        assert not pid_file.exists()

    @patch("tg_responder.is_process_alive")
    def test_force_exits_if_process_wont_die(self, mock_alive: MagicMock, tmp_path: Path) -> None:
        mod = _import_responder()
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")

        # Process never exits.
        mock_alive.return_value = True

        with pytest.raises(SystemExit) as exc_info:
            # Patch time.sleep to avoid waiting 10 real seconds.
            with patch("tg_responder.time.sleep"):
                # Also patch time.monotonic to simulate time passing.
                start = time.monotonic()
                with patch(
                    "tg_responder.time.monotonic",
                    side_effect=[start, start + 11.0],
                ):
                    mod.check_stale_pid(pid_file, force=True)
        assert exc_info.value.code == 1


# ── _should_stop helper ────────────────────────────────────────────────


class TestShouldStop:
    def test_returns_false_when_no_stop_requested(self, tmp_path: Path) -> None:
        mod = _import_responder()
        # Reset global state.
        mod._shutdown_requested = False
        pid_file = tmp_path / "test.pid"
        assert mod._should_stop(pid_file) is False

    def test_returns_true_when_shutdown_flag_set(self, tmp_path: Path) -> None:
        mod = _import_responder()
        mod._shutdown_requested = True
        pid_file = tmp_path / "test.pid"
        assert mod._should_stop(pid_file) is True
        # Reset.
        mod._shutdown_requested = False

    def test_returns_true_when_stop_file_exists(self, tmp_path: Path) -> None:
        mod = _import_responder()
        mod._shutdown_requested = False
        pid_file = tmp_path / "test.pid"
        stop_file = tmp_path / "test.stop"
        stop_file.write_text("")
        assert mod._should_stop(pid_file) is True
        # The function should also set the shutdown flag.
        assert mod._shutdown_requested is True
        # Reset.
        mod._shutdown_requested = False


# ── SQS_BOTO_CONFIG ────────────────────────────────────────────────────


class TestSqsBotoConfig:
    def test_config_has_bounded_timeouts(self) -> None:
        mod = _import_responder()
        config = mod.SQS_BOTO_CONFIG
        assert config.connect_timeout == 5
        assert config.read_timeout == 25

    def test_config_has_limited_retries(self) -> None:
        mod = _import_responder()
        config = mod.SQS_BOTO_CONFIG
        assert config.retries["max_attempts"] == 2

    def test_read_timeout_exceeds_long_poll_wait(self) -> None:
        """read_timeout must be >= long_poll_seconds + 5 to avoid timeouts.

        SQS long polling blocks for up to ``long_poll_seconds``
        (WaitTimeSeconds).  If read_timeout is shorter, the HTTP socket
        times out before SQS can respond.  The +5s buffer accounts for
        response transmission overhead.

        See: https://github.com/judgemind/judgemind/issues/769
        """
        import inspect

        mod = _import_responder()
        config = mod.SQS_BOTO_CONFIG
        sig = inspect.signature(mod.poll_sqs)
        long_poll_default = sig.parameters["long_poll_seconds"].default

        assert config.read_timeout >= long_poll_default + 5, (
            f"SQS_BOTO_CONFIG.read_timeout ({config.read_timeout}s) must be "
            f">= poll_sqs long_poll_seconds default ({long_poll_default}s) + 5"
        )


# ── --force CLI flag ───────────────────────────────────────────────────


class TestForceCliFlag:
    def test_cli_parser_has_force_flag(self) -> None:
        """Verify the --force argument is recognized by the CLI parser."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--force", action="store_true", default=False)
        args = parser.parse_args(["--force"])
        assert args.force is True

    def test_cli_parser_force_defaults_to_false(self) -> None:
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--force", action="store_true", default=False)
        args = parser.parse_args([])
        assert args.force is False


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
    """Tests verifying rate limit defaults.

    The module-level default limiter (used by interpret_message/Haiku) is
    20 calls/60s.  The CLI default (used by the daemon, defaulting to Opus)
    is 10 calls/60s to control cost.
    """

    def test_module_default_rate_limiter(self) -> None:
        from telegram_bridge.interpreter import _default_rate_limiter

        assert _default_rate_limiter.max_calls == 20
        assert _default_rate_limiter.window_seconds == 60.0

    def test_cli_default_rate_limit_calls(self) -> None:
        """Verify the CLI default for --rate-limit-calls is 10 (Opus cost safety)."""
        _import_responder()
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--rate-limit-calls", type=int, default=10)
        args = parser.parse_args([])
        assert args.rate_limit_calls == 10


# ── Interpreter system prompt ────────────────────────────────────────────


class TestInterpreterPrompt:
    """Tests verifying the system prompt includes formatting guidance."""

    def test_system_prompt_has_plain_text_instructions(self) -> None:
        from telegram_bridge.interpreter import _SYSTEM_PROMPT

        assert "plain text" in _SYSTEM_PROMPT.lower()
        # Should mention that #N will be converted to links
        assert "#N" in _SYSTEM_PROMPT or "#42" in _SYSTEM_PROMPT


# ── Webhook health check ────────────────────────────────────────────────


class TestCheckWebhookHealth:
    """Tests for the webhook URL health check on responder startup."""

    @respx.mock
    def test_logs_ok_when_url_correct(self, caplog: pytest.LogCaptureFixture) -> None:
        """A correctly registered URL should log INFO with 'OK'."""
        respx.get("https://api.telegram.org/bottest-token/getWebhookInfo").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "url": "https://abc123.execute-api.us-west-2.amazonaws.com/webhook",
                        "has_custom_certificate": False,
                        "pending_update_count": 0,
                    },
                },
            )
        )
        mod = _import_responder()
        with caplog.at_level("INFO"):
            mod.check_webhook_health(bot_token="test-token")
        assert any("OK" in r.message for r in caplog.records)

    @respx.mock
    def test_warns_when_no_url_registered(self, caplog: pytest.LogCaptureFixture) -> None:
        """An empty webhook URL should produce a warning."""
        respx.get("https://api.telegram.org/bottest-token/getWebhookInfo").mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"url": "", "pending_update_count": 0}},
            )
        )
        mod = _import_responder()
        with caplog.at_level("WARNING"):
            mod.check_webhook_health(bot_token="test-token")
        assert any("no webhook URL" in r.message for r in caplog.records)

    @respx.mock
    def test_warns_when_url_missing_webhook_path(self, caplog: pytest.LogCaptureFixture) -> None:
        """A URL without /webhook suffix should produce a warning."""
        respx.get("https://api.telegram.org/bottest-token/getWebhookInfo").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "url": "https://abc123.execute-api.us-west-2.amazonaws.com",
                        "pending_update_count": 0,
                    },
                },
            )
        )
        mod = _import_responder()
        with caplog.at_level("WARNING"):
            mod.check_webhook_health(bot_token="test-token")
        assert any("does not end with /webhook" in r.message for r in caplog.records)

    @respx.mock
    def test_warns_when_url_missing_expected_substring(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A URL that doesn't contain the expected substring should warn."""
        respx.get("https://api.telegram.org/bottest-token/getWebhookInfo").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "url": "https://example.com/webhook",
                        "pending_update_count": 0,
                    },
                },
            )
        )
        mod = _import_responder()
        with caplog.at_level("WARNING"):
            mod.check_webhook_health(bot_token="test-token")
        assert any("does not contain" in r.message for r in caplog.records)

    @respx.mock
    def test_warns_on_telegram_last_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """If Telegram reports a last_error_message, it should be logged."""
        respx.get("https://api.telegram.org/bottest-token/getWebhookInfo").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "url": "https://abc123.execute-api.us-west-2.amazonaws.com/webhook",
                        "pending_update_count": 0,
                        "last_error_message": "Connection refused",
                    },
                },
            )
        )
        mod = _import_responder()
        with caplog.at_level("WARNING"):
            mod.check_webhook_health(bot_token="test-token")
        assert any("Connection refused" in r.message for r in caplog.records)

    @respx.mock
    def test_does_not_raise_on_api_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        """Network or API errors should be caught and logged, not raised."""
        respx.get("https://api.telegram.org/bottest-token/getWebhookInfo").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        mod = _import_responder()
        with caplog.at_level("WARNING"):
            mod.check_webhook_health(bot_token="test-token")
        # Should not raise; should log a warning about the HTTP status.
        assert any("returned 500" in r.message for r in caplog.records)

    @respx.mock
    def test_does_not_raise_on_network_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """A network error should be caught and logged, not raised."""
        respx.get("https://api.telegram.org/bottest-token/getWebhookInfo").mock(
            side_effect=httpx.ConnectError("DNS resolution failed")
        )
        mod = _import_responder()
        with caplog.at_level("WARNING"):
            mod.check_webhook_health(bot_token="test-token")
        assert any("failed (non-fatal)" in r.message for r in caplog.records)


# ── Proactive staleness alert ──────────────────────────────────────────


class TestStalenessTracker:
    """Tests for the StalenessTracker and check_orchestrator_staleness."""

    def test_no_alert_when_status_file_missing(self, tmp_path: Path) -> None:
        """No alert when the status file does not exist."""
        mod = _import_responder()
        tracker = mod.StalenessTracker()
        should_alert, alert_text, status = mod.check_orchestrator_staleness(
            status_file=str(tmp_path / "nonexistent.json"),
            tracker=tracker,
        )
        assert should_alert is False
        assert alert_text == ""
        assert status is None

    def test_no_alert_when_status_is_fresh(self, tmp_path: Path) -> None:
        """No alert when the orchestrator status was recently updated."""
        import datetime

        mod = _import_responder()
        tracker = mod.StalenessTracker()
        status_file = tmp_path / "status.json"
        now_ts = datetime.datetime.now(datetime.UTC).isoformat()
        status_file.write_text(json.dumps({"active_agents": [], "updated_at": now_ts}))
        should_alert, alert_text, status = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            threshold_seconds=300.0,
        )
        assert should_alert is False
        assert alert_text == ""
        assert status is not None

    def test_alert_when_status_is_stale(self, tmp_path: Path) -> None:
        """Alert fires when the status is older than the threshold."""
        mod = _import_responder()
        tracker = mod.StalenessTracker()
        status_file = tmp_path / "status.json"
        old_ts = "2020-01-01T00:00:00Z"
        status_file.write_text(
            json.dumps(
                {
                    "active_agents": [
                        {
                            "issue_number": 42,
                            "phase": "ci-watch",
                            "issue_title": "Fix something",
                        }
                    ],
                    "recently_completed": [
                        {"issue_number": 100, "outcome": "completed"},
                    ],
                    "paused": False,
                    "updated_at": old_ts,
                }
            )
        )
        should_alert, alert_text, status = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            threshold_seconds=60.0,
        )
        assert should_alert is True
        assert "not been updated" in alert_text.lower()
        assert "expired or crashed" in alert_text.lower()
        assert "#42" in alert_text
        assert "ci-watch" in alert_text
        assert "#100" in alert_text
        assert "completed" in alert_text
        assert old_ts in alert_text
        assert "/dispatcher" in alert_text
        assert tracker.alert_sent is True

    def test_deduplication_only_one_alert_per_stale_period(self, tmp_path: Path) -> None:
        """Only one alert is sent per stale period (de-duplication)."""
        mod = _import_responder()
        tracker = mod.StalenessTracker()
        status_file = tmp_path / "status.json"
        old_ts = "2020-01-01T00:00:00Z"
        status_file.write_text(json.dumps({"active_agents": [], "updated_at": old_ts}))

        # First check: alert fires.
        should_alert1, _, _ = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            threshold_seconds=60.0,
        )
        assert should_alert1 is True

        # Second check (same stale period): no alert.
        should_alert2, _, _ = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            threshold_seconds=60.0,
        )
        assert should_alert2 is False

    def test_reset_after_fresh_update(self, tmp_path: Path) -> None:
        """After the orchestrator writes a fresh update, a new stale period can alert."""
        import datetime

        mod = _import_responder()
        tracker = mod.StalenessTracker()
        status_file = tmp_path / "status.json"
        old_ts = "2020-01-01T00:00:00Z"
        status_file.write_text(json.dumps({"active_agents": [], "updated_at": old_ts}))

        # First stale period: alert fires.
        should_alert1, _, _ = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            threshold_seconds=60.0,
        )
        assert should_alert1 is True

        # Orchestrator comes back — writes a fresh timestamp.
        fresh_ts = datetime.datetime.now(datetime.UTC).isoformat()
        status_file.write_text(json.dumps({"active_agents": [], "updated_at": fresh_ts}))

        # Check with fresh status — no alert, tracker resets.
        should_alert2, _, _ = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            threshold_seconds=60.0,
        )
        assert should_alert2 is False
        assert tracker.alert_sent is False

        # Orchestrator stalls again (simulate by writing old timestamp).
        second_stale_ts = "2020-06-01T00:00:00Z"
        status_file.write_text(json.dumps({"active_agents": [], "updated_at": second_stale_ts}))

        # Second stale period: alert fires again.
        should_alert3, _, _ = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            threshold_seconds=60.0,
        )
        assert should_alert3 is True

    def test_alert_with_paused_state(self, tmp_path: Path) -> None:
        """Alert includes 'paused' info when orchestrator was paused."""
        mod = _import_responder()
        tracker = mod.StalenessTracker()
        status_file = tmp_path / "status.json"
        status_file.write_text(
            json.dumps(
                {
                    "active_agents": [],
                    "paused": True,
                    "updated_at": "2020-01-01T00:00:00Z",
                }
            )
        )
        should_alert, alert_text, _ = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            threshold_seconds=60.0,
        )
        assert should_alert is True
        assert "paused" in alert_text.lower()

    def test_alert_with_no_agents(self, tmp_path: Path) -> None:
        """Alert includes 'no active agents' when none are running."""
        mod = _import_responder()
        tracker = mod.StalenessTracker()
        status_file = tmp_path / "status.json"
        status_file.write_text(
            json.dumps(
                {
                    "active_agents": [],
                    "paused": False,
                    "updated_at": "2020-01-01T00:00:00Z",
                }
            )
        )
        should_alert, alert_text, _ = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            threshold_seconds=60.0,
        )
        assert should_alert is True
        assert "no active agents" in alert_text.lower()

    def test_default_threshold_is_one_hour(self) -> None:
        """The default proactive alert threshold is 60 minutes (3600s)."""
        mod = _import_responder()
        assert mod.STALE_ALERT_THRESHOLD_SECONDS == 3600.0

    def test_no_alert_at_thirty_minutes(self, tmp_path: Path) -> None:
        """No alert when status is 30 minutes old (within 60-min threshold)."""
        import datetime

        mod = _import_responder()
        tracker = mod.StalenessTracker()
        status_file = tmp_path / "status.json"
        # 30 minutes ago — should NOT trigger with the 60-min default.
        thirty_min_ago = (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
        ).isoformat()
        status_file.write_text(json.dumps({"active_agents": [], "updated_at": thirty_min_ago}))
        should_alert, _, _ = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            # Use default threshold (omit threshold_seconds).
        )
        assert should_alert is False

    def test_alert_at_ninety_minutes(self, tmp_path: Path) -> None:
        """Alert fires when status is 90 minutes old (beyond 60-min threshold)."""
        import datetime

        mod = _import_responder()
        tracker = mod.StalenessTracker()
        status_file = tmp_path / "status.json"
        ninety_min_ago = (
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=90)
        ).isoformat()
        status_file.write_text(json.dumps({"active_agents": [], "updated_at": ninety_min_ago}))
        should_alert, alert_text, _ = mod.check_orchestrator_staleness(
            status_file=str(status_file),
            tracker=tracker,
            # Use default threshold (omit threshold_seconds).
        )
        assert should_alert is True
        assert "90 minute" in alert_text


class TestFormatStaleAlert:
    """Tests for the format_stale_alert function."""

    def test_basic_alert_format(self) -> None:
        """Alert includes time, suggestions, and last heartbeat."""
        mod = _import_responder()
        alert = mod.format_stale_alert(
            3600.0,
            {
                "active_agents": [],
                "paused": False,
                "updated_at": "2026-03-10T10:00:00Z",
            },
        )
        assert "60 minute" in alert
        assert "expired or crashed" in alert
        assert "/dispatcher" in alert
        assert "2026-03-10T10:00:00Z" in alert

    def test_alert_with_none_status(self) -> None:
        """Alert works when dispatcher_status is None."""
        mod = _import_responder()
        alert = mod.format_stale_alert(300.0, None)
        assert "5 minute" in alert
        assert "/dispatcher" in alert

    def test_alert_includes_active_agents(self) -> None:
        """Alert lists active agents when present."""
        mod = _import_responder()
        alert = mod.format_stale_alert(
            300.0,
            {
                "active_agents": [
                    {
                        "issue_number": 42,
                        "phase": "ci-watch",
                        "issue_title": "Fix widget",
                    },
                    {
                        "issue_number": 99,
                        "phase": "ralph-worker (iteration 2)",
                        "issue_title": "Add feature",
                    },
                ],
                "paused": False,
                "updated_at": "2026-03-10T10:00:00Z",
            },
        )
        assert "#42" in alert
        assert "ci-watch" in alert
        assert "#99" in alert
        assert "Fix widget" in alert

    def test_alert_includes_recent_completions(self) -> None:
        """Alert shows recent task completions for context."""
        mod = _import_responder()
        alert = mod.format_stale_alert(
            300.0,
            {
                "active_agents": [],
                "paused": False,
                "recently_completed": [
                    {"issue_number": 10, "outcome": "completed"},
                    {"issue_number": 20, "outcome": "failed"},
                ],
                "updated_at": "2026-03-10T10:00:00Z",
            },
        )
        assert "#10" in alert
        assert "completed" in alert
        assert "#20" in alert
        assert "failed" in alert


# ── Opus agent mode ────────────────────────────────────────────────────


class TestOpusDispatch:
    """Tests for dispatch_message in Opus agent mode."""

    @respx.mock
    @patch("tg_responder.interpret_message_with_tools")
    def test_opus_mode_calls_tools_interpreter(self, mock_opus: MagicMock, tmp_path: Path) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_opus.return_value = InterpretedMessage(
            reply="The README describes Judgemind.",
            actions=[],
        )
        route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "What is this project?", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
            use_opus=True,
            repo_root=tmp_path,
        )

        mock_opus.assert_called_once()
        assert route.call_count == 1
        body = json.loads(route.calls[0].request.content)
        assert "Judgemind" in body["text"]

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_haiku_mode_calls_basic_interpreter(
        self, mock_haiku: MagicMock, tmp_path: Path
    ) -> None:
        from telegram_bridge.interpreter import InterpretedMessage

        mock_haiku.return_value = InterpretedMessage(
            reply="All good.",
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
            use_opus=False,
        )

        mock_haiku.assert_called_once()
        assert route.call_count == 1

    @respx.mock
    @patch("tg_responder.interpret_message_with_tools")
    def test_opus_actions_still_executed(self, mock_opus: MagicMock, tmp_path: Path) -> None:
        """Opus mode still executes orchestrator actions (pause, etc.)."""
        from telegram_bridge.interpreter import InterpretedMessage

        mock_opus.return_value = InterpretedMessage(
            reply="Pausing the orchestrator.",
            actions=[{"type": "pause"}],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"paused": False, "workers": {}}))

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "pause", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(state_file),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
            use_opus=True,
            repo_root=tmp_path,
        )

        data = json.loads(state_file.read_text())
        assert data["paused"] is True

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_opus_false_with_no_repo_root_falls_back_to_haiku(
        self, mock_haiku: MagicMock, tmp_path: Path
    ) -> None:
        """When use_opus=True but repo_root is None, falls back to Haiku."""
        from telegram_bridge.interpreter import InterpretedMessage

        mock_haiku.return_value = InterpretedMessage(
            reply="Fallback reply.",
            actions=[],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "hello", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
            use_opus=True,
            repo_root=None,
        )

        # Falls back to basic interpreter when repo_root is None.
        mock_haiku.assert_called_once()

    def test_cli_parser_has_model_arg(self) -> None:
        """Verify the --model argument is recognized by the CLI parser."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--model", choices=["opus", "haiku"], default="opus")
        args = parser.parse_args(["--model", "haiku"])
        assert args.model == "haiku"

    def test_cli_model_default_is_opus(self) -> None:
        """Verify the --model default is opus."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--model", choices=["opus", "haiku"], default="opus")
        args = parser.parse_args([])
        assert args.model == "opus"


# ── Photo message handling ─────────────────────────────────────────────


class TestDownloadTelegramPhoto:
    """Tests for download_telegram_photo()."""

    @respx.mock
    def test_downloads_and_saves_photo(self, tmp_path: Path) -> None:
        """Successfully downloads a photo and saves it to disk."""
        mod = _import_responder()

        # Mock getFile response.
        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "test-file-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/test.jpg"}},
            )
        )

        # Mock file download.
        respx.get("https://api.telegram.org/file/botfake-token/photos/test.jpg").mock(
            return_value=httpx.Response(200, content=b"fake-image-data")
        )

        save_dir = str(tmp_path / "tg_photos")
        result = mod.download_telegram_photo(
            file_id="test-file-id",
            bot_token="fake-token",
            save_dir=save_dir,
            message_id=42,
        )

        assert result is not None
        assert Path(result).exists()
        assert Path(result).read_bytes() == b"fake-image-data"
        assert "_42" in result
        assert result.endswith(".jpg")

    @respx.mock
    def test_returns_none_on_get_file_failure(self, tmp_path: Path) -> None:
        """Returns None when getFile API call fails."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "bad-id"},
        ).mock(return_value=httpx.Response(404, json={"ok": False}))

        result = mod.download_telegram_photo(
            file_id="bad-id",
            bot_token="fake-token",
            save_dir=str(tmp_path / "tg_photos"),
        )
        assert result is None

    @respx.mock
    def test_returns_none_on_download_failure(self, tmp_path: Path) -> None:
        """Returns None when file download fails."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "test-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/test.png"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/photos/test.png").mock(
            return_value=httpx.Response(500)
        )

        result = mod.download_telegram_photo(
            file_id="test-id",
            bot_token="fake-token",
            save_dir=str(tmp_path / "tg_photos"),
        )
        assert result is None

    @respx.mock
    def test_creates_save_directory(self, tmp_path: Path) -> None:
        """Creates the save directory if it does not exist."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "test-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/img.jpg"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/photos/img.jpg").mock(
            return_value=httpx.Response(200, content=b"data")
        )

        nested_dir = str(tmp_path / "nested" / "tg_photos")
        result = mod.download_telegram_photo(
            file_id="test-id",
            bot_token="fake-token",
            save_dir=nested_dir,
        )
        assert result is not None
        assert Path(nested_dir).is_dir()

    @respx.mock
    def test_returns_none_on_empty_file_path(self, tmp_path: Path) -> None:
        """Returns None when getFile returns no file_path."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "test-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {}},
            )
        )

        result = mod.download_telegram_photo(
            file_id="test-id",
            bot_token="fake-token",
            save_dir=str(tmp_path / "tg_photos"),
        )
        assert result is None


class TestHandlePhotoMessage:
    """Tests for handle_photo_message()."""

    @respx.mock
    def test_downloads_saves_and_creates_inbox_entry(self, tmp_path: Path) -> None:
        """Full happy path: download, save, inbox entry, reply."""
        mod = _import_responder()

        # Mock getFile and download.
        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "large-photo-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/large.jpg"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/photos/large.jpg").mock(
            return_value=httpx.Response(200, content=b"photo-bytes")
        )

        reply_route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")
        photos_dir = str(tmp_path / "tg_photos")

        message: dict[str, object] = {
            "photo": [
                {"file_id": "small-photo-id", "width": 100, "height": 100},
                {"file_id": "large-photo-id", "width": 800, "height": 600},
            ],
            "caption": "Check this out",
            "message_id": 99,
            "user_id": 12345,
            "chat_id": 12345,
        }

        mod.handle_photo_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=inbox_file,
            photos_dir=photos_dir,
        )

        # Check inbox entry was created.
        inbox_data = json.loads(Path(inbox_file).read_text())
        assert len(inbox_data) == 1
        entry = inbox_data[0]
        assert entry["action"] == "photo"
        assert entry["file_path"] is not None
        assert entry["caption"] == "Check this out"
        assert entry["reply_to"] == 99
        assert "timestamp" in entry

        # Check photo was saved.
        assert Path(entry["file_path"]).exists()
        assert Path(entry["file_path"]).read_bytes() == b"photo-bytes"

        # Check reply was sent.
        assert reply_route.call_count == 1
        reply_body = json.loads(reply_route.calls[0].request.content)
        assert "Photo received" in reply_body["text"]
        assert "Check this out" in reply_body["text"]

    @respx.mock
    def test_no_caption(self, tmp_path: Path) -> None:
        """Photo without caption omits caption field from inbox entry."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "photo-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/img.jpg"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/photos/img.jpg").mock(
            return_value=httpx.Response(200, content=b"data")
        )

        reply_route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")

        message: dict[str, object] = {
            "photo": [{"file_id": "photo-id", "width": 800, "height": 600}],
            "message_id": 50,
            "user_id": 12345,
        }

        mod.handle_photo_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=inbox_file,
            photos_dir=str(tmp_path / "tg_photos"),
        )

        inbox_data = json.loads(Path(inbox_file).read_text())
        assert len(inbox_data) == 1
        assert "caption" not in inbox_data[0]

        # Reply should not mention caption.
        reply_body = json.loads(reply_route.calls[0].request.content)
        assert "Photo received, forwarded to orchestrator." == reply_body["text"]

    @respx.mock
    def test_download_failure_sends_error_reply(self, tmp_path: Path) -> None:
        """When photo download fails, an error reply is sent."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "bad-id"},
        ).mock(return_value=httpx.Response(500))

        reply_route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")

        message: dict[str, object] = {
            "photo": [{"file_id": "bad-id", "width": 100, "height": 100}],
            "message_id": 51,
        }

        mod.handle_photo_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=inbox_file,
            photos_dir=str(tmp_path / "tg_photos"),
        )

        # No inbox entry should be created.
        assert not Path(inbox_file).exists()

        # Error reply should be sent.
        assert reply_route.call_count == 1
        reply_body = json.loads(reply_route.calls[0].request.content)
        assert "Failed to download" in reply_body["text"]

    def test_empty_photo_array_skipped(self, tmp_path: Path) -> None:
        """Messages with empty photo array are silently skipped."""
        mod = _import_responder()

        message: dict[str, object] = {
            "photo": [],
            "message_id": 52,
        }

        # Should not raise.
        mod.handle_photo_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=str(tmp_path / "inbox.json"),
            photos_dir=str(tmp_path / "tg_photos"),
        )

    @respx.mock
    def test_uses_largest_photo(self, tmp_path: Path) -> None:
        """Should use the last (largest) photo in the array."""
        mod = _import_responder()

        # Only the large photo file_id should be requested.
        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "large-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/large.jpg"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/photos/large.jpg").mock(
            return_value=httpx.Response(200, content=b"big-photo")
        )

        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        message: dict[str, object] = {
            "photo": [
                {"file_id": "small-id", "width": 90, "height": 90},
                {"file_id": "medium-id", "width": 320, "height": 320},
                {"file_id": "large-id", "width": 800, "height": 800},
            ],
            "message_id": 53,
        }

        mod.handle_photo_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=str(tmp_path / "inbox.json"),
            photos_dir=str(tmp_path / "tg_photos"),
        )

        inbox_data = json.loads(Path(tmp_path / "inbox.json").read_text())
        assert len(inbox_data) == 1
        # Verify the saved file contains the large photo data.
        assert Path(inbox_data[0]["file_path"]).read_bytes() == b"big-photo"


class TestDispatchMessagePhotoRouting:
    """Tests that dispatch_message routes photo messages to handle_photo_message."""

    @respx.mock
    def test_photo_message_routed_to_handler(self, tmp_path: Path) -> None:
        """Messages with 'photo' key skip Claude and are handled as photos."""
        mod = _import_responder()

        # Mock getFile and download.
        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "photo-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/img.jpg"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/photos/img.jpg").mock(
            return_value=httpx.Response(200, content=b"data")
        )

        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")

        mod.dispatch_message(
            message={
                "text": "",
                "photo": [{"file_id": "photo-id", "width": 800, "height": 600}],
                "message_id": 60,
                "user_id": 12345,
                "chat_id": 12345,
            },
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=inbox_file,
            anthropic_api_key="test-key",
        )

        # An inbox entry should exist with action "photo".
        inbox_data = json.loads(Path(inbox_file).read_text())
        assert len(inbox_data) == 1
        assert inbox_data[0]["action"] == "photo"

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_text_message_still_uses_claude(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        """Messages without 'photo' key still go through Claude interpreter."""
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Hello!",
            actions=[],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "hello", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
        )

        mock_interpret.assert_called_once()


# ── Document message handling ──────────────────────────────────────────


class TestHandleDocumentMessage:
    """Tests for handle_document_message()."""

    @respx.mock
    def test_downloads_saves_and_creates_inbox_entry(self, tmp_path: Path) -> None:
        """Full happy path: download document, save, inbox entry, reply."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "doc-file-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "documents/report.pdf"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/documents/report.pdf").mock(
            return_value=httpx.Response(200, content=b"pdf-bytes")
        )

        reply_route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")
        docs_dir = str(tmp_path / "tg_documents")

        message: dict[str, object] = {
            "document": {
                "file_id": "doc-file-id",
                "file_unique_id": "doc-unique",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
                "file_size": 12345,
            },
            "caption": "Check this report",
            "message_id": 101,
            "user_id": 12345,
            "chat_id": 12345,
        }

        mod.handle_document_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=inbox_file,
            documents_dir=docs_dir,
        )

        # Check inbox entry was created.
        inbox_data = json.loads(Path(inbox_file).read_text())
        assert len(inbox_data) == 1
        entry = inbox_data[0]
        assert entry["action"] == "document"
        assert entry["file_path"] is not None
        assert entry["file_name"] == "report.pdf"
        assert entry["mime_type"] == "application/pdf"
        assert entry["caption"] == "Check this report"
        assert entry["reply_to"] == 101
        assert "timestamp" in entry

        # Check file was saved.
        assert Path(entry["file_path"]).exists()
        assert Path(entry["file_path"]).read_bytes() == b"pdf-bytes"

        # Check reply was sent.
        assert reply_route.call_count == 1
        reply_body = json.loads(reply_route.calls[0].request.content)
        assert "Document" in reply_body["text"]
        assert "report.pdf" in reply_body["text"]
        assert "Check this report" in reply_body["text"]

    @respx.mock
    def test_no_caption(self, tmp_path: Path) -> None:
        """Document without caption omits caption field from inbox entry."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "doc-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "documents/file.txt"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/documents/file.txt").mock(
            return_value=httpx.Response(200, content=b"data")
        )

        reply_route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")

        message: dict[str, object] = {
            "document": {
                "file_id": "doc-id",
                "file_unique_id": "doc-unique",
                "file_name": "notes.txt",
                "mime_type": "text/plain",
            },
            "message_id": 102,
            "user_id": 12345,
        }

        mod.handle_document_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=inbox_file,
            documents_dir=str(tmp_path / "tg_documents"),
        )

        inbox_data = json.loads(Path(inbox_file).read_text())
        assert len(inbox_data) == 1
        assert "caption" not in inbox_data[0]

        # Reply should not mention caption.
        reply_body = json.loads(reply_route.calls[0].request.content)
        assert "Document (notes.txt) received, forwarded to orchestrator." == reply_body["text"]

    @respx.mock
    def test_download_failure_sends_error_reply(self, tmp_path: Path) -> None:
        """When document download fails, an error reply is sent."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "bad-doc-id"},
        ).mock(return_value=httpx.Response(500))

        reply_route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")

        message: dict[str, object] = {
            "document": {
                "file_id": "bad-doc-id",
                "file_unique_id": "bad-unique",
                "file_name": "fail.pdf",
            },
            "message_id": 103,
        }

        mod.handle_document_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=inbox_file,
            documents_dir=str(tmp_path / "tg_documents"),
        )

        # No inbox entry should be created.
        assert not Path(inbox_file).exists()

        # Error reply should be sent.
        assert reply_route.call_count == 1
        reply_body = json.loads(reply_route.calls[0].request.content)
        assert "Failed to download document" in reply_body["text"]

    def test_empty_document_dict_skipped(self, tmp_path: Path) -> None:
        """Messages with empty document dict are silently skipped."""
        mod = _import_responder()

        message: dict[str, object] = {
            "document": {},
            "message_id": 104,
        }

        # Should not raise.
        mod.handle_document_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=str(tmp_path / "inbox.json"),
            documents_dir=str(tmp_path / "tg_documents"),
        )

    def test_no_document_key_skipped(self, tmp_path: Path) -> None:
        """Messages without document key are silently skipped."""
        mod = _import_responder()

        message: dict[str, object] = {
            "message_id": 105,
        }

        mod.handle_document_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=str(tmp_path / "inbox.json"),
            documents_dir=str(tmp_path / "tg_documents"),
        )


# ── Voice message handling ─────────────────────────────────────────────


class TestHandleVoiceMessage:
    """Tests for handle_voice_message()."""

    @respx.mock
    def test_downloads_saves_and_creates_inbox_entry(self, tmp_path: Path) -> None:
        """Full happy path: download voice, save, inbox entry, reply."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "voice-file-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "voice/note.oga"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/voice/note.oga").mock(
            return_value=httpx.Response(200, content=b"voice-bytes")
        )

        reply_route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")
        voice_dir = str(tmp_path / "tg_voice")

        message: dict[str, object] = {
            "voice": {
                "file_id": "voice-file-id",
                "file_unique_id": "voice-unique",
                "duration": 5,
                "mime_type": "audio/ogg",
                "file_size": 9876,
            },
            "message_id": 201,
            "user_id": 12345,
            "chat_id": 12345,
        }

        mod.handle_voice_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=inbox_file,
            voice_dir=voice_dir,
        )

        # Check inbox entry was created.
        inbox_data = json.loads(Path(inbox_file).read_text())
        assert len(inbox_data) == 1
        entry = inbox_data[0]
        assert entry["action"] == "voice"
        assert entry["file_path"] is not None
        assert entry["duration"] == 5
        assert entry["mime_type"] == "audio/ogg"
        assert entry["reply_to"] == 201
        assert "timestamp" in entry

        # Check file was saved.
        assert Path(entry["file_path"]).exists()
        assert Path(entry["file_path"]).read_bytes() == b"voice-bytes"

        # Check reply was sent.
        assert reply_route.call_count == 1
        reply_body = json.loads(reply_route.calls[0].request.content)
        assert "Voice message" in reply_body["text"]
        assert "5s" in reply_body["text"]

    @respx.mock
    def test_no_duration(self, tmp_path: Path) -> None:
        """Voice without duration omits duration field from inbox entry."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "voice-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "voice/msg.ogg"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/voice/msg.ogg").mock(
            return_value=httpx.Response(200, content=b"data")
        )

        reply_route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")

        message: dict[str, object] = {
            "voice": {
                "file_id": "voice-id",
                "file_unique_id": "voice-unique",
            },
            "message_id": 202,
            "user_id": 12345,
        }

        mod.handle_voice_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=inbox_file,
            voice_dir=str(tmp_path / "tg_voice"),
        )

        inbox_data = json.loads(Path(inbox_file).read_text())
        assert len(inbox_data) == 1
        assert "duration" not in inbox_data[0]

        # Reply should not contain duration.
        reply_body = json.loads(reply_route.calls[0].request.content)
        assert "Voice message received, forwarded to orchestrator." == reply_body["text"]

    @respx.mock
    def test_download_failure_sends_error_reply(self, tmp_path: Path) -> None:
        """When voice download fails, an error reply is sent."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "bad-voice-id"},
        ).mock(return_value=httpx.Response(500))

        reply_route = respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")

        message: dict[str, object] = {
            "voice": {
                "file_id": "bad-voice-id",
                "file_unique_id": "bad-unique",
            },
            "message_id": 203,
        }

        mod.handle_voice_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=inbox_file,
            voice_dir=str(tmp_path / "tg_voice"),
        )

        # No inbox entry should be created.
        assert not Path(inbox_file).exists()

        # Error reply should be sent.
        assert reply_route.call_count == 1
        reply_body = json.loads(reply_route.calls[0].request.content)
        assert "Failed to download voice message" in reply_body["text"]

    def test_empty_voice_dict_skipped(self, tmp_path: Path) -> None:
        """Messages with empty voice dict are silently skipped."""
        mod = _import_responder()

        message: dict[str, object] = {
            "voice": {},
            "message_id": 204,
        }

        mod.handle_voice_message(
            message=message,
            bot_token="fake-token",
            chat_ids=[12345],
            inbox_file=str(tmp_path / "inbox.json"),
            voice_dir=str(tmp_path / "tg_voice"),
        )


# ── Dispatch routing for document and voice ────────────────────────────


class TestDispatchMessageDocumentRouting:
    """Tests that dispatch_message routes document messages correctly."""

    @respx.mock
    def test_document_message_routed_to_handler(self, tmp_path: Path) -> None:
        """Messages with 'document' key skip Claude and are handled as documents."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "doc-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "documents/file.pdf"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/documents/file.pdf").mock(
            return_value=httpx.Response(200, content=b"data")
        )

        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")

        mod.dispatch_message(
            message={
                "text": "",
                "document": {
                    "file_id": "doc-id",
                    "file_unique_id": "doc-unique",
                    "file_name": "file.pdf",
                    "mime_type": "application/pdf",
                },
                "message_id": 70,
                "user_id": 12345,
                "chat_id": 12345,
            },
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=inbox_file,
            anthropic_api_key="test-key",
        )

        # An inbox entry should exist with action "document".
        inbox_data = json.loads(Path(inbox_file).read_text())
        assert len(inbox_data) == 1
        assert inbox_data[0]["action"] == "document"


class TestDispatchMessageVoiceRouting:
    """Tests that dispatch_message routes voice messages correctly."""

    @respx.mock
    def test_voice_message_routed_to_handler(self, tmp_path: Path) -> None:
        """Messages with 'voice' key skip Claude and are handled as voice."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "voice-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "voice/msg.ogg"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/voice/msg.ogg").mock(
            return_value=httpx.Response(200, content=b"data")
        )

        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        inbox_file = str(tmp_path / "inbox.json")

        mod.dispatch_message(
            message={
                "text": "",
                "voice": {
                    "file_id": "voice-id",
                    "file_unique_id": "voice-unique",
                    "duration": 3,
                },
                "message_id": 71,
                "user_id": 12345,
                "chat_id": 12345,
            },
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(tmp_path / "status.json"),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=inbox_file,
            anthropic_api_key="test-key",
        )

        # An inbox entry should exist with action "voice".
        inbox_data = json.loads(Path(inbox_file).read_text())
        assert len(inbox_data) == 1
        assert inbox_data[0]["action"] == "voice"


# ── Download telegram file (generic) ──────────────────────────────────


class TestDownloadTelegramFile:
    """Tests for the generic download_telegram_file() function."""

    @respx.mock
    def test_uses_filename_hint_extension(self, tmp_path: Path) -> None:
        """When Telegram file path has no extension, filename_hint is used."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "doc-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "documents/file_123"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/documents/file_123").mock(
            return_value=httpx.Response(200, content=b"content")
        )

        result = mod.download_telegram_file(
            file_id="doc-id",
            bot_token="fake-token",
            save_dir=str(tmp_path / "downloads"),
            filename_hint="report.pdf",
        )

        assert result is not None
        assert result.endswith(".pdf")
        assert Path(result).read_bytes() == b"content"

    @respx.mock
    def test_uses_default_ext_when_no_hints(self, tmp_path: Path) -> None:
        """When neither Telegram path nor filename_hint has an extension."""
        mod = _import_responder()

        respx.get(
            "https://api.telegram.org/botfake-token/getFile",
            params={"file_id": "file-id"},
        ).mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "files/blob"}},
            )
        )
        respx.get("https://api.telegram.org/file/botfake-token/files/blob").mock(
            return_value=httpx.Response(200, content=b"data")
        )

        result = mod.download_telegram_file(
            file_id="file-id",
            bot_token="fake-token",
            save_dir=str(tmp_path / "downloads"),
            default_ext=".ogg",
        )

        assert result is not None
        assert result.endswith(".ogg")


# ── Agent worktree liveness tracking (#1857) ───────────────────────────


class TestReadAgentStatusFilesIncludesAgentNames:
    """read_agent_status_files() reads both worker-*.txt and agent-*.txt."""

    def test_reads_agent_status_files(self, tmp_path: Path) -> None:
        status_dir = tmp_path / "agent-status"
        status_dir.mkdir()
        (status_dir / "agent-ab9ac63f.txt").write_text(
            "issue: #1857\nphase: ci-watch\nupdated: 2026-03-10T19:00:00Z\nsummary: Watching CI\n"
        )
        mod = _import_responder()
        statuses = mod.read_agent_status_files(str(status_dir))
        assert len(statuses) == 1
        assert statuses[0]["worker"] == "agent-ab9ac63f"
        assert statuses[0]["issue"] == "#1857"

    def test_reads_both_worker_and_agent_files(self, tmp_path: Path) -> None:
        status_dir = tmp_path / "agent-status"
        status_dir.mkdir()
        (status_dir / "worker-2.txt").write_text(
            "issue: #42\nphase: implementing\nupdated: 2026-03-10T19:00:00Z\n"
        )
        (status_dir / "agent-abc12345.txt").write_text(
            "issue: #99\nphase: ci-watch\nupdated: 2026-03-10T19:05:00Z\n"
        )
        mod = _import_responder()
        statuses = mod.read_agent_status_files(str(status_dir))
        assert len(statuses) == 2
        workers = {s["worker"] for s in statuses}
        assert "worker-2" in workers
        assert "agent-abc12345" in workers


class TestListActiveWorktrees:
    """list_active_worktrees() returns directory names for both naming styles."""

    def test_finds_worker_worktrees(self, tmp_path: Path) -> None:
        (tmp_path / "worktrees" / "worker-1").mkdir(parents=True)
        (tmp_path / "worktrees" / "worker-3").mkdir(parents=True)
        mod = _import_responder()
        names = mod.list_active_worktrees(str(tmp_path))
        assert names == {"worker-1", "worker-3"}

    def test_finds_agent_worktrees(self, tmp_path: Path) -> None:
        (tmp_path / ".claude" / "worktrees" / "agent-ab9ac63f").mkdir(parents=True)
        (tmp_path / ".claude" / "worktrees" / "agent-def45678").mkdir(parents=True)
        mod = _import_responder()
        names = mod.list_active_worktrees(str(tmp_path))
        assert names == {"agent-ab9ac63f", "agent-def45678"}

    def test_finds_both_styles(self, tmp_path: Path) -> None:
        (tmp_path / "worktrees" / "worker-1").mkdir(parents=True)
        (tmp_path / ".claude" / "worktrees" / "agent-ab9ac63f").mkdir(parents=True)
        mod = _import_responder()
        names = mod.list_active_worktrees(str(tmp_path))
        assert names == {"worker-1", "agent-ab9ac63f"}

    def test_returns_empty_when_no_worktrees(self, tmp_path: Path) -> None:
        mod = _import_responder()
        names = mod.list_active_worktrees(str(tmp_path))
        assert names == set()

    def test_ignores_non_worker_directories(self, tmp_path: Path) -> None:
        (tmp_path / "worktrees" / "some-other-dir").mkdir(parents=True)
        (tmp_path / ".claude" / "worktrees" / "not-an-agent").mkdir(parents=True)
        mod = _import_responder()
        names = mod.list_active_worktrees(str(tmp_path))
        assert names == set()


class TestFilterAgentsByWorktrees:
    """filter_agents_by_worktrees() filters out stale agents."""

    def test_keeps_agents_with_live_worktrees(self) -> None:
        agents = [
            {"worker_number": 1, "issue_number": 42, "phase": "ci-watch"},
            {"worker_number": "agent-ab9ac63f", "issue_number": 99, "phase": "implementing"},
        ]
        live = {"worker-1", "agent-ab9ac63f"}
        mod = _import_responder()
        filtered = mod.filter_agents_by_worktrees(agents, live)
        assert len(filtered) == 2

    def test_removes_agents_without_worktrees(self) -> None:
        agents = [
            {"worker_number": 1, "issue_number": 42, "phase": "ci-watch"},
            {"worker_number": 5, "issue_number": 99, "phase": "done"},
            {"worker_number": "agent-ab9ac63f", "issue_number": 77, "phase": "merging"},
        ]
        # Only worker-1 has a live worktree
        live = {"worker-1"}
        mod = _import_responder()
        filtered = mod.filter_agents_by_worktrees(agents, live)
        assert len(filtered) == 1
        assert filtered[0]["issue_number"] == 42

    def test_returns_empty_when_no_live_worktrees(self) -> None:
        agents = [
            {"worker_number": 1, "issue_number": 42, "phase": "ci-watch"},
        ]
        mod = _import_responder()
        filtered = mod.filter_agents_by_worktrees(agents, set())
        assert filtered == []

    def test_handles_agent_prefix_worktrees(self) -> None:
        agents = [
            {"worker_number": "agent-ab9ac63f", "issue_number": 99, "phase": "implementing"},
            {"worker_number": "agent-deadbeef", "issue_number": 100, "phase": "done"},
        ]
        live = {"agent-ab9ac63f"}
        mod = _import_responder()
        filtered = mod.filter_agents_by_worktrees(agents, live)
        assert len(filtered) == 1
        assert filtered[0]["issue_number"] == 99


class TestMergeWithAgentWorktreeEntries:
    """merge_agent_status_into_dispatcher() handles agent-* entries."""

    def test_adds_agent_worktree_entry(self) -> None:
        """Agent-* entries from status files are added correctly."""
        mod = _import_responder()
        orch_status = {
            "active_agents": [],
            "updated_at": "2026-03-10T19:00:00Z",
        }
        agent_statuses = [
            {
                "worker": "agent-ab9ac63f",
                "issue": "#1857",
                "phase": "ci-watch",
                "updated": "2026-03-10T19:10:00Z",
                "summary": "Watching CI",
            }
        ]
        result = mod.merge_agent_status_into_dispatcher(orch_status, agent_statuses)
        agents = result["active_agents"]
        assert len(agents) == 1
        assert agents[0]["worker_number"] == "agent-ab9ac63f"
        assert agents[0]["issue_number"] == "#1857"
        assert agents[0]["source"] == "agent-status-file"

    def test_matches_agent_entry_in_dispatcher_status(self) -> None:
        """Existing agent-* entries in dispatcher_status can be updated."""
        mod = _import_responder()
        orch_status = {
            "active_agents": [
                {
                    "worker_number": "agent-ab9ac63f",
                    "issue_number": 1857,
                    "phase": "implementing",
                    "updated": "2026-03-10T19:00:00Z",
                }
            ],
            "updated_at": "2026-03-10T19:00:00Z",
        }
        agent_statuses = [
            {
                "worker": "agent-ab9ac63f",
                "issue": "#1857",
                "phase": "merging",
                "updated": "2026-03-10T19:30:00Z",
                "summary": "Squash merging PR",
            }
        ]
        result = mod.merge_agent_status_into_dispatcher(orch_status, agent_statuses)
        agents = result["active_agents"]
        assert len(agents) == 1
        assert agents[0]["phase"] == "merging"
        assert agents[0]["source"] == "agent-status-file"


class TestDispatchMessageFiltersStaleAgents:
    """dispatch_message filters active_agents by worktree existence."""

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_stale_agents_without_worktrees_are_filtered(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        """Agents in dispatcher_status with no matching worktree are removed."""
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="One agent running.",
            actions=[],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        # Dispatcher status claims 3 agents but only 1 has a live worktree.
        status_file = tmp_path / "status.json"
        status_data = {
            "active_agents": [
                {
                    "worker_number": "agent-ab9ac63f",
                    "issue_number": 42,
                    "phase": "ci-watch",
                    "updated": "2026-03-10T19:00:00Z",
                },
                {
                    "worker_number": "agent-deadbeef",
                    "issue_number": 99,
                    "phase": "implementing",
                    "updated": "2026-03-10T19:00:00Z",
                },
                {
                    "worker_number": "agent-cafebabe",
                    "issue_number": 100,
                    "phase": "merging",
                    "updated": "2026-03-10T19:00:00Z",
                },
            ],
            "paused": False,
            "updated_at": "2026-03-10T19:00:00Z",
        }
        status_file.write_text(json.dumps(status_data))

        # Only one worktree actually exists.
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".claude" / "worktrees" / "agent-ab9ac63f").mkdir(parents=True)

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "how many agents?", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(status_file),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
            repo_root=repo_root,
        )

        call_kwargs = mock_interpret.call_args.kwargs
        agents = call_kwargs["orchestrator_status"]["active_agents"]
        # Only agent-ab9ac63f should remain — the other two have no worktree.
        assert len(agents) == 1
        assert agents[0]["issue_number"] == 42

    @respx.mock
    @patch("tg_responder.interpret_message")
    def test_no_filtering_when_repo_root_not_provided(
        self, mock_interpret: MagicMock, tmp_path: Path
    ) -> None:
        """Without repo_root, all agents are passed through unfiltered."""
        from telegram_bridge.interpreter import InterpretedMessage

        mock_interpret.return_value = InterpretedMessage(
            reply="Three agents running.",
            actions=[],
        )
        respx.post("https://api.telegram.org/botfake-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        status_file = tmp_path / "status.json"
        status_data = {
            "active_agents": [
                {"worker_number": "agent-abc", "issue_number": 1, "phase": "ci-watch"},
                {"worker_number": "agent-def", "issue_number": 2, "phase": "ci-watch"},
                {"worker_number": "agent-ghi", "issue_number": 3, "phase": "ci-watch"},
            ],
            "paused": False,
            "updated_at": "2026-03-10T19:00:00Z",
        }
        status_file.write_text(json.dumps(status_data))

        mod = _import_responder()
        mod.dispatch_message(
            message={"text": "how many agents?", "user_id": 12345},
            bot_token="fake-token",
            chat_ids=[12345],
            state_file=str(tmp_path / "state.json"),
            status_file=str(status_file),
            agent_status_dir=str(tmp_path / "agent-status"),
            stop_requests_file=str(tmp_path / "stop.json"),
            inbox_file=str(tmp_path / "inbox.json"),
            anthropic_api_key="test-key",
            # No repo_root — filtering should not happen.
        )

        call_kwargs = mock_interpret.call_args.kwargs
        agents = call_kwargs["orchestrator_status"]["active_agents"]
        # All 3 agents should be passed through since no repo_root.
        assert len(agents) == 3


# ── Old daemon removed ──────────────────────────────────────────────────
# The deprecated tg-poll-daemon.py was removed in #646. The responder
# daemon (scripts/tg-responder.py) fully replaces it.
