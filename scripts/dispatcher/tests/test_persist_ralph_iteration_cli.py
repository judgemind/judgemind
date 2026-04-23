"""Unit tests for ``scripts/dispatcher/persist_ralph_iteration.py`` (#3026).

The CLI is the bridge that lets the ralph skill call back into
postgres at end-of-iteration without requiring the daemon to own the
per-iteration hook directly. All failure paths exit 0 — ralph must
never get a non-zero exit from this helper.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import persist_ralph_iteration  # noqa: E402


def _args(
    worktree: Path,
    *,
    agent_id: str = "agent-1",
    issue_number: int = 42,
    iteration: int = 1,
    verdict: str = "LOOP",
) -> list[str]:
    return [
        "--worktree",
        str(worktree),
        "--agent-id",
        agent_id,
        "--issue-number",
        str(issue_number),
        "--iteration",
        str(iteration),
        "--verdict",
        verdict,
    ]


def test_no_database_url_exits_0_and_emits_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Absent ``DATABASE_URL`` env → exit 0, stdout ``skipped=no_database_url``.
    Ralph must keep going even when the daemon / env is misconfigured."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    rc = persist_ralph_iteration.main(_args(worktree))

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "skipped=no_database_url"


def test_missing_worktree_exits_0_with_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Worktree path doesn't exist → skipped, exit 0."""
    monkeypatch.setenv("DATABASE_URL", "postgres://fake")
    missing = tmp_path / "missing"

    rc = persist_ralph_iteration.main(_args(missing))

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.startswith("skipped=worktree_missing:")


def test_empty_patch_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``git format-patch`` returns no bytes → skipped, exit 0, no DB write."""
    monkeypatch.setenv("DATABASE_URL", "postgres://fake")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    empty = subprocess.CompletedProcess(
        args=["git", "format-patch"], returncode=0, stdout="", stderr=""
    )
    with patch("subprocess.run", side_effect=[empty]):
        rc = persist_ralph_iteration.main(_args(worktree))

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "skipped=empty_or_failed_format_patch"


def test_happy_path_inserts_and_emits_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: format-patch + rev-parse + psycopg.connect.execute
    all succeed; stdout says ``persisted=<patch_id>``."""
    monkeypatch.setenv("DATABASE_URL", "postgres://fake")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    format_patch_ok = subprocess.CompletedProcess(
        args=["git", "format-patch"],
        returncode=0,
        stdout="From abc Mon...\npatch body\n",
        stderr="",
    )
    rev_parse_ok = subprocess.CompletedProcess(
        args=["git", "rev-parse"], returncode=0, stdout="deadbeef\n", stderr=""
    )

    # Fake psycopg: connect → context mgr → cursor() → context mgr →
    # execute() + fetchone() returning the new patch_id tuple.
    fake_cursor = MagicMock()
    fake_cursor.__enter__ = MagicMock(return_value=fake_cursor)
    fake_cursor.__exit__ = MagicMock(return_value=None)
    fake_cursor.fetchone = MagicMock(return_value=("generated-uuid",))
    fake_cursor.execute = MagicMock()

    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=None)
    fake_conn.cursor = MagicMock(return_value=fake_cursor)
    fake_conn.commit = MagicMock()

    fake_psycopg = MagicMock()
    fake_psycopg.connect = MagicMock(return_value=fake_conn)

    with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        with patch(
            "subprocess.run",
            side_effect=[format_patch_ok, rev_parse_ok],
        ):
            rc = persist_ralph_iteration.main(
                _args(worktree, iteration=3, verdict="LOOP", agent_id="ag-xyz")
            )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "persisted=generated-uuid"

    # INSERT params carry iteration + verdict columns.
    fake_cursor.execute.assert_called_once()
    call_args = fake_cursor.execute.call_args
    sql = call_args.args[0]
    params = call_args.args[1]
    assert "INSERT INTO dispatcher.ralph_patches" in sql
    assert "iteration_n" in sql
    assert "verdict" in sql
    # (agent_id, issue_number, patch_content, commit_sha, iteration_n, verdict).
    assert params[0] == "ag-xyz"
    assert params[1] == 42
    assert params[3] == "deadbeef"
    assert params[4] == 3
    assert params[5] == "LOOP"
    fake_conn.commit.assert_called_once()


def test_db_error_emits_error_and_exits_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A DB exception during INSERT → stdout ``error=...``, exit 0.
    Never propagates to ralph."""
    monkeypatch.setenv("DATABASE_URL", "postgres://fake")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    format_patch_ok = subprocess.CompletedProcess(
        args=["git", "format-patch"],
        returncode=0,
        stdout="patch bytes\n",
        stderr="",
    )
    rev_parse_ok = subprocess.CompletedProcess(
        args=["git", "rev-parse"], returncode=0, stdout="sha\n", stderr=""
    )

    fake_psycopg = MagicMock()
    fake_psycopg.connect = MagicMock(side_effect=RuntimeError("connection refused"))

    with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
        with patch(
            "subprocess.run",
            side_effect=[format_patch_ok, rev_parse_ok],
        ):
            rc = persist_ralph_iteration.main(_args(worktree))

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.startswith("error=")
    assert "connection refused" in captured.out
