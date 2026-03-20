"""Tests for the scripts/dispatcher-request.py CLI script."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _script_path() -> str:
    """Return the absolute path to dispatcher-request.py."""
    return str(Path(__file__).resolve().parents[3] / "scripts" / "dispatcher-request.py")


def _run_request(
    *args: str,
    inbox_file: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run dispatcher-request.py with the given arguments."""
    cmd = [sys.executable, _script_path()]
    cmd.extend(args)
    if inbox_file:
        cmd.extend(["--inbox-file", inbox_file])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


class TestDispatcherRequest:
    def test_restart_responder(self, tmp_path: Path) -> None:
        inbox = str(tmp_path / "inbox.json")
        result = _run_request(
            "restart_responder",
            "--reason",
            "code updated",
            "--from-issue",
            "733",
            inbox_file=inbox,
        )
        assert result.returncode == 0

        data = json.loads(Path(inbox).read_text())
        assert len(data) == 1
        assert data[0]["action"] == "restart_responder"
        assert data[0]["reason"] == "code updated"
        assert data[0]["from_issue"] == 733
        assert "timestamp" in data[0]

    def test_notify(self, tmp_path: Path) -> None:
        inbox = str(tmp_path / "inbox.json")
        result = _run_request(
            "notify",
            "--message",
            "Found a regression",
            inbox_file=inbox,
        )
        assert result.returncode == 0

        data = json.loads(Path(inbox).read_text())
        assert len(data) == 1
        assert data[0]["action"] == "notify"
        assert data[0]["message"] == "Found a regression"

    def test_terraform_apply(self, tmp_path: Path) -> None:
        inbox = str(tmp_path / "inbox.json")
        result = _run_request(
            "terraform_apply",
            "--module",
            "telegram-bot",
            "--from-issue",
            "712",
            inbox_file=inbox,
        )
        assert result.returncode == 0

        data = json.loads(Path(inbox).read_text())
        assert len(data) == 1
        assert data[0]["action"] == "terraform_apply"
        assert data[0]["module"] == "telegram-bot"
        assert data[0]["from_issue"] == 712

    def test_run_script(self, tmp_path: Path) -> None:
        inbox = str(tmp_path / "inbox.json")
        result = _run_request(
            "run_script",
            "--script",
            "scripts/tg-set-webhook.sh",
            "--args",
            "arg1",
            "arg2",
            inbox_file=inbox,
        )
        assert result.returncode == 0

        data = json.loads(Path(inbox).read_text())
        assert len(data) == 1
        assert data[0]["action"] == "run_script"
        assert data[0]["script"] == "scripts/tg-set-webhook.sh"
        assert data[0]["args"] == ["arg1", "arg2"]

    def test_file_issue(self, tmp_path: Path) -> None:
        inbox = str(tmp_path / "inbox.json")
        result = _run_request(
            "file_issue",
            "--title",
            "Bug report",
            "--description",
            "Something broke",
            "--priority",
            "p1",
            "--labels",
            "area/scraping",
            "type/bug",
            "--from-issue",
            "500",
            inbox_file=inbox,
        )
        assert result.returncode == 0

        data = json.loads(Path(inbox).read_text())
        assert len(data) == 1
        assert data[0]["action"] == "file_issue"
        assert data[0]["title"] == "Bug report"
        assert data[0]["description"] == "Something broke"
        assert data[0]["priority"] == "p1"
        assert data[0]["labels"] == ["area/scraping", "type/bug"]
        assert data[0]["from_issue"] == 500

    def test_appends_to_existing_file(self, tmp_path: Path) -> None:
        inbox = str(tmp_path / "inbox.json")
        # Write an initial entry.
        Path(inbox).write_text(json.dumps([{"action": "notify", "message": "first"}]))

        result = _run_request(
            "notify",
            "--message",
            "second",
            inbox_file=inbox,
        )
        assert result.returncode == 0

        data = json.loads(Path(inbox).read_text())
        assert len(data) == 2
        assert data[0]["message"] == "first"
        assert data[1]["message"] == "second"

    def test_creates_inbox_file_if_missing(self, tmp_path: Path) -> None:
        inbox = str(tmp_path / "new_dir" / "inbox.json")
        result = _run_request(
            "notify",
            "--message",
            "hello",
            inbox_file=inbox,
        )
        assert result.returncode == 0
        assert Path(inbox).exists()

        data = json.loads(Path(inbox).read_text())
        assert len(data) == 1

    def test_invalid_action_rejected(self, tmp_path: Path) -> None:
        inbox = str(tmp_path / "inbox.json")
        result = _run_request(
            "bad_action",
            inbox_file=inbox,
        )
        assert result.returncode != 0
        assert not Path(inbox).exists()

    def test_default_priority_not_written(self, tmp_path: Path) -> None:
        """Default priority p2 is not written to the entry to keep it lean."""
        inbox = str(tmp_path / "inbox.json")
        result = _run_request(
            "file_issue",
            "--title",
            "Test",
            "--description",
            "Desc",
            inbox_file=inbox,
        )
        assert result.returncode == 0

        data = json.loads(Path(inbox).read_text())
        assert "priority" not in data[0]

    def test_explicit_non_default_priority_written(self, tmp_path: Path) -> None:
        inbox = str(tmp_path / "inbox.json")
        result = _run_request(
            "file_issue",
            "--title",
            "Test",
            "--priority",
            "p1",
            inbox_file=inbox,
        )
        assert result.returncode == 0

        data = json.loads(Path(inbox).read_text())
        assert data[0]["priority"] == "p1"
