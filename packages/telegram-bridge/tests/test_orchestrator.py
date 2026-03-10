"""Tests for the orchestrator integration module."""

from __future__ import annotations

import json

import boto3
import httpx
import respx
from moto import mock_aws

from telegram_bridge import Command, CommandKind, OrchestratorBridge, TelegramBridge
from telegram_bridge.orchestrator import parse_command

# ── Helpers ──────────────────────────────────────────────────────────────


def _setup_secret(
    *,
    token: str = "fake-bot-token",
    user_ids: list[int] | None = None,
) -> None:
    """Create the Secrets Manager secret used by TelegramBridge."""
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


def _make_bridge(*, sqs_queue_url: str | None = None) -> TelegramBridge:
    return TelegramBridge(region_name="us-west-2", sqs_queue_url=sqs_queue_url)


# ── parse_command() ──────────────────────────────────────────────────────


class TestParseCommand:
    def test_status(self) -> None:
        cmd = parse_command("status")
        assert cmd.kind == CommandKind.STATUS

    def test_status_case_insensitive(self) -> None:
        cmd = parse_command("  Status  ")
        assert cmd.kind == CommandKind.STATUS

    def test_pause(self) -> None:
        cmd = parse_command("pause")
        assert cmd.kind == CommandKind.PAUSE

    def test_resume(self) -> None:
        cmd = parse_command("resume")
        assert cmd.kind == CommandKind.RESUME

    def test_start_with_hash(self) -> None:
        cmd = parse_command("start #42")
        assert cmd.kind == CommandKind.START
        assert cmd.issue_number == 42

    def test_start_without_hash(self) -> None:
        cmd = parse_command("start 99")
        assert cmd.kind == CommandKind.START
        assert cmd.issue_number == 99

    def test_stop_with_hash(self) -> None:
        cmd = parse_command("stop #10")
        assert cmd.kind == CommandKind.STOP
        assert cmd.issue_number == 10

    def test_stop_without_hash(self) -> None:
        cmd = parse_command("Stop 7")
        assert cmd.kind == CommandKind.STOP
        assert cmd.issue_number == 7

    def test_start_invalid_number_is_free_text(self) -> None:
        cmd = parse_command("start abc")
        assert cmd.kind == CommandKind.FREE_TEXT

    def test_free_text(self) -> None:
        cmd = parse_command("run the scrapers please")
        assert cmd.kind == CommandKind.FREE_TEXT
        assert cmd.raw_text == "run the scrapers please"

    def test_empty_string_is_free_text(self) -> None:
        cmd = parse_command("")
        assert cmd.kind == CommandKind.FREE_TEXT


# ── Lifecycle notifications ──────────────────────────────────────────────


class TestLifecycleNotifications:
    @respx.mock
    async def test_session_started(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.session_started()
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert "session started" in body["text"].lower()

    @respx.mock
    async def test_session_ended(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.session_ended()
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert "session ended" in body["text"].lower()

    @respx.mock
    async def test_task_started_sends_status_update(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.task_started(issue_number=42, title="Fix the widget", worker=3)
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert "#42" in body["text"]

    @respx.mock
    async def test_task_started_tracks_worker(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.task_started(issue_number=42, title="Fix the widget", worker=3)

            workers = orch.get_workers()
            assert len(workers) == 1
            assert workers[0].issue_number == 42
            assert workers[0].worker_number == 3

    @respx.mock
    async def test_task_completed_removes_worker(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.task_started(issue_number=42, title="Fix the widget", worker=3)
            await orch.task_completed(issue_number=42, summary="PR merged.", worker=3)

            assert orch.get_workers() == []

    @respx.mock
    async def test_task_failed_removes_worker(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.task_started(issue_number=42, title="Fix the widget", worker=3)
            await orch.task_failed(issue_number=42, error="CI failed.", worker=3)

            assert orch.get_workers() == []


# ── No-op mode ───────────────────────────────────────────────────────────


class TestNoOpMode:
    async def test_all_methods_noop_when_disabled(self) -> None:
        """All orchestrator methods should silently succeed when bridge is disabled."""
        with mock_aws():
            # No secret → bridge will be disabled
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)

            # These should all succeed silently
            await orch.session_started()
            await orch.session_ended()
            await orch.task_started(issue_number=1, title="Test", worker=1)
            await orch.task_completed(issue_number=1, summary="Done.", worker=1)
            await orch.task_failed(issue_number=2, error="Err.", worker=2)
            await orch.reply_status()

            commands = await orch.poll_commands()
            assert commands == []


# ── Command polling ──────────────────────────────────────────────────────


class TestPollCommands:
    async def test_poll_parses_messages_into_commands(self) -> None:
        with mock_aws():
            _setup_secret()
            queue_url = _setup_sqs()

            sqs = boto3.client("sqs", region_name="us-west-2")
            for text in ["status", "start #10", "hello there"]:
                sqs.send_message(
                    QueueUrl=queue_url,
                    MessageBody=json.dumps(
                        {
                            "text": text,
                            "user_id": 12345,
                            "timestamp": "2026-03-09T20:00:00+00:00",
                        }
                    ),
                )

            bridge = _make_bridge(sqs_queue_url=queue_url)
            orch = OrchestratorBridge(bridge=bridge)
            commands = await orch.poll_commands()

            assert len(commands) == 3
            kinds = {c.kind for c in commands}
            assert CommandKind.STATUS in kinds
            assert CommandKind.START in kinds
            assert CommandKind.FREE_TEXT in kinds

    async def test_poll_returns_empty_when_queue_empty(self) -> None:
        with mock_aws():
            _setup_secret()
            queue_url = _setup_sqs()

            bridge = _make_bridge(sqs_queue_url=queue_url)
            orch = OrchestratorBridge(bridge=bridge)
            commands = await orch.poll_commands()
            assert commands == []


# ── handle_command() ────────────────────────────────────────────────────


class TestHandleCommand:
    @respx.mock
    async def test_handle_status(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            result = await orch.handle_command(Command(kind=CommandKind.STATUS))
            await bridge.close()

            assert result["handled"] is True
            assert "Status sent" in result["reply"]

    @respx.mock
    async def test_handle_pause_sets_flag(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            assert orch.paused is False
            await orch.handle_command(Command(kind=CommandKind.PAUSE))
            assert orch.paused is True

    @respx.mock
    async def test_handle_resume_clears_flag(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, paused=True)
            await orch.handle_command(Command(kind=CommandKind.RESUME))
            assert orch.paused is False

    @respx.mock
    async def test_handle_start_returns_action(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(kind=CommandKind.START, issue_number=42, raw_text="start #42")
            )
            await bridge.close()

            assert result["action"] == "start_task"
            assert result["issue_number"] == 42

    @respx.mock
    async def test_handle_stop_returns_action(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(kind=CommandKind.STOP, issue_number=10, raw_text="stop #10")
            )
            await bridge.close()

            assert result["action"] == "stop_task"
            assert result["issue_number"] == 10

    @respx.mock
    async def test_handle_free_text_forwards(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(kind=CommandKind.FREE_TEXT, raw_text="check the scrapers")
            )
            await bridge.close()

            assert result["action"] == "forward"
            assert result["text"] == "check the scrapers"


# ── reply_status() ──────────────────────────────────────────────────────


class TestReplyStatus:
    @respx.mock
    async def test_no_workers_sends_no_active_tasks(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.reply_status()
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert "no active tasks" in body["text"].lower()

    @respx.mock
    async def test_with_workers_lists_them(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.task_started(issue_number=42, title="Fix widget", worker=3)

            # Reset route call count
            route.reset()
            route.mock(return_value=httpx.Response(200, json={"ok": True}))

            await orch.reply_status()
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert "#42" in body["text"]
            assert "Worker\\-3" in body["text"] or "Worker-3" in body["text"]

    @respx.mock
    async def test_paused_indicator_in_status(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, paused=True)
            await orch.reply_status()
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert "paused" in body["text"].lower()


# ── process_commands() (end-to-end) ─────────────────────────────────────


class TestProcessCommands:
    @respx.mock
    async def test_process_polls_and_handles(self) -> None:
        with mock_aws():
            _setup_secret()
            queue_url = _setup_sqs()

            sqs = boto3.client("sqs", region_name="us-west-2")
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(
                    {
                        "text": "pause",
                        "user_id": 12345,
                        "timestamp": "2026-03-09T20:00:00+00:00",
                    }
                ),
            )

            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge(sqs_queue_url=queue_url)
            orch = OrchestratorBridge(bridge=bridge)
            results = await orch.process_commands()
            await bridge.close()

            assert len(results) == 1
            assert orch.paused is True


# ── update_worker() ────────────────────────────────────────────────────


class TestUpdateWorker:
    @respx.mock
    async def test_update_worker_phase(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.task_started(issue_number=42, title="Fix widget", worker=3)

            orch.update_worker(3, phase="ci-watch")
            workers = orch.get_workers()
            assert workers[0].phase == "ci-watch"

    def test_update_nonexistent_worker_is_noop(self) -> None:
        with mock_aws():
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            orch.update_worker(99, phase="done")  # Should not raise
            assert orch.get_workers() == []
