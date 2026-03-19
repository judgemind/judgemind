"""Tests for the scripts/orchestrator-send.py CLI script."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _script_path() -> str:
    """Return the absolute path to orchestrator-send.py."""
    return str(Path(__file__).resolve().parents[3] / "scripts" / "orchestrator-send.py")


def _run_send(
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """Run orchestrator-send.py with the given arguments."""
    cmd = [sys.executable, _script_path()]
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


class TestOrchestratorSend:
    def test_basic_send(self, tmp_path: Path) -> None:
        """Test that a message is written to the inbox file."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        result = _run_send(
            "--worktree",
            str(worktree),
            "Rebase onto latest main",
        )
        assert result.returncode == 0

        inbox = worktree / "tmp" / "inbox.json"
        assert inbox.exists()

        data = json.loads(inbox.read_text())
        assert len(data) == 1
        assert data[0]["message"] == "Rebase onto latest main"
        assert "timestamp" in data[0]

    def test_appends_to_existing_inbox(self, tmp_path: Path) -> None:
        """Test that messages are appended to an existing inbox."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        tmp_dir = worktree / "tmp"
        tmp_dir.mkdir()

        # Write an initial message
        inbox = tmp_dir / "inbox.json"
        inbox.write_text(json.dumps([{"timestamp": "2026-01-01T00:00:00", "message": "first"}]))

        result = _run_send(
            "--worktree",
            str(worktree),
            "second message",
        )
        assert result.returncode == 0

        data = json.loads(inbox.read_text())
        assert len(data) == 2
        assert data[0]["message"] == "first"
        assert data[1]["message"] == "second message"

    def test_creates_tmp_directory_if_missing(self, tmp_path: Path) -> None:
        """Test that the script creates tmp/ if it doesn't exist."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        # Note: tmp/ not created

        result = _run_send(
            "--worktree",
            str(worktree),
            "hello",
        )
        assert result.returncode == 0
        assert (worktree / "tmp" / "inbox.json").exists()

    def test_missing_worktree_flag_rejected(self) -> None:
        """Test that omitting --worktree causes an error."""
        result = _run_send("some message")
        assert result.returncode != 0

    def test_missing_message_rejected(self, tmp_path: Path) -> None:
        """Test that omitting the message causes an error."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        result = _run_send("--worktree", str(worktree))
        assert result.returncode != 0

    def test_nonexistent_worktree_rejected(self, tmp_path: Path) -> None:
        """Test that a non-existent worktree path causes an error."""
        result = _run_send(
            "--worktree",
            str(tmp_path / "does-not-exist"),
            "hello",
        )
        assert result.returncode != 0

    def test_empty_message_allowed(self, tmp_path: Path) -> None:
        """Test that an empty string message is accepted."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        result = _run_send(
            "--worktree",
            str(worktree),
            "",
        )
        assert result.returncode == 0

        data = json.loads((worktree / "tmp" / "inbox.json").read_text())
        assert len(data) == 1
        assert data[0]["message"] == ""

    def test_handles_corrupted_inbox(self, tmp_path: Path) -> None:
        """Test that a corrupted inbox file is handled gracefully."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        tmp_dir = worktree / "tmp"
        tmp_dir.mkdir()

        # Write invalid JSON
        inbox = tmp_dir / "inbox.json"
        inbox.write_text("this is not json{{{")

        result = _run_send(
            "--worktree",
            str(worktree),
            "recovery message",
        )
        assert result.returncode == 0

        data = json.loads(inbox.read_text())
        assert len(data) == 1
        assert data[0]["message"] == "recovery message"

    def test_message_has_iso_timestamp(self, tmp_path: Path) -> None:
        """Test that the timestamp is in ISO-8601 format."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        result = _run_send(
            "--worktree",
            str(worktree),
            "test timestamp",
        )
        assert result.returncode == 0

        data = json.loads((worktree / "tmp" / "inbox.json").read_text())
        ts = data[0]["timestamp"]
        # ISO-8601 should contain 'T' and digits
        assert "T" in ts
        assert any(c.isdigit() for c in ts)

    def test_stdout_confirms_send(self, tmp_path: Path) -> None:
        """Test that stdout shows a confirmation message."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "tmp").mkdir()

        result = _run_send(
            "--worktree",
            str(worktree),
            "hello",
        )
        assert result.returncode == 0
        assert "Sent message to" in result.stdout
