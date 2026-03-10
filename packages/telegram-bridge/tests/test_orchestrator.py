"""Tests for the orchestrator integration module."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
            # Should contain a clickable link to issue #42.
            assert "https://github.com/judgemind/judgemind/issues/42" in body["text"]
            # Should say "Issue", not "Task".
            assert "Issue" in body["text"]
            assert "Task" not in body["text"]

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

    @respx.mock
    async def test_handle_free_text_sets_needs_reply(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(kind=CommandKind.FREE_TEXT, raw_text="how are scrapers doing?")
            )
            await bridge.close()

            assert result["needs_reply"] is True

    @respx.mock
    async def test_handle_non_free_text_has_no_needs_reply(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            result = await orch.handle_command(Command(kind=CommandKind.STATUS))
            await bridge.close()

            assert "needs_reply" not in result


# ── reply() ─────────────────────────────────────────────────────────


class TestReply:
    @respx.mock
    async def test_reply_sends_text_via_telegram(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.reply("All scrapers are healthy.")
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert "scrapers are healthy" in body["text"].lower()

    async def test_reply_noop_when_disabled(self) -> None:
        with mock_aws():
            # No secret → bridge disabled
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            # Should not raise
            await orch.reply("This should be silently dropped.")

    @respx.mock
    async def test_reply_linkifies_github_refs(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            await orch.reply("See #42 for details.")
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert "https://github.com/judgemind/judgemind/issues/42" in body["text"]


# ── reply_status() ──────────────────────────────────────────────────────


class TestReplyStatus:
    @respx.mock
    async def test_no_workers_sends_no_active_issues(self) -> None:
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
            assert "no active issues" in body["text"].lower()

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
            # Issue reference should be a clickable link.
            assert "https://github.com/judgemind/judgemind/issues/42" in body["text"]
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


# ── Background polling ─────────────────────────────────────────────────


class TestBackgroundPolling:
    async def test_start_polling_sets_polling_flag(self) -> None:
        with mock_aws():
            _setup_secret()
            queue_url = _setup_sqs()

            bridge = _make_bridge(sqs_queue_url=queue_url)
            orch = OrchestratorBridge(bridge=bridge)

            assert orch.polling is False
            await orch.start_polling(interval=100.0)
            assert orch.polling is True

            await orch.stop_polling()
            assert orch.polling is False

    async def test_stop_polling_is_idempotent(self) -> None:
        with mock_aws():
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)

            # Should not raise even when no task is running.
            await orch.stop_polling()
            await orch.stop_polling()

    async def test_poll_loop_accumulates_commands(self) -> None:
        with mock_aws():
            _setup_secret()
            queue_url = _setup_sqs()

            sqs = boto3.client("sqs", region_name="us-west-2")
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(
                    {
                        "text": "status",
                        "user_id": 12345,
                        "timestamp": "2026-03-09T20:00:00+00:00",
                    }
                ),
            )

            bridge = _make_bridge(sqs_queue_url=queue_url)
            orch = OrchestratorBridge(bridge=bridge)

            # Start polling with a short interval.
            await orch.start_polling(interval=0.05)

            # Give the loop time to run at least once.
            await asyncio.sleep(0.2)

            commands = orch.drain_pending_commands()
            assert len(commands) >= 1
            assert commands[0].kind == CommandKind.STATUS

            await orch.stop_polling()

    async def test_drain_clears_pending(self) -> None:
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

            bridge = _make_bridge(sqs_queue_url=queue_url)
            orch = OrchestratorBridge(bridge=bridge)

            await orch.start_polling(interval=0.05)
            await asyncio.sleep(0.2)

            first_drain = orch.drain_pending_commands()
            assert len(first_drain) >= 1

            # Second drain should be empty — pending was cleared.
            second_drain = orch.drain_pending_commands()
            assert second_drain == []

            await orch.stop_polling()

    async def test_restart_polling_replaces_task(self) -> None:
        with mock_aws():
            _setup_secret()
            queue_url = _setup_sqs()

            bridge = _make_bridge(sqs_queue_url=queue_url)
            orch = OrchestratorBridge(bridge=bridge)

            await orch.start_polling(interval=100.0)
            first_task = orch._poll_task

            await orch.start_polling(interval=50.0)
            second_task = orch._poll_task

            assert first_task is not second_task
            assert first_task is not None and first_task.done()
            assert orch.polling is True

            await orch.stop_polling()

    async def test_polling_noop_when_disabled(self) -> None:
        with mock_aws():
            # No secret → bridge disabled
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)

            await orch.start_polling(interval=0.05)
            await asyncio.sleep(0.2)

            # Should accumulate no commands (poll returns []).
            commands = orch.drain_pending_commands()
            assert commands == []

            await orch.stop_polling()


# ── File-based inbox (read_inbox) ──────────────────────────────────────


class TestReadInbox:
    def test_returns_empty_when_no_inbox_path(self) -> None:
        with mock_aws():
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            assert orch.read_inbox() == []

    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox = str(tmp_path / "nonexistent.json")
            orch = OrchestratorBridge(bridge=bridge, inbox_path=inbox)
            assert orch.read_inbox() == []

    def test_returns_empty_when_file_empty(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text("")
            orch = OrchestratorBridge(bridge=bridge, inbox_path=str(inbox_file))
            assert orch.read_inbox() == []

    def test_reads_and_parses_commands(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {"text": "status", "user_id": 123},
                        {"text": "start #42", "user_id": 123},
                        {"text": "hello world", "user_id": 123},
                    ]
                )
            )
            orch = OrchestratorBridge(bridge=bridge, inbox_path=str(inbox_file))
            commands = orch.read_inbox()

            assert len(commands) == 3
            assert commands[0].kind == CommandKind.STATUS
            assert commands[1].kind == CommandKind.START
            assert commands[1].issue_number == 42
            assert commands[2].kind == CommandKind.FREE_TEXT

    def test_clears_file_after_reading(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(json.dumps([{"text": "status"}]))
            orch = OrchestratorBridge(bridge=bridge, inbox_path=str(inbox_file))

            commands = orch.read_inbox()
            assert len(commands) == 1

            # File should be empty now.
            assert inbox_file.read_text().strip() == ""

            # Second read returns empty.
            assert orch.read_inbox() == []

    def test_handles_corrupt_file(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text("not valid json{{{")
            orch = OrchestratorBridge(bridge=bridge, inbox_path=str(inbox_file))

            commands = orch.read_inbox()
            assert commands == []
            # File should be cleared.
            assert inbox_file.read_text().strip() == ""

    def test_pause_command_from_inbox(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(json.dumps([{"text": "pause"}]))
            orch = OrchestratorBridge(bridge=bridge, inbox_path=str(inbox_file))

            commands = orch.read_inbox()
            assert len(commands) == 1
            assert commands[0].kind == CommandKind.PAUSE


# ── State persistence ──────────────────────────────────────────────────


class TestStatePersistence:
    @respx.mock
    async def test_task_started_persists_to_file(self, tmp_path: Path) -> None:
        """task_started() writes worker state to the state file."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, state_file=state_file)
            await orch.task_started(issue_number=42, title="Fix the widget", worker=3)

            # The file should exist and contain the worker.
            data = json.loads(Path(state_file).read_text())
            assert "3" in data["workers"]
            assert data["workers"]["3"]["issue_number"] == 42
            assert data["workers"]["3"]["issue_title"] == "Fix the widget"

    @respx.mock
    async def test_new_instance_loads_persisted_workers(self, tmp_path: Path) -> None:
        """A fresh OrchestratorBridge instance loads workers from the state file."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")

            # First instance: start a task.
            bridge1 = _make_bridge()
            orch1 = OrchestratorBridge(bridge=bridge1, state_file=state_file)
            await orch1.task_started(issue_number=42, title="Fix the widget", worker=3)

            # Second instance: should pick up the worker from disk.
            bridge2 = _make_bridge()
            orch2 = OrchestratorBridge(bridge=bridge2, state_file=state_file)
            workers = orch2.get_workers()
            assert len(workers) == 1
            assert workers[0].issue_number == 42
            assert workers[0].worker_number == 3

    @respx.mock
    async def test_task_completed_removes_worker_from_file(self, tmp_path: Path) -> None:
        """task_completed() removes the worker from the persisted state."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, state_file=state_file)
            await orch.task_started(issue_number=42, title="Fix the widget", worker=3)
            await orch.task_completed(issue_number=42, summary="Done.", worker=3)

            data = json.loads(Path(state_file).read_text())
            assert data["workers"] == {}

    @respx.mock
    async def test_task_failed_removes_worker_from_file(self, tmp_path: Path) -> None:
        """task_failed() removes the worker from the persisted state."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, state_file=state_file)
            await orch.task_started(issue_number=42, title="Fix the widget", worker=3)
            await orch.task_failed(issue_number=42, error="CI broke.", worker=3)

            data = json.loads(Path(state_file).read_text())
            assert data["workers"] == {}

    @respx.mock
    async def test_update_worker_persists_phase(self, tmp_path: Path) -> None:
        """update_worker() saves the new phase to disk."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, state_file=state_file)
            await orch.task_started(issue_number=42, title="Fix widget", worker=3)
            orch.update_worker(3, phase="ci-watch")

            data = json.loads(Path(state_file).read_text())
            assert data["workers"]["3"]["phase"] == "ci-watch"

    @respx.mock
    async def test_pause_persists_to_file(self, tmp_path: Path) -> None:
        """handle_command(PAUSE) persists paused=True to disk."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, state_file=state_file)
            await orch.handle_command(Command(kind=CommandKind.PAUSE))

            data = json.loads(Path(state_file).read_text())
            assert data["paused"] is True

    @respx.mock
    async def test_resume_persists_to_file(self, tmp_path: Path) -> None:
        """handle_command(RESUME) persists paused=False to disk."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, state_file=state_file, paused=True)
            await orch.handle_command(Command(kind=CommandKind.RESUME))

            data = json.loads(Path(state_file).read_text())
            assert data["paused"] is False

    @respx.mock
    async def test_new_instance_loads_paused_flag(self, tmp_path: Path) -> None:
        """A fresh instance loads the paused flag from the state file."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")
            bridge1 = _make_bridge()
            orch1 = OrchestratorBridge(bridge=bridge1, state_file=state_file)
            await orch1.handle_command(Command(kind=CommandKind.PAUSE))

            bridge2 = _make_bridge()
            orch2 = OrchestratorBridge(bridge=bridge2, state_file=state_file)
            assert orch2.paused is True

    @respx.mock
    async def test_session_ended_removes_state_file(self, tmp_path: Path) -> None:
        """session_ended() cleans up the state file."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, state_file=state_file)
            await orch.task_started(issue_number=42, title="Fix widget", worker=3)
            assert Path(state_file).exists()

            await orch.session_ended()
            assert not Path(state_file).exists()

    def test_missing_state_file_starts_fresh(self, tmp_path: Path) -> None:
        """If the state file doesn't exist, instance starts with empty state."""
        with mock_aws():
            state_file = str(tmp_path / "nonexistent.json")
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, state_file=state_file)
            assert orch.get_workers() == []
            assert orch.paused is False

    def test_corrupt_state_file_starts_fresh(self, tmp_path: Path) -> None:
        """If the state file is corrupt, instance starts with empty state."""
        with mock_aws():
            state_file = tmp_path / "corrupt.json"
            state_file.write_text("not valid json{{{")

            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge, state_file=str(state_file))
            assert orch.get_workers() == []
            assert orch.paused is False

    def test_no_state_file_means_in_memory_only(self) -> None:
        """Without state_file, behavior is unchanged (in-memory only)."""
        with mock_aws():
            bridge = _make_bridge()
            orch = OrchestratorBridge(bridge=bridge)
            # No state_file set — _save_state and _load_state should be no-ops.
            orch._save_state()
            orch._load_state()
            assert orch.get_workers() == []

    @respx.mock
    async def test_reply_status_uses_persisted_workers(self, tmp_path: Path) -> None:
        """A fresh instance's reply_status() reports workers loaded from disk."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")

            # First process: start a task.
            bridge1 = _make_bridge()
            orch1 = OrchestratorBridge(bridge=bridge1, state_file=state_file)
            await orch1.task_started(issue_number=42, title="Fix widget", worker=3)

            # Second process: reply_status should include the worker.
            route.reset()
            route.mock(return_value=httpx.Response(200, json={"ok": True}))

            bridge2 = _make_bridge()
            orch2 = OrchestratorBridge(bridge=bridge2, state_file=state_file)
            await orch2.reply_status()
            await bridge2.close()

            body = json.loads(route.calls[0].request.content)
            assert "Worker-3" in body["text"] or "Worker\\-3" in body["text"]
            assert "#42" in body["text"] or "42" in body["text"]
