"""Tests for the dispatcher integration module."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import boto3
import httpx
import respx
from moto import mock_aws

from telegram_bridge import (
    Command,
    CommandKind,
    DispatcherBridge,
    PendingReply,
    TelegramBridge,
    create_dispatcher_bridge,
)
from telegram_bridge.dispatcher import (
    InstructionKind,
    _parse_instruction,
    _worker_label,
    parse_command,
)

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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)

            # These should all succeed silently
            await orch.session_started()
            await orch.session_ended()
            await orch.task_started(issue_number=1, title="Test", worker=1)
            await orch.task_completed(issue_number=1, summary="Done.", worker=1)
            await orch.task_failed(issue_number=2, error="Err.", worker=2)
            await orch.reply_status()
            await orch.notify("Test notification.")
            await orch.status_update(task="#1", state="complete", details="Done.")


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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge, paused=True)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
            await orch.reply("All scrapers are healthy.")
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert "scrapers are healthy" in body["text"].lower()

    async def test_reply_noop_when_disabled(self) -> None:
        with mock_aws():
            # No secret → bridge disabled
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
            await orch.reply("See #42 for details.")
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert "https://github.com/judgemind/judgemind/issues/42" in body["text"]


# ── notify() pass-through ────────────────────────────────────────────────


class TestNotifyPassthrough:
    """Tests for DispatcherBridge.notify() delegation to TelegramBridge."""

    @respx.mock
    async def test_notify_delegates_to_inner_bridge(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.notify("Scraper run complete.")
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert "Scraper run complete" in body["text"]

    async def test_notify_noop_when_disabled(self) -> None:
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            # Should not raise
            await orch.notify("This should be silently dropped.")

    @respx.mock
    async def test_notify_passes_repo_kwarg(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.notify("See #10 for details.", repo="myorg/myrepo")
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert "https://github.com/myorg/myrepo/issues/10" in body["text"]


# ── status_update() pass-through ─────────────────────────────────────────


class TestStatusUpdatePassthrough:
    """Tests for DispatcherBridge.status_update() delegation to TelegramBridge."""

    @respx.mock
    async def test_status_update_delegates_to_inner_bridge(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.status_update(task="#99", state="complete", details="All done.")
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            # The formatted card should contain the task and details
            assert "#99" in body["text"] or "99" in body["text"]

    async def test_status_update_noop_when_disabled(self) -> None:
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            # Should not raise
            await orch.status_update(task="#1", state="failed", details="Error occurred.")

    @respx.mock
    async def test_status_update_passes_repo_kwarg(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.status_update(
                task="#5", state="in_progress", details="Working", repo="myorg/myrepo"
            )
            await bridge.close()

            assert route.call_count == 1


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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge)
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
            orch = DispatcherBridge(bridge=bridge, paused=True)
            await orch.reply_status()
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert "paused" in body["text"].lower()


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
            orch = DispatcherBridge(bridge=bridge)
            await orch.task_started(issue_number=42, title="Fix widget", worker=3)

            orch.update_worker(3, phase="ci-watch")
            workers = orch.get_workers()
            assert workers[0].phase == "ci-watch"

    def test_update_nonexistent_worker_is_noop(self) -> None:
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            orch.update_worker(99, phase="done")  # Should not raise
            assert orch.get_workers() == []


# ── File-based inbox (read_inbox) ──────────────────────────────────────


class TestReadInbox:
    def test_returns_empty_when_no_inbox_path(self) -> None:
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            assert orch.read_inbox() == []

    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox = str(tmp_path / "nonexistent.json")
            orch = DispatcherBridge(bridge=bridge, inbox_path=inbox)
            assert orch.read_inbox() == []

    def test_returns_empty_when_file_empty(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text("")
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
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
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
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
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))

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
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))

            commands = orch.read_inbox()
            assert commands == []
            # File should be cleared.
            assert inbox_file.read_text().strip() == ""

    def test_pause_command_from_inbox(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(json.dumps([{"text": "pause"}]))
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))

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
            orch = DispatcherBridge(bridge=bridge, state_file=state_file)
            await orch.task_started(issue_number=42, title="Fix the widget", worker=3)

            # The file should exist and contain the worker.
            data = json.loads(Path(state_file).read_text())
            assert "3" in data["workers"]
            assert data["workers"]["3"]["issue_number"] == 42
            assert data["workers"]["3"]["issue_title"] == "Fix the widget"

    @respx.mock
    async def test_new_instance_loads_persisted_workers(self, tmp_path: Path) -> None:
        """A fresh DispatcherBridge instance loads workers from the state file."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")

            # First instance: start a task.
            bridge1 = _make_bridge()
            orch1 = DispatcherBridge(bridge=bridge1, state_file=state_file)
            await orch1.task_started(issue_number=42, title="Fix the widget", worker=3)

            # Second instance: should pick up the worker from disk.
            bridge2 = _make_bridge()
            orch2 = DispatcherBridge(bridge=bridge2, state_file=state_file)
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
            orch = DispatcherBridge(bridge=bridge, state_file=state_file)
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
            orch = DispatcherBridge(bridge=bridge, state_file=state_file)
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
            orch = DispatcherBridge(bridge=bridge, state_file=state_file)
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
            orch = DispatcherBridge(bridge=bridge, state_file=state_file)
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
            orch = DispatcherBridge(bridge=bridge, state_file=state_file, paused=True)
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
            orch1 = DispatcherBridge(bridge=bridge1, state_file=state_file)
            await orch1.handle_command(Command(kind=CommandKind.PAUSE))

            bridge2 = _make_bridge()
            orch2 = DispatcherBridge(bridge=bridge2, state_file=state_file)
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
            orch = DispatcherBridge(bridge=bridge, state_file=state_file)
            await orch.task_started(issue_number=42, title="Fix widget", worker=3)
            assert Path(state_file).exists()

            await orch.session_ended()
            assert not Path(state_file).exists()

    def test_missing_state_file_starts_fresh(self, tmp_path: Path) -> None:
        """If the state file doesn't exist, instance starts with empty state."""
        with mock_aws():
            state_file = str(tmp_path / "nonexistent.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, state_file=state_file)
            assert orch.get_workers() == []
            assert orch.paused is False

    def test_corrupt_state_file_starts_fresh(self, tmp_path: Path) -> None:
        """If the state file is corrupt, instance starts with empty state."""
        with mock_aws():
            state_file = tmp_path / "corrupt.json"
            state_file.write_text("not valid json{{{")

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, state_file=str(state_file))
            assert orch.get_workers() == []
            assert orch.paused is False

    def test_no_state_file_means_in_memory_only(self) -> None:
        """Without state_file, behavior is unchanged (in-memory only)."""
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
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
            orch1 = DispatcherBridge(bridge=bridge1, state_file=state_file)
            await orch1.task_started(issue_number=42, title="Fix widget", worker=3)

            # Second process: reply_status should include the worker.
            route.reset()
            route.mock(return_value=httpx.Response(200, json={"ok": True}))

            bridge2 = _make_bridge()
            orch2 = DispatcherBridge(bridge=bridge2, state_file=state_file)
            await orch2.reply_status()
            await bridge2.close()

            body = json.loads(route.calls[0].request.content)
            assert "Worker-3" in body["text"] or "Worker\\-3" in body["text"]
            assert "#42" in body["text"] or "42" in body["text"]


# ── refresh_state() ───────────────────────────────────────────────────


class TestRefreshState:
    @respx.mock
    async def test_refresh_picks_up_external_pause(self, tmp_path: Path) -> None:
        """refresh_state() re-reads the state file to pick up external changes."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, state_file=state_file)
            assert orch.paused is False

            # Simulate the responder daemon writing paused=True externally.
            Path(state_file).write_text(json.dumps({"paused": True, "workers": {}}))

            orch.refresh_state()
            assert orch.paused is True

    @respx.mock
    async def test_refresh_picks_up_external_workers(self, tmp_path: Path) -> None:
        """refresh_state() loads workers added by another process."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, state_file=state_file)
            assert orch.get_workers() == []

            # Simulate another process adding a worker.
            Path(state_file).write_text(
                json.dumps(
                    {
                        "paused": False,
                        "workers": {
                            "5": {
                                "worker_number": 5,
                                "issue_number": 99,
                                "issue_title": "New task",
                                "phase": "implementing",
                                "updated": "",
                            }
                        },
                    }
                )
            )

            orch.refresh_state()
            workers = orch.get_workers()
            assert len(workers) == 1
            assert workers[0].issue_number == 99

    def test_refresh_noop_without_state_file(self) -> None:
        """refresh_state() is a no-op when no state_file is set."""
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            # Should not raise.
            orch.refresh_state()
            assert orch.paused is False


# ── read_stop_requests() ──────────────────────────────────────────────


class TestReadStopRequests:
    def test_returns_empty_when_no_path(self) -> None:
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            assert orch.read_stop_requests() == []

    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            stop_file = str(tmp_path / "nonexistent.json")
            orch = DispatcherBridge(bridge=bridge, stop_requests_path=stop_file)
            assert orch.read_stop_requests() == []

    def test_returns_empty_when_file_empty(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            stop_file = tmp_path / "stop.json"
            stop_file.write_text("")
            orch = DispatcherBridge(bridge=bridge, stop_requests_path=str(stop_file))
            assert orch.read_stop_requests() == []

    def test_reads_and_clears_stop_requests(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            stop_file = tmp_path / "stop.json"
            stop_file.write_text(
                json.dumps(
                    [
                        {"issue_number": 42, "timestamp": "2026-03-10T20:00:00Z"},
                        {"issue_number": 99, "timestamp": "2026-03-10T20:01:00Z"},
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, stop_requests_path=str(stop_file))
            result = orch.read_stop_requests()

            assert result == [42, 99]
            # File should be cleared.
            assert stop_file.read_text().strip() == ""
            # Second read returns empty.
            assert orch.read_stop_requests() == []

    def test_accumulates_in_stopped_issues(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            stop_file = tmp_path / "stop.json"
            stop_file.write_text(json.dumps([{"issue_number": 42}]))
            orch = DispatcherBridge(bridge=bridge, stop_requests_path=str(stop_file))

            orch.read_stop_requests()
            assert orch.is_issue_stopped(42)
            assert not orch.is_issue_stopped(99)

            # Write more stop requests.
            stop_file.write_text(json.dumps([{"issue_number": 99}]))
            orch.read_stop_requests()
            assert orch.is_issue_stopped(42)  # Still remembered.
            assert orch.is_issue_stopped(99)

    def test_stopped_issues_property(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            stop_file = tmp_path / "stop.json"
            stop_file.write_text(json.dumps([{"issue_number": 10}, {"issue_number": 20}]))
            orch = DispatcherBridge(bridge=bridge, stop_requests_path=str(stop_file))
            orch.read_stop_requests()

            assert orch.stopped_issues == frozenset({10, 20})

    def test_clear_stopped_issues(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            stop_file = tmp_path / "stop.json"
            stop_file.write_text(json.dumps([{"issue_number": 42}]))
            orch = DispatcherBridge(bridge=bridge, stop_requests_path=str(stop_file))
            orch.read_stop_requests()
            assert orch.is_issue_stopped(42)

            orch.clear_stopped_issues()
            assert not orch.is_issue_stopped(42)
            assert orch.stopped_issues == frozenset()

    def test_handles_corrupt_file(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            stop_file = tmp_path / "stop.json"
            stop_file.write_text("not valid json{{{")
            orch = DispatcherBridge(bridge=bridge, stop_requests_path=str(stop_file))

            result = orch.read_stop_requests()
            assert result == []
            # File should be cleared.
            assert stop_file.read_text().strip() == ""

    def test_skips_invalid_entries(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            stop_file = tmp_path / "stop.json"
            stop_file.write_text(
                json.dumps(
                    [
                        {"issue_number": 42},
                        {"no_issue": "bad"},
                        "just a string",
                        {"issue_number": "not_an_int"},
                        {"issue_number": 99},
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, stop_requests_path=str(stop_file))
            result = orch.read_stop_requests()
            assert result == [42, 99]


# ── create_dispatcher_bridge with stop_requests_path ────────────────


class TestCreateDispatcherBridgeStopRequests:
    def test_factory_accepts_stop_requests_path(self) -> None:
        with mock_aws():
            orch = create_dispatcher_bridge(stop_requests_path="/tmp/test_stop.json")
            assert orch.stop_requests_path == "/tmp/test_stop.json"


# ── write_status() ───────────────────────────────────────────────────


class TestWriteStatus:
    def test_writes_status_json_to_file(self, tmp_path: Path) -> None:
        """write_status() creates dispatcher_status.json with expected keys."""
        with mock_aws():
            status_file = str(tmp_path / "dispatcher_status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            orch.write_status()

            data = json.loads(Path(status_file).read_text())
            assert data["active_agents"] == []
            assert data["open_prs"] == []
            assert data["recently_completed"] == []
            assert data["queue"] == []
            assert data["paused"] is False
            assert data["stopped_issues"] == []
            assert data["prs_since_last_audit"] == 0
            assert "updated_at" in data

    @respx.mock
    async def test_task_started_writes_status_file(self, tmp_path: Path) -> None:
        """task_started() should write the status file with the new worker."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            status_file = str(tmp_path / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            await orch.task_started(issue_number=42, title="Fix widget", worker=3)

            data = json.loads(Path(status_file).read_text())
            assert len(data["active_agents"]) == 1
            assert data["active_agents"][0]["issue_number"] == 42
            assert data["active_agents"][0]["worker_number"] == 3
            assert "updated_at" in data

    @respx.mock
    async def test_task_completed_writes_status_file(self, tmp_path: Path) -> None:
        """task_completed() should update the status file (worker removed, added to completed)."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            status_file = str(tmp_path / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            await orch.task_started(issue_number=42, title="Fix widget", worker=3)
            await orch.task_completed(issue_number=42, summary="PR merged.", worker=3)

            data = json.loads(Path(status_file).read_text())
            assert data["active_agents"] == []
            assert len(data["recently_completed"]) == 1
            assert data["recently_completed"][0]["issue_number"] == 42
            assert data["recently_completed"][0]["outcome"] == "completed"

    @respx.mock
    async def test_task_failed_writes_status_file(self, tmp_path: Path) -> None:
        """task_failed() should update the status file (worker removed, failure recorded)."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            status_file = str(tmp_path / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            await orch.task_started(issue_number=42, title="Fix widget", worker=3)
            await orch.task_failed(issue_number=42, error="CI broke.", worker=3)

            data = json.loads(Path(status_file).read_text())
            assert data["active_agents"] == []
            assert len(data["recently_completed"]) == 1
            assert data["recently_completed"][0]["issue_number"] == 42
            assert data["recently_completed"][0]["outcome"] == "failed"

    @respx.mock
    async def test_pause_writes_status_file(self, tmp_path: Path) -> None:
        """handle_command(PAUSE) should update the status file with paused=True."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            status_file = str(tmp_path / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            await orch.handle_command(Command(kind=CommandKind.PAUSE))

            data = json.loads(Path(status_file).read_text())
            assert data["paused"] is True

    @respx.mock
    async def test_resume_writes_status_file(self, tmp_path: Path) -> None:
        """handle_command(RESUME) should update the status file with paused=False."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            status_file = str(tmp_path / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file, paused=True)
            await orch.handle_command(Command(kind=CommandKind.RESUME))

            data = json.loads(Path(status_file).read_text())
            assert data["paused"] is False

    def test_write_status_includes_open_prs_and_queue(self, tmp_path: Path) -> None:
        """write_status() passes open_prs and queue through to the output."""
        with mock_aws():
            status_file = str(tmp_path / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            orch.write_status(
                open_prs=[{"number": 100, "ci_status": "green", "mergeable": True}],
                queue=[{"number": 650, "title": "Next task"}],
            )

            data = json.loads(Path(status_file).read_text())
            assert len(data["open_prs"]) == 1
            assert data["open_prs"][0]["number"] == 100
            assert len(data["queue"]) == 1
            assert data["queue"][0]["number"] == 650

    def test_write_status_noop_without_status_file(self) -> None:
        """write_status() is a no-op when status_file is not set."""
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            # Should not raise.
            orch.write_status()

    def test_write_status_creates_parent_directories(self, tmp_path: Path) -> None:
        """write_status() creates parent directories if they don't exist."""
        with mock_aws():
            status_file = str(tmp_path / "nested" / "dir" / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            orch.write_status()

            assert Path(status_file).exists()

    def test_write_status_includes_stopped_issues(self, tmp_path: Path) -> None:
        """write_status() reflects stopped issues in the output."""
        with mock_aws():
            status_file = str(tmp_path / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            orch._stopped_issues.add(99)
            orch._stopped_issues.add(101)
            orch.write_status()

            data = json.loads(Path(status_file).read_text())
            assert sorted(data["stopped_issues"]) == [99, 101]

    def test_write_status_includes_prs_since_last_audit(self, tmp_path: Path) -> None:
        """write_status() includes the prs_since_last_audit counter."""
        with mock_aws():
            status_file = str(tmp_path / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            orch.prs_since_last_audit = 15
            orch.write_status()

            data = json.loads(Path(status_file).read_text())
            assert data["prs_since_last_audit"] == 15

    def test_write_status_prs_since_last_audit_defaults_to_zero(self, tmp_path: Path) -> None:
        """write_status() defaults prs_since_last_audit to 0."""
        with mock_aws():
            status_file = str(tmp_path / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            orch.write_status()

            data = json.loads(Path(status_file).read_text())
            assert data["prs_since_last_audit"] == 0

    def test_prs_since_last_audit_persisted_across_sessions(self, tmp_path: Path) -> None:
        """prs_since_last_audit is saved to and loaded from the state file."""
        with mock_aws():
            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()

            # Save state with counter at 12
            orch1 = DispatcherBridge(bridge=bridge, state_file=state_file)
            orch1.prs_since_last_audit = 12
            orch1._save_state()

            # Load into a new bridge
            orch2 = DispatcherBridge(bridge=bridge, state_file=state_file)
            assert orch2.prs_since_last_audit == 12

    def test_session_number_persisted_across_sessions(self, tmp_path: Path) -> None:
        """session_number is saved to and loaded from the state file."""
        with mock_aws():
            state_file = str(tmp_path / "state.json")
            bridge = _make_bridge()

            # Save state with session_number at 3
            orch1 = DispatcherBridge(bridge=bridge, state_file=state_file)
            orch1.session_number = 3
            orch1._save_state()

            # Load into a new bridge — should pick up session_number
            orch2 = DispatcherBridge(bridge=bridge, state_file=state_file)
            assert orch2.session_number == 3

    def test_session_number_defaults_to_zero(self, tmp_path: Path) -> None:
        """session_number defaults to 0 when missing from state file."""
        with mock_aws():
            state_file = tmp_path / "state.json"
            state_file.write_text(json.dumps({"paused": False, "workers": {}}))

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, state_file=str(state_file))
            assert orch.session_number == 0

    def test_write_status_includes_session_number(self, tmp_path: Path) -> None:
        """write_status() includes the session_number field."""
        with mock_aws():
            status_file = str(tmp_path / "status.json")
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            orch.session_number = 5
            orch.write_status()

            data = json.loads(Path(status_file).read_text())
            assert data["session_number"] == 5


# ── _parse_inbox_entry() ──────────────────────────────────────────────────


class TestParseInboxEntry:
    """Tests for structured inbox entries with action metadata."""

    def test_file_issue_entry(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {
                            "action": "file_issue",
                            "description": "OC scraper timing out",
                            "priority": "p2",
                            "labels": ["area/scraping"],
                            "reply_to": 12345,
                            "timestamp": "2026-03-10T20:00:00+00:00",
                        }
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
            commands = orch.read_inbox()

            assert len(commands) == 1
            cmd = commands[0]
            assert cmd.kind == CommandKind.FILE_ISSUE
            assert cmd.description == "OC scraper timing out"
            assert cmd.priority == "p2"
            assert cmd.labels == ("area/scraping",)
            assert cmd.reply_to == 12345

    def test_discuss_entry(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {
                            "action": "discuss",
                            "message": "Should we use Redis for caching?",
                            "reply_to": 12345,
                            "timestamp": "2026-03-10T20:00:00+00:00",
                        }
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
            commands = orch.read_inbox()

            assert len(commands) == 1
            cmd = commands[0]
            assert cmd.kind == CommandKind.DISCUSS
            assert cmd.message == "Should we use Redis for caching?"
            assert cmd.reply_to == 12345

    def test_do_entry(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {
                            "action": "do",
                            "instruction": "Check CI on PR #738 and merge it",
                            "reply_to": 12345,
                            "timestamp": "2026-03-10T20:00:00+00:00",
                        }
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
            commands = orch.read_inbox()

            assert len(commands) == 1
            cmd = commands[0]
            assert cmd.kind == CommandKind.DO
            assert cmd.instruction == "Check CI on PR #738 and merge it"
            assert cmd.reply_to == 12345

    def test_start_entry_with_action_key(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {
                            "action": "start",
                            "issue": 42,
                            "reply_to": 12345,
                        }
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
            commands = orch.read_inbox()

            assert len(commands) == 1
            cmd = commands[0]
            assert cmd.kind == CommandKind.START
            assert cmd.issue_number == 42

    def test_mixed_text_and_structured_entries(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {"text": "start #100", "user_id": 123},
                        {
                            "action": "file_issue",
                            "description": "Bug in parser",
                            "priority": "p1",
                            "labels": [],
                            "reply_to": 123,
                        },
                        {"text": "pause", "user_id": 123},
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
            commands = orch.read_inbox()

            assert len(commands) == 3
            assert commands[0].kind == CommandKind.START
            assert commands[0].issue_number == 100
            assert commands[1].kind == CommandKind.FILE_ISSUE
            assert commands[1].description == "Bug in parser"
            assert commands[2].kind == CommandKind.PAUSE

    def test_unknown_action_falls_back_to_text(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {
                            "action": "unknown_type",
                            "text": "status",
                        }
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
            commands = orch.read_inbox()

            assert len(commands) == 1
            assert commands[0].kind == CommandKind.STATUS

    def test_file_issue_invalid_priority_normalized(self, tmp_path: Path) -> None:
        """Invalid priority values in inbox entries should be normalized to p2."""
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {
                            "action": "file_issue",
                            "description": "Critical bug",
                            "priority": "critical",
                            "labels": [],
                            "reply_to": 123,
                        }
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
            commands = orch.read_inbox()

            assert len(commands) == 1
            assert commands[0].kind == CommandKind.FILE_ISSUE
            assert commands[0].priority == "p2"

    def test_file_issue_p0_priority_normalized(self, tmp_path: Path) -> None:
        """p0 is human-only and should be normalized to p2 in inbox entries."""
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {
                            "action": "file_issue",
                            "description": "Urgent bug",
                            "priority": "p0",
                            "labels": [],
                        }
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
            commands = orch.read_inbox()

            assert len(commands) == 1
            assert commands[0].priority == "p2"

    def test_file_issue_valid_priorities_pass_through(self, tmp_path: Path) -> None:
        """Valid priority values (p1, p2, p3) should pass through unchanged."""
        for priority in ("p1", "p2", "p3"):
            with mock_aws():
                bridge = _make_bridge()
                inbox_file = tmp_path / "inbox.json"
                inbox_file.write_text(
                    json.dumps(
                        [
                            {
                                "action": "file_issue",
                                "description": "Bug",
                                "priority": priority,
                                "labels": [],
                            }
                        ]
                    )
                )
                orch = DispatcherBridge(bridge=bridge, inbox_path=str(inbox_file))
                commands = orch.read_inbox()

                assert len(commands) == 1
                assert commands[0].priority == priority


# ── handle_command() — new command types ────────────────────────────────


class TestHandleCommandNewTypes:
    @respx.mock
    async def test_handle_file_issue(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(
                    kind=CommandKind.FILE_ISSUE,
                    description="OC scraper timing out",
                    priority="p2",
                    labels=("area/scraping",),
                    reply_to=12345,
                )
            )
            await bridge.close()

            assert result["action"] == "file_issue"
            assert result["description"] == "OC scraper timing out"
            assert result["priority"] == "p2"
            assert result["labels"] == ["area/scraping"]
            assert result["reply_to"] == 12345

    @respx.mock
    async def test_handle_discuss_no_duplicate_notification(self) -> None:
        """Discuss commands should NOT send a Telegram notification (responder already did)."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(
                    kind=CommandKind.DISCUSS,
                    message="Should we use Redis?",
                    reply_to=12345,
                )
            )
            await bridge.close()

            assert result["action"] == "discuss"
            assert result["message"] == "Should we use Redis?"
            assert result["needs_reply"] is True
            assert "command_id" in result
            # No Telegram message should have been sent by handle_command.
            assert route.call_count == 0

    @respx.mock
    async def test_handle_do_no_duplicate_notification(self) -> None:
        """Do commands should NOT send a Telegram notification (responder already did)."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(
                    kind=CommandKind.DO,
                    instruction="Merge PR #750",
                    reply_to=12345,
                )
            )
            await bridge.close()

            assert result["action"] == "do"
            assert result["instruction"] == "Merge PR #750"
            assert result["needs_reply"] is True
            assert "command_id" in result
            # No Telegram message should have been sent by handle_command.
            assert route.call_count == 0

    @respx.mock
    async def test_handle_free_text_no_duplicate_notification(self) -> None:
        """Free-text commands should NOT send a Telegram notification (responder already did)."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(kind=CommandKind.FREE_TEXT, raw_text="how are scrapers doing?")
            )
            await bridge.close()

            assert result["needs_reply"] is True
            assert "command_id" in result
            # No Telegram message should have been sent by handle_command.
            assert route.call_count == 0


# ── DispatcherInstruction parsing ────────────────────────────────────


class TestParseInstruction:
    def test_restart_responder(self) -> None:
        entry = {
            "action": "restart_responder",
            "reason": "telegram-bridge code updated",
            "from_issue": 733,
            "timestamp": "2026-03-11T09:00:00Z",
        }
        inst = _parse_instruction(entry)
        assert inst is not None
        assert inst.kind == InstructionKind.RESTART_RESPONDER
        assert inst.reason == "telegram-bridge code updated"
        assert inst.from_issue == 733
        assert inst.timestamp == "2026-03-11T09:00:00Z"

    def test_terraform_apply(self) -> None:
        entry = {
            "action": "terraform_apply",
            "module": "telegram-bot",
            "from_issue": 712,
        }
        inst = _parse_instruction(entry)
        assert inst is not None
        assert inst.kind == InstructionKind.TERRAFORM_APPLY
        assert inst.module == "telegram-bot"
        assert inst.from_issue == 712

    def test_notify(self) -> None:
        entry = {
            "action": "notify",
            "message": "Found a regression in OC scraper",
            "from_issue": 600,
        }
        inst = _parse_instruction(entry)
        assert inst is not None
        assert inst.kind == InstructionKind.NOTIFY
        assert inst.message == "Found a regression in OC scraper"

    def test_run_script(self) -> None:
        entry = {
            "action": "run_script",
            "script": "scripts/tg-set-webhook.sh",
            "args": ["--force"],
            "from_issue": 725,
        }
        inst = _parse_instruction(entry)
        assert inst is not None
        assert inst.kind == InstructionKind.RUN_SCRIPT
        assert inst.script == "scripts/tg-set-webhook.sh"
        assert inst.args == ("--force",)

    def test_file_issue(self) -> None:
        entry = {
            "action": "file_issue",
            "title": "OC scraper timing out",
            "description": "The Orange County scraper has been timing out.",
            "priority": "p1",
            "labels": ["area/scraping", "priority/p1"],
            "from_issue": 500,
        }
        inst = _parse_instruction(entry)
        assert inst is not None
        assert inst.kind == InstructionKind.FILE_ISSUE
        assert inst.title == "OC scraper timing out"
        assert inst.description == "The Orange County scraper has been timing out."
        assert inst.priority == "p1"
        assert inst.labels == ("area/scraping", "priority/p1")

    def test_unknown_action_returns_none(self) -> None:
        entry = {"action": "unknown_action", "from_issue": 1}
        inst = _parse_instruction(entry)
        assert inst is None

    def test_missing_action_returns_none(self) -> None:
        entry = {"from_issue": 1}
        inst = _parse_instruction(entry)
        assert inst is None

    def test_non_int_from_issue_becomes_none(self) -> None:
        entry = {"action": "notify", "message": "test", "from_issue": "not_int"}
        inst = _parse_instruction(entry)
        assert inst is not None
        assert inst.from_issue is None

    def test_missing_optional_fields_default(self) -> None:
        entry = {"action": "restart_responder"}
        inst = _parse_instruction(entry)
        assert inst is not None
        assert inst.reason == ""
        assert inst.from_issue is None
        assert inst.timestamp == ""


# ── read_dispatcher_inbox() ──────────────────────────────────────────


class TestReadDispatcherInbox:
    def test_returns_empty_when_no_path(self) -> None:
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            assert orch.read_dispatcher_inbox() == []

    def test_returns_empty_when_file_missing(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox = str(tmp_path / "nonexistent.json")
            orch = DispatcherBridge(bridge=bridge, dispatcher_inbox_path=inbox)
            assert orch.read_dispatcher_inbox() == []

    def test_returns_empty_when_file_empty(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text("")
            orch = DispatcherBridge(bridge=bridge, dispatcher_inbox_path=str(inbox_file))
            assert orch.read_dispatcher_inbox() == []

    def test_reads_and_parses_instructions(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {
                            "action": "restart_responder",
                            "reason": "code updated",
                            "from_issue": 733,
                        },
                        {
                            "action": "notify",
                            "message": "Regression found",
                            "from_issue": 600,
                        },
                        {
                            "action": "terraform_apply",
                            "module": "telegram-bot",
                        },
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, dispatcher_inbox_path=str(inbox_file))
            instructions = orch.read_dispatcher_inbox()

            assert len(instructions) == 3
            assert instructions[0].kind == InstructionKind.RESTART_RESPONDER
            assert instructions[0].reason == "code updated"
            assert instructions[0].from_issue == 733
            assert instructions[1].kind == InstructionKind.NOTIFY
            assert instructions[1].message == "Regression found"
            assert instructions[2].kind == InstructionKind.TERRAFORM_APPLY
            assert instructions[2].module == "telegram-bot"

    def test_clears_file_after_reading(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(json.dumps([{"action": "notify", "message": "test"}]))
            orch = DispatcherBridge(bridge=bridge, dispatcher_inbox_path=str(inbox_file))

            instructions = orch.read_dispatcher_inbox()
            assert len(instructions) == 1

            # File should be empty now.
            assert inbox_file.read_text().strip() == ""

            # Second read returns empty.
            assert orch.read_dispatcher_inbox() == []

    def test_handles_corrupt_file(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text("not valid json{{{")
            orch = DispatcherBridge(bridge=bridge, dispatcher_inbox_path=str(inbox_file))

            instructions = orch.read_dispatcher_inbox()
            assert instructions == []
            # File should be cleared.
            assert inbox_file.read_text().strip() == ""

    def test_skips_unknown_actions(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {"action": "notify", "message": "valid"},
                        {"action": "unknown_thing"},
                        {"action": "restart_responder"},
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, dispatcher_inbox_path=str(inbox_file))
            instructions = orch.read_dispatcher_inbox()
            assert len(instructions) == 2
            assert instructions[0].kind == InstructionKind.NOTIFY
            assert instructions[1].kind == InstructionKind.RESTART_RESPONDER

    def test_skips_non_dict_entries(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(
                json.dumps(
                    [
                        {"action": "notify", "message": "valid"},
                        "just a string",
                        42,
                        {"action": "restart_responder"},
                    ]
                )
            )
            orch = DispatcherBridge(bridge=bridge, dispatcher_inbox_path=str(inbox_file))
            instructions = orch.read_dispatcher_inbox()
            assert len(instructions) == 2

    def test_handles_non_array_json(self, tmp_path: Path) -> None:
        with mock_aws():
            bridge = _make_bridge()
            inbox_file = tmp_path / "inbox.json"
            inbox_file.write_text(json.dumps({"action": "notify"}))
            orch = DispatcherBridge(bridge=bridge, dispatcher_inbox_path=str(inbox_file))
            instructions = orch.read_dispatcher_inbox()
            assert instructions == []


# ── create_dispatcher_bridge with dispatcher_inbox_path ────────────


class TestCreateDispatcherBridgeDispatcherInbox:
    def test_factory_accepts_dispatcher_inbox_path(self) -> None:
        with mock_aws():
            orch = create_dispatcher_bridge(dispatcher_inbox_path="/tmp/test_orch_inbox.json")
            assert orch.dispatcher_inbox_path == "/tmp/test_orch_inbox.json"


# ── Pending reply tracking ──────────────────────────────────────────────


class TestPendingReplyTracking:
    """Tests for the pending reply tracking system (discuss/do timeout fallback)."""

    @respx.mock
    async def test_discuss_creates_pending_reply(self) -> None:
        """handle_command(DISCUSS) should create a pending reply entry."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(
                    kind=CommandKind.DISCUSS,
                    message="Should we use Redis?",
                    reply_to=12345,
                )
            )
            await bridge.close()

            pending = orch.get_pending_replies()
            assert len(pending) == 1
            assert pending[0].kind == "discuss"
            assert pending[0].raw_text == "Should we use Redis?"
            assert pending[0].reply_to == 12345
            assert pending[0].command_id == result["command_id"]

    @respx.mock
    async def test_do_creates_pending_reply(self) -> None:
        """handle_command(DO) should create a pending reply entry."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(
                    kind=CommandKind.DO,
                    instruction="Merge PR #750",
                    reply_to=12345,
                )
            )
            await bridge.close()

            pending = orch.get_pending_replies()
            assert len(pending) == 1
            assert pending[0].kind == "do"
            assert pending[0].raw_text == "Merge PR #750"
            assert pending[0].command_id == result["command_id"]

    @respx.mock
    async def test_resolve_pending_reply_removes_entry(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(kind=CommandKind.DISCUSS, message="test", reply_to=12345)
            )
            command_id = result["command_id"]

            assert len(orch.get_pending_replies()) == 1
            resolved = orch.resolve_pending_reply(command_id)
            assert resolved is not None
            assert resolved.command_id == command_id
            assert len(orch.get_pending_replies()) == 0

    def test_resolve_unknown_command_id_returns_none(self) -> None:
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            assert orch.resolve_pending_reply("nonexistent") is None

    @respx.mock
    async def test_reply_with_command_id_resolves_pending(self) -> None:
        """Calling reply() with command_id should auto-resolve the pending entry."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            result = await orch.handle_command(
                Command(kind=CommandKind.DISCUSS, message="test", reply_to=12345)
            )
            command_id = result["command_id"]

            assert len(orch.get_pending_replies()) == 1
            await orch.reply("Here is my answer.", command_id=command_id)
            assert len(orch.get_pending_replies()) == 0
            await bridge.close()

    @respx.mock
    async def test_check_reply_timeouts_sends_fallback(self) -> None:
        """Pending replies that exceed the timeout should get a fallback message."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, reply_timeout_seconds=60.0)

            # Manually track a pending reply with a timestamp in the past.
            orch._pending_replies["test-cmd"] = PendingReply(
                command_id="test-cmd",
                kind="discuss",
                raw_text="Old question",
                reply_to=12345,
                created_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=120),
            )

            timed_out = await orch.check_reply_timeouts()
            await bridge.close()

            assert len(timed_out) == 1
            assert timed_out[0].command_id == "test-cmd"
            assert timed_out[0].fallback_sent is True
            # A fallback message should have been sent.
            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert "still working" in body["text"].lower()

    @respx.mock
    async def test_check_reply_timeouts_skips_recent(self) -> None:
        """Pending replies within the timeout should not trigger a fallback."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, reply_timeout_seconds=60.0)

            # Manually track a pending reply with a recent timestamp.
            orch._pending_replies["fresh-cmd"] = PendingReply(
                command_id="fresh-cmd",
                kind="discuss",
                raw_text="Recent question",
                created_at=datetime.datetime.now(datetime.UTC),
            )

            timed_out = await orch.check_reply_timeouts()
            await bridge.close()

            assert len(timed_out) == 0
            assert route.call_count == 0

    @respx.mock
    async def test_check_reply_timeouts_only_sends_fallback_once(self) -> None:
        """Fallback should only be sent once per pending reply."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, reply_timeout_seconds=60.0)

            orch._pending_replies["test-cmd"] = PendingReply(
                command_id="test-cmd",
                kind="do",
                raw_text="Old instruction",
                created_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=120),
            )

            # First check sends fallback.
            timed_out_1 = await orch.check_reply_timeouts()
            assert len(timed_out_1) == 1

            # Second check should not send another fallback.
            timed_out_2 = await orch.check_reply_timeouts()
            assert len(timed_out_2) == 0
            await bridge.close()

            # Only one message total.
            assert route.call_count == 1

    @respx.mock
    async def test_status_commands_do_not_create_pending_reply(self) -> None:
        """Non-discuss/do commands should not create pending reply entries."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.handle_command(Command(kind=CommandKind.STATUS))
            await orch.handle_command(Command(kind=CommandKind.PAUSE))
            await bridge.close()

            assert len(orch.get_pending_replies()) == 0


# ── reply_to_message_id threading ───────────────────────────────────────


class TestReplyToMessageId:
    """Tests for reply_to_message_id support in outbound messages."""

    @respx.mock
    async def test_reply_with_reply_to_message_id(self) -> None:
        """reply() should pass reply_to_message_id to the Telegram API."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.reply("Answering your question.", reply_to_message_id=42)
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert body["reply_to_message_id"] == 42

    @respx.mock
    async def test_reply_without_reply_to_message_id(self) -> None:
        """reply() without reply_to_message_id should not include it in the payload."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.reply("Just a message.")
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert "reply_to_message_id" not in body

    @respx.mock
    async def test_notify_with_reply_to_message_id(self) -> None:
        """TelegramBridge.notify() should support reply_to_message_id."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            await bridge.notify("Threaded reply.", reply_to_message_id=99)
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert body["reply_to_message_id"] == 99

    @respx.mock
    async def test_timeout_fallback_includes_reply_to(self) -> None:
        """Timeout fallback should include reply_to_message_id if set on the pending reply."""
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, reply_timeout_seconds=60.0)

            orch._pending_replies["test-cmd"] = PendingReply(
                command_id="test-cmd",
                kind="discuss",
                raw_text="Question",
                reply_to=42,
                created_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=120),
            )

            await orch.check_reply_timeouts()
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert body.get("reply_to_message_id") == 42


# ── _worker_label() ──────────────────────────────────────────────────────


class TestWorkerLabel:
    """Tests for the _worker_label helper."""

    def test_int_worker_number(self) -> None:
        assert _worker_label(3) == "Worker-3"

    def test_string_agent_id_truncated(self) -> None:
        assert _worker_label("agent-ab4722a2") == "Agent-ab47"

    def test_short_agent_id_not_truncated(self) -> None:
        # agent IDs with 10 chars or fewer are not truncated
        assert _worker_label("agent-abcd") == "agent-abcd"

    def test_non_agent_string_passthrough(self) -> None:
        assert _worker_label("custom-worker") == "custom-worker"


# ── Agent ID support (int | str worker) ──────────────────────────────────


class TestAgentIdWorkerSupport:
    """Tests that string agent IDs work for task_started/completed/failed."""

    @respx.mock
    async def test_task_started_with_agent_id(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.task_started(
                issue_number=42, title="Fix the widget", worker="agent-ab4722a2"
            )

            workers = orch.get_workers()
            assert len(workers) == 1
            assert workers[0].worker_number == "agent-ab4722a2"
            assert workers[0].issue_number == 42

            body = json.loads(route.calls[0].request.content)
            assert "Agent\\-ab47" in body["text"] or "Agent-ab47" in body["text"]

    @respx.mock
    async def test_task_completed_with_agent_id(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.task_started(
                issue_number=42, title="Fix the widget", worker="agent-ab4722a2"
            )
            await orch.task_completed(
                issue_number=42, summary="PR merged.", worker="agent-ab4722a2"
            )

            assert orch.get_workers() == []

    @respx.mock
    async def test_task_failed_with_agent_id(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.task_started(
                issue_number=42, title="Fix the widget", worker="agent-ab4722a2"
            )
            await orch.task_failed(issue_number=42, error="CI broke.", worker="agent-ab4722a2")

            assert orch.get_workers() == []

    @respx.mock
    async def test_update_worker_with_agent_id(self) -> None:
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.task_started(issue_number=42, title="Fix widget", worker="agent-ab4722a2")

            orch.update_worker("agent-ab4722a2", phase="ci-watch")
            workers = orch.get_workers()
            assert workers[0].phase == "ci-watch"

    @respx.mock
    async def test_reply_status_with_agent_id(self) -> None:
        with mock_aws():
            _setup_secret()
            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.task_started(issue_number=42, title="Fix widget", worker="agent-ab4722a2")

            route.reset()
            route.mock(return_value=httpx.Response(200, json={"ok": True}))

            await orch.reply_status()
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert "Agent\\-ab47" in body["text"] or "Agent-ab47" in body["text"]

    @respx.mock
    async def test_state_persistence_with_agent_id(self, tmp_path: Path) -> None:
        """Agent ID worker state round-trips through save/load."""
        state_file = str(tmp_path / "state.json")
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge1 = _make_bridge()
            orch1 = DispatcherBridge(bridge=bridge1, state_file=state_file)
            await orch1.task_started(
                issue_number=42, title="Fix the widget", worker="agent-ab4722a2"
            )

            # Load from the file in a new instance.
            bridge2 = _make_bridge()
            orch2 = DispatcherBridge(bridge=bridge2, state_file=state_file)
            workers = orch2.get_workers()
            assert len(workers) == 1
            assert workers[0].worker_number == "agent-ab4722a2"
            assert workers[0].issue_number == 42

    @respx.mock
    async def test_status_file_includes_agent_label(self, tmp_path: Path) -> None:
        """write_status() should include agent_label in active_agents."""
        status_file = str(tmp_path / "status.json")
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, status_file=status_file)
            await orch.task_started(issue_number=42, title="Fix widget", worker="agent-ab4722a2")

            data = json.loads(Path(status_file).read_text())
            assert len(data["active_agents"]) == 1
            agent = data["active_agents"][0]
            assert agent["worker_number"] == "agent-ab4722a2"
            assert agent["agent_label"] == "Agent-ab47"

    @respx.mock
    async def test_mixed_int_and_string_workers(self) -> None:
        """Dispatcher should handle a mix of integer and agent-ID workers."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            await orch.task_started(issue_number=1, title="Int worker", worker=3)
            await orch.task_started(issue_number=2, title="Agent worker", worker="agent-deadbeef")

            workers = orch.get_workers()
            assert len(workers) == 2

            # Complete the int worker, agent worker should remain.
            await orch.task_completed(issue_number=1, summary="Done.", worker=3)
            workers = orch.get_workers()
            assert len(workers) == 1
            assert workers[0].worker_number == "agent-deadbeef"


# ── prune_stale_state() ────────────────────────────────────────────────


class TestPruneStaleState:
    """Tests for DispatcherBridge.prune_stale_state()."""

    def test_clears_stale_workers_from_state_file(self, tmp_path: Path) -> None:
        """prune_stale_state() removes all workers from the state file."""
        with mock_aws():
            state_file = tmp_path / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "paused": False,
                        "prs_since_last_audit": 5,
                        "session_number": 3,
                        "workers": {
                            "3": {
                                "worker_number": 3,
                                "issue_number": 42,
                                "issue_title": "Stale task A",
                                "phase": "ci-watch",
                                "updated": "2026-03-13T10:00:00",
                            },
                            "agent-abc12345": {
                                "worker_number": "agent-abc12345",
                                "issue_number": 99,
                                "issue_title": "Stale task B",
                                "phase": "implementing",
                                "updated": "2026-03-13T11:00:00",
                            },
                        },
                    }
                )
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, state_file=str(state_file))
            # Before pruning: 2 stale workers loaded.
            assert len(orch.get_workers()) == 2

            orch.prune_stale_state()

            # After pruning: no workers in memory.
            assert orch.get_workers() == []

            # State file on disk also has empty workers.
            data = json.loads(state_file.read_text())
            assert data["workers"] == {}

    def test_preserves_counters_across_prune(self, tmp_path: Path) -> None:
        """prune_stale_state() preserves prs_since_last_audit and session_number."""
        with mock_aws():
            state_file = tmp_path / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "paused": True,
                        "prs_since_last_audit": 17,
                        "session_number": 5,
                        "workers": {
                            "1": {
                                "worker_number": 1,
                                "issue_number": 10,
                                "issue_title": "Old",
                                "phase": "done",
                                "updated": "",
                            },
                        },
                    }
                )
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge, state_file=str(state_file))
            orch.prune_stale_state()

            data = json.loads(state_file.read_text())
            assert data["prs_since_last_audit"] == 17
            assert data["session_number"] == 5
            assert data["paused"] is True
            assert data["workers"] == {}

    def test_clears_stopped_issues(self, tmp_path: Path) -> None:
        """prune_stale_state() clears the in-memory stopped issues set."""
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            # Manually add stopped issues.
            orch._stopped_issues.add(42)
            orch._stopped_issues.add(99)
            assert len(orch.stopped_issues) == 2

            orch.prune_stale_state()

            assert len(orch.stopped_issues) == 0

    def test_truncates_stop_requests_file(self, tmp_path: Path) -> None:
        """prune_stale_state() truncates stop_requests.json to an empty array."""
        with mock_aws():
            stop_file = tmp_path / "stop_requests.json"
            stop_file.write_text(
                json.dumps(
                    [
                        {"issue_number": 954, "stopped_at": "2026-03-13T10:00:00"},
                        {"issue_number": 973, "stopped_at": "2026-03-13T11:00:00"},
                    ]
                )
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(
                bridge=bridge,
                stop_requests_path=str(stop_file),
            )
            orch.prune_stale_state()

            content = json.loads(stop_file.read_text())
            assert content == []

    def test_writes_clean_status_file(self, tmp_path: Path) -> None:
        """prune_stale_state() writes a status file with empty active_agents."""
        with mock_aws():
            state_file = tmp_path / "state.json"
            status_file = tmp_path / "status.json"
            state_file.write_text(
                json.dumps(
                    {
                        "paused": False,
                        "prs_since_last_audit": 0,
                        "session_number": 1,
                        "workers": {
                            "3": {
                                "worker_number": 3,
                                "issue_number": 42,
                                "issue_title": "Stale",
                                "phase": "done",
                                "updated": "",
                            },
                        },
                    }
                )
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(
                bridge=bridge,
                state_file=str(state_file),
                status_file=str(status_file),
            )
            orch.prune_stale_state()

            data = json.loads(status_file.read_text())
            assert data["active_agents"] == []

    def test_preserves_recently_completed_from_status_file(self, tmp_path: Path) -> None:
        """prune_stale_state() preserves recently_completed from the existing status file."""
        with mock_aws():
            state_file = tmp_path / "state.json"
            status_file = tmp_path / "status.json"
            state_file.write_text(
                json.dumps(
                    {
                        "paused": False,
                        "prs_since_last_audit": 5,
                        "session_number": 2,
                        "workers": {
                            "1": {
                                "worker_number": 1,
                                "issue_number": 10,
                                "issue_title": "Old",
                                "phase": "done",
                                "updated": "",
                            },
                        },
                    }
                )
            )
            old_completed = [
                {
                    "issue_number": 42,
                    "outcome": "completed",
                    "summary": "Fixed the widget",
                    "timestamp": "2026-03-22T10:00:00",
                },
                {
                    "issue_number": 99,
                    "outcome": "failed",
                    "error": "CI broke",
                    "timestamp": "2026-03-22T11:00:00",
                },
            ]
            status_file.write_text(
                json.dumps(
                    {
                        "active_agents": [
                            {
                                "worker_number": 1,
                                "issue_number": 10,
                                "issue_title": "Old",
                                "phase": "done",
                            }
                        ],
                        "recently_completed": old_completed,
                        "open_prs": [],
                        "queue": [],
                        "paused": False,
                        "stopped_issues": [],
                    }
                )
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(
                bridge=bridge,
                state_file=str(state_file),
                status_file=str(status_file),
            )
            orch.prune_stale_state()

            data = json.loads(status_file.read_text())
            assert data["active_agents"] == []
            assert data["recently_completed"] == old_completed

    @respx.mock
    async def test_task_started_after_prune_has_only_new_worker(self, tmp_path: Path) -> None:
        """After pruning, task_started() writes only the new worker to status."""
        with mock_aws():
            _setup_secret()
            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            state_file = tmp_path / "state.json"
            status_file = tmp_path / "status.json"
            state_file.write_text(
                json.dumps(
                    {
                        "paused": False,
                        "prs_since_last_audit": 0,
                        "session_number": 1,
                        "workers": {
                            "1": {
                                "worker_number": 1,
                                "issue_number": 100,
                                "issue_title": "Old stale task",
                                "phase": "verifying",
                                "updated": "",
                            },
                            "2": {
                                "worker_number": 2,
                                "issue_number": 200,
                                "issue_title": "Another stale task",
                                "phase": "ci-watch",
                                "updated": "",
                            },
                        },
                    }
                )
            )

            bridge = _make_bridge()
            orch = DispatcherBridge(
                bridge=bridge,
                state_file=str(state_file),
                status_file=str(status_file),
            )
            orch.prune_stale_state()
            await orch.task_started(issue_number=300, title="New task", worker="agent-new12345")

            # Status file should contain exactly 1 active agent.
            data = json.loads(status_file.read_text())
            assert len(data["active_agents"]) == 1
            assert data["active_agents"][0]["issue_number"] == 300
            assert data["active_agents"][0]["worker_number"] == "agent-new12345"

            # State file should also contain exactly 1 worker.
            state_data = json.loads(state_file.read_text())
            assert len(state_data["workers"]) == 1

    def test_noop_without_files_configured(self) -> None:
        """prune_stale_state() is a no-op when no file paths are set."""
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(bridge=bridge)
            # Should not raise.
            orch.prune_stale_state()
            assert orch.get_workers() == []

    def test_noop_when_stop_requests_file_missing(self, tmp_path: Path) -> None:
        """prune_stale_state() handles missing stop_requests file gracefully."""
        with mock_aws():
            bridge = _make_bridge()
            orch = DispatcherBridge(
                bridge=bridge,
                stop_requests_path=str(tmp_path / "nonexistent.json"),
            )
            # Should not raise.
            orch.prune_stale_state()

    def test_corrupt_status_file_does_not_break_prune(self, tmp_path: Path) -> None:
        """prune_stale_state() handles a corrupt status file gracefully."""
        with mock_aws():
            state_file = tmp_path / "state.json"
            status_file = tmp_path / "status.json"
            state_file.write_text(json.dumps({"paused": False, "workers": {}}))
            status_file.write_text("not valid json{{{")

            bridge = _make_bridge()
            orch = DispatcherBridge(
                bridge=bridge,
                state_file=str(state_file),
                status_file=str(status_file),
            )
            # Should not raise — falls back to empty recently_completed.
            orch.prune_stale_state()

            data = json.loads(status_file.read_text())
            assert data["active_agents"] == []
            assert data["recently_completed"] == []

    def test_stop_requests_truncate_error_is_logged(self, tmp_path: Path) -> None:
        """prune_stale_state() logs a warning if stop_requests truncation fails."""
        with mock_aws():
            # Point to a directory instead of a file to trigger an OS error.
            stop_dir = tmp_path / "stop_requests.json"
            stop_dir.mkdir()

            bridge = _make_bridge()
            orch = DispatcherBridge(
                bridge=bridge,
                stop_requests_path=str(stop_dir),
            )
            # Should not raise — the error is logged.
            orch.prune_stale_state()

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling prune_stale_state() multiple times is safe."""
        with mock_aws():
            state_file = tmp_path / "state.json"
            stop_file = tmp_path / "stop_requests.json"
            state_file.write_text(
                json.dumps(
                    {
                        "paused": False,
                        "prs_since_last_audit": 3,
                        "session_number": 2,
                        "workers": {
                            "1": {
                                "worker_number": 1,
                                "issue_number": 10,
                                "issue_title": "Task",
                                "phase": "done",
                                "updated": "",
                            },
                        },
                    }
                )
            )
            stop_file.write_text(json.dumps([{"issue_number": 42}]))

            bridge = _make_bridge()
            orch = DispatcherBridge(
                bridge=bridge,
                state_file=str(state_file),
                stop_requests_path=str(stop_file),
            )
            orch.prune_stale_state()
            orch.prune_stale_state()

            assert orch.get_workers() == []
            assert len(orch.stopped_issues) == 0
            data = json.loads(state_file.read_text())
            assert data["workers"] == {}
            assert data["prs_since_last_audit"] == 3
            assert json.loads(stop_file.read_text()) == []
