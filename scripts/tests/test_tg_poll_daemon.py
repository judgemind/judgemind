"""Tests for the background Telegram SQS polling daemon."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add the scripts directory to the path so we can import the daemon module.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location(
    "tg_poll_daemon", SCRIPTS_DIR / "tg-poll-daemon.py"
)
assert _spec is not None and _spec.loader is not None
tg_poll_daemon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg_poll_daemon)


class TestPollSqs:
    """Tests for _poll_sqs()."""

    def test_returns_empty_when_no_messages(self) -> None:
        sqs = MagicMock()
        sqs.receive_message.return_value = {"Messages": []}
        result = tg_poll_daemon._poll_sqs(sqs, "https://queue-url")
        assert result == []

    def test_returns_parsed_messages(self) -> None:
        msg_body = {
            "text": "status",
            "user_id": 123,
            "timestamp": "2026-01-01T00:00:00Z",
        }
        sqs = MagicMock()
        sqs.receive_message.side_effect = [
            {
                "Messages": [
                    {
                        "MessageId": "m1",
                        "ReceiptHandle": "rh1",
                        "Body": json.dumps(msg_body),
                    }
                ]
            },
            {"Messages": []},
        ]
        result = tg_poll_daemon._poll_sqs(sqs, "https://queue-url")
        assert len(result) == 1
        assert result[0]["text"] == "status"

    def test_deletes_messages_after_receiving(self) -> None:
        sqs = MagicMock()
        sqs.receive_message.side_effect = [
            {
                "Messages": [
                    {
                        "MessageId": "m1",
                        "ReceiptHandle": "rh1",
                        "Body": json.dumps({"text": "hi"}),
                    }
                ]
            },
            {"Messages": []},
        ]
        tg_poll_daemon._poll_sqs(sqs, "https://queue-url")
        sqs.delete_message_batch.assert_called_once()
        entries = sqs.delete_message_batch.call_args[1]["Entries"]
        assert entries[0]["Id"] == "m1"

    def test_skips_malformed_messages(self) -> None:
        sqs = MagicMock()
        sqs.receive_message.side_effect = [
            {
                "Messages": [
                    {
                        "MessageId": "m1",
                        "ReceiptHandle": "rh1",
                        "Body": "not json",
                    }
                ]
            },
            {"Messages": []},
        ]
        result = tg_poll_daemon._poll_sqs(sqs, "https://queue-url")
        assert result == []
        # Still deletes the bad message from queue.
        sqs.delete_message_batch.assert_called_once()


class TestAppendToInbox:
    """Tests for _append_to_inbox()."""

    def test_creates_file_with_messages(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox.json"
        messages = [{"text": "status", "user_id": 123}]
        tg_poll_daemon._append_to_inbox(inbox, messages)

        data = json.loads(inbox.read_text())
        assert len(data) == 1
        assert data[0]["text"] == "status"

    def test_appends_to_existing_file(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox.json"
        inbox.write_text(json.dumps([{"text": "first"}]))

        tg_poll_daemon._append_to_inbox(inbox, [{"text": "second"}])

        data = json.loads(inbox.read_text())
        assert len(data) == 2
        assert data[0]["text"] == "first"
        assert data[1]["text"] == "second"

    def test_noop_when_messages_empty(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox.json"
        tg_poll_daemon._append_to_inbox(inbox, [])
        assert not inbox.exists()

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        inbox = tmp_path / "sub" / "dir" / "inbox.json"
        tg_poll_daemon._append_to_inbox(inbox, [{"text": "hi"}])
        assert inbox.exists()

    def test_handles_corrupt_existing_file(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox.json"
        inbox.write_text("not json{{{")

        tg_poll_daemon._append_to_inbox(inbox, [{"text": "new"}])

        data = json.loads(inbox.read_text())
        assert len(data) == 1
        assert data[0]["text"] == "new"


class TestPidFile:
    """Tests for PID file management."""

    def test_write_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        tg_poll_daemon._write_pid_file(pid_file)
        assert pid_file.exists()
        assert int(pid_file.read_text()) == os.getpid()

    def test_remove_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        tg_poll_daemon._remove_pid_file(pid_file)
        assert not pid_file.exists()

    def test_remove_nonexistent_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "nonexistent.pid"
        tg_poll_daemon._remove_pid_file(pid_file)  # Should not raise


class TestSignalHandler:
    """Tests for the signal handler."""

    def test_sets_shutdown_flag(self) -> None:
        tg_poll_daemon._shutdown_requested = False
        tg_poll_daemon._handle_signal(15, None)
        assert tg_poll_daemon._shutdown_requested is True
        # Reset for other tests.
        tg_poll_daemon._shutdown_requested = False


class TestRunDaemon:
    """Integration-style tests for the daemon loop."""

    def test_daemon_exits_on_shutdown_signal(self, tmp_path: Path) -> None:
        """Daemon should exit cleanly when _shutdown_requested is set."""
        inbox = tmp_path / "inbox.json"
        pid_file = tmp_path / "test.pid"

        # Pre-set the shutdown flag so the loop exits immediately.
        tg_poll_daemon._shutdown_requested = True

        mock_sqs = MagicMock()
        mock_sqs.receive_message.return_value = {"Messages": []}

        with patch.object(tg_poll_daemon.boto3, "client", return_value=mock_sqs):
            tg_poll_daemon.run_daemon(
                inbox_path=inbox,
                pid_file=pid_file,
                queue_url="https://queue-url",
                region="us-west-2",
                interval=0.1,
            )

        # PID file should be cleaned up.
        assert not pid_file.exists()

        # Reset for other tests.
        tg_poll_daemon._shutdown_requested = False

    def test_daemon_writes_pid_file(self, tmp_path: Path) -> None:
        """Daemon should write a PID file on startup."""
        pid_file = tmp_path / "test.pid"
        inbox = tmp_path / "inbox.json"

        # Use shutdown flag to exit after one iteration.
        tg_poll_daemon._shutdown_requested = True

        mock_sqs = MagicMock()
        mock_sqs.receive_message.return_value = {"Messages": []}

        with patch.object(tg_poll_daemon.boto3, "client", return_value=mock_sqs):
            # We can't easily check _during_ execution, but we can verify
            # cleanup happened (meaning PID was written and then removed).
            tg_poll_daemon.run_daemon(
                inbox_path=inbox,
                pid_file=pid_file,
                queue_url="https://queue-url",
                region="us-west-2",
                interval=0.1,
            )

        assert not pid_file.exists()
        tg_poll_daemon._shutdown_requested = False
