"""Unit tests for ``scripts.dispatcher.task_claim`` — /task skill claim helper.

Issue #2866. Covers the four code paths the helper owns:

* ``claim`` via psycopg (DATABASE_URL set) — happy path + UniqueViolation.
* ``claim`` via dev-db-query.sh (DATABASE_URL unset) — happy path +
  UniqueViolation-by-string-match + non-unique-violation subprocess error.
* ``terminal`` via psycopg — happy path + UPDATE-matched-0-rows.
* ``terminal`` via dev-db-query.sh — happy path + rowcount parse.

All external calls — psycopg and the ``dev-db-query.sh`` subprocess —
are mocked. The helper is deliberately light on logic so these tests
cover most of its LOC via pair-assertions.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Make ``scripts`` importable without installing the repo as a package.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


# Provide a stub ``psycopg`` module before importing the helper. The
# helper imports psycopg lazily inside each function, but we install a
# stub whose ``errors.UniqueViolation`` is a real Exception so the
# ``except psycopg.errors.UniqueViolation`` clause works.
#
# Match the defensive install pattern from test_daemon_phase3c.py —
# only overwrite ``sys.modules["psycopg"]`` when it's missing or
# malformed. Another test in the same pytest session (e.g.
# test_daemon_phase3a.py) may have already installed a compatible stub;
# overwriting it would swap out the ``UniqueViolation`` class that
# other tests captured via ``import psycopg``, causing the daemon's
# ``except psycopg.errors.UniqueViolation`` clause to miss.
if "psycopg" not in sys.modules or not isinstance(
    getattr(sys.modules["psycopg"].errors, "UniqueViolation", None), type
):

    class _UniqueViolation(Exception):
        """Test sentinel — stands in for real psycopg.errors.UniqueViolation."""

    _psycopg_stub = MagicMock()
    _psycopg_errors = MagicMock()
    _psycopg_errors.UniqueViolation = _UniqueViolation
    _psycopg_stub.errors = _psycopg_errors
    sys.modules["psycopg"] = _psycopg_stub

import psycopg  # noqa: E402  — re-import after stub install

from dispatcher import task_claim  # noqa: E402  — sys.path mutation above


# --------------------------------------------------------------------------
# Shared fakes — mirror the _FakeCursor / _FakeConnection pattern from
# test_daemon_phase3a.py so behavior stays consistent across suites.
# --------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, fetch_result: Any = None, rowcount: int = 1) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._fetch_result = fetch_result
        self.rowcount = rowcount
        self.raise_on_execute: Exception | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        if self.raise_on_execute is not None:
            raise self.raise_on_execute

    def fetchone(self) -> Any:
        return self._fetch_result


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.closed = True


def _patch_psycopg_connect(
    monkeypatch: pytest.MonkeyPatch, cursor: _FakeCursor
) -> _FakeConnection:
    """Install a fake ``psycopg.connect`` that returns the provided cursor.

    Returns the underlying connection so the test can assert commits /
    rollbacks.
    """
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(psycopg, "connect", lambda *_args, **_kwargs: conn)
    return conn


# --------------------------------------------------------------------------
# SQL literal escaping — defensive against single-quote inputs
# --------------------------------------------------------------------------


class TestSqlLiteral:
    """``_sql_literal`` doubles single quotes and rejects NUL."""

    def test_simple_string_roundtrips(self) -> None:
        assert task_claim._sql_literal("hello") == "'hello'"

    def test_doubles_single_quotes(self) -> None:
        assert task_claim._sql_literal("it's") == "'it''s'"

    def test_rejects_null_byte(self) -> None:
        with pytest.raises(ValueError):
            task_claim._sql_literal("has\x00null")


# --------------------------------------------------------------------------
# _claim_via_psycopg — DATABASE_URL path
# --------------------------------------------------------------------------


class TestClaimViaPsycopg:
    """psycopg-path claim INSERT covers happy + UniqueViolation."""

    def test_happy_path_inserts_and_commits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cur = _FakeCursor()
        conn = _patch_psycopg_connect(monkeypatch, cur)

        task_claim._claim_via_psycopg(
            "postgres://fake",
            issue_number=2866,
            agent_id="aabbccdd-eeff-0011-2233-445566778899",
            worktree_path="/tmp/wt",
            issue_title="title",
        )

        assert len(cur.executed) == 1
        sql, params = cur.executed[0]
        assert "INSERT INTO dispatcher.agents" in sql
        assert params[0] == "aabbccdd-eeff-0011-2233-445566778899"
        assert params[1] == task_claim.TASK_SKILL_KIND
        assert params[2] == 2866
        assert params[3] == "title"
        assert params[4] == "/tmp/wt"
        assert conn.commits == 1

    def test_unique_violation_is_translated_to_claim_lost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cur = _FakeCursor()
        cur.raise_on_execute = psycopg.errors.UniqueViolation("dup")
        conn = _patch_psycopg_connect(monkeypatch, cur)

        with pytest.raises(task_claim.ClaimLost):
            task_claim._claim_via_psycopg(
                "postgres://fake",
                issue_number=2866,
                agent_id="abc",
                worktree_path="/tmp/wt",
                issue_title=None,
            )
        assert conn.rollbacks == 1


# --------------------------------------------------------------------------
# _lookup_owner_via_psycopg — owner-identification path
# --------------------------------------------------------------------------


class TestLookupOwnerViaPsycopg:
    def test_returns_owner_dict_when_active_row_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cur = _FakeCursor(
            fetch_result=(
                "0000-owner-uuid",
                "task-skill",
                "running",
                "2026-04-19T20:00:00Z",
            )
        )
        _patch_psycopg_connect(monkeypatch, cur)

        owner = task_claim._lookup_owner_via_psycopg("postgres://fake", 2866)
        assert owner is not None
        assert owner["owner_agent_id"] == "0000-owner-uuid"
        assert owner["owner_kind"] == "task-skill"
        assert owner["owner_status"] == "running"
        assert owner["issue_number"] == 2866

    def test_returns_none_when_no_active_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cur = _FakeCursor(fetch_result=None)
        _patch_psycopg_connect(monkeypatch, cur)

        owner = task_claim._lookup_owner_via_psycopg("postgres://fake", 2866)
        assert owner is None


# --------------------------------------------------------------------------
# _claim_via_db_query_sh — DATABASE_URL-unset path
# --------------------------------------------------------------------------


class TestClaimViaDbQuerySh:
    """Subprocess-path claim covers happy + UniqueViolation string match."""

    def test_happy_path_invokes_dev_db_query_sh_rw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = '{"rowcount": 1}'
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Point the helper's dev-db-query.sh resolver at a real path so
        # the _run_sql_via_db_query_sh "exists" check passes. The script
        # in this repo lives at scripts/dev-db-query.sh which the
        # worktree always has.
        task_claim._claim_via_db_query_sh(
            issue_number=2866,
            agent_id="abc",
            worktree_path="/tmp/wt",
            issue_title=None,
        )
        assert len(captured) == 1
        assert captured[0][0] == "bash"
        assert captured[0][1].endswith("scripts/dev-db-query.sh")
        assert "--rw" in captured[0]

    def test_unique_violation_error_message_raises_claim_lost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = (
                "Database error: duplicate key value violates unique "
                'constraint "idx_dispatcher_agents_active_issue"\n'
            )
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(task_claim.ClaimLost):
            task_claim._claim_via_db_query_sh(
                issue_number=2866,
                agent_id="abc",
                worktree_path="/tmp/wt",
                issue_title=None,
            )

    def test_non_unique_violation_error_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "Database error: connection refused\n"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError):
            task_claim._claim_via_db_query_sh(
                issue_number=2866,
                agent_id="abc",
                worktree_path="/tmp/wt",
                issue_title=None,
            )


# --------------------------------------------------------------------------
# _terminal_via_psycopg — psycopg terminal UPDATE
# --------------------------------------------------------------------------


class TestTerminalViaPsycopg:
    def test_happy_path_updates_and_commits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cur = _FakeCursor(rowcount=1)
        conn = _patch_psycopg_connect(monkeypatch, cur)

        rowcount = task_claim._terminal_via_psycopg(
            "postgres://fake",
            agent_id="abc",
            status="succeeded",
            pr_number=42,
        )
        assert rowcount == 1
        assert len(cur.executed) == 1
        sql, params = cur.executed[0]
        assert "UPDATE dispatcher.agents" in sql
        assert params[0] == "succeeded"
        assert params[1] == 42
        assert params[2] == "abc"
        assert conn.commits == 1

    def test_rowcount_zero_when_no_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cur = _FakeCursor(rowcount=0)
        _patch_psycopg_connect(monkeypatch, cur)

        rowcount = task_claim._terminal_via_psycopg(
            "postgres://fake",
            agent_id="missing",
            status="failed",
            pr_number=None,
        )
        assert rowcount == 0


# --------------------------------------------------------------------------
# do_claim — top-level orchestration + exit codes
# --------------------------------------------------------------------------


class TestDoClaim:
    """Exit code contract: 0 on success, 1 on error, 2 on claim_lost."""

    def test_happy_path_with_database_url_returns_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        called: list[dict[str, Any]] = []

        def fake_claim(*args: Any, **kwargs: Any) -> None:
            called.append({"args": args, "kwargs": kwargs})

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", fake_claim)
        rc = task_claim.do_claim(2866, "abc", "/tmp/wt", None)
        assert rc == 0
        assert len(called) == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip())
        assert payload == {"status": "claimed", "issue_number": 2866}

    def test_claim_lost_returns_two_and_emits_owner_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")

        def raise_claim_lost(*_args: Any, **_kwargs: Any) -> None:
            raise task_claim.ClaimLost("dup")

        def fake_lookup(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "owner_agent_id": "daemon-agent-uuid",
                "owner_kind": "task",
                "owner_status": "running",
                "owner_started_at": "2026-04-19T20:00:00Z",
                "issue_number": 2866,
            }

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", raise_claim_lost)
        monkeypatch.setattr(task_claim, "_lookup_owner_via_psycopg", fake_lookup)

        rc = task_claim.do_claim(2866, "abc", "/tmp/wt", None)
        assert rc == 2
        out = capsys.readouterr()
        payload = json.loads(out.out.strip())
        assert payload["owner_agent_id"] == "daemon-agent-uuid"
        assert payload["owner_kind"] == "task"
        assert payload["owner_status"] == "running"
        assert payload["issue_number"] == 2866
        assert payload["reason"] == "claim_lost"
        assert "daemon agent" in out.err or "task agent" in out.err

    def test_claim_lost_with_no_owner_still_returns_two(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Edge case: owner lookup returned None (row released between attempts).

        We still report claim_lost (exit 2) because the INSERT failed,
        but the payload is minimal — just the issue number + reason.
        """
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")

        def raise_claim_lost(*_args: Any, **_kwargs: Any) -> None:
            raise task_claim.ClaimLost("dup")

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", raise_claim_lost)
        monkeypatch.setattr(
            task_claim, "_lookup_owner_via_psycopg", lambda *_a, **_k: None
        )

        rc = task_claim.do_claim(2866, "abc", "/tmp/wt", None)
        assert rc == 2
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["issue_number"] == 2866
        assert payload["reason"] == "claim_lost"

    def test_unexpected_error_returns_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")

        def fake_claim(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", fake_claim)
        rc = task_claim.do_claim(2866, "abc", "/tmp/wt", None)
        assert rc == 1
        err = capsys.readouterr().err
        assert "boom" in err

    def test_no_database_url_uses_db_query_sh_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        called: list[tuple[Any, ...]] = []

        def fake_claim_sh(*args: Any, **kwargs: Any) -> None:
            called.append((args, kwargs))

        monkeypatch.setattr(task_claim, "_claim_via_db_query_sh", fake_claim_sh)
        rc = task_claim.do_claim(2866, "abc", "/tmp/wt", None)
        assert rc == 0
        assert len(called) == 1


# --------------------------------------------------------------------------
# do_terminal — top-level orchestration + rowcount handling
# --------------------------------------------------------------------------


class TestDoTerminal:
    def test_happy_path_returns_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        monkeypatch.setattr(
            task_claim,
            "_terminal_via_psycopg",
            lambda *_a, **_k: 1,
        )
        rc = task_claim.do_terminal("abc", "succeeded", 42)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["status"] == "terminal_recorded"
        assert payload["rowcount"] == 1

    def test_rowcount_zero_still_returns_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Terminal update on a missing row is best-effort — exit 0 with warning."""
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        monkeypatch.setattr(
            task_claim,
            "_terminal_via_psycopg",
            lambda *_a, **_k: 0,
        )
        rc = task_claim.do_terminal("missing", "failed", None)
        assert rc == 0
        err = capsys.readouterr().err
        assert "matched 0 rows" in err

    def test_db_error_returns_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")

        def fake_terminal(*_a: Any, **_k: Any) -> int:
            raise RuntimeError("db down")

        monkeypatch.setattr(task_claim, "_terminal_via_psycopg", fake_terminal)
        rc = task_claim.do_terminal("abc", "succeeded", None)
        assert rc == 1
        err = capsys.readouterr().err
        assert "db down" in err


# --------------------------------------------------------------------------
# main() CLI — argparse + dispatch
# --------------------------------------------------------------------------


class TestMain:
    def test_claim_subcommand_dispatches_to_do_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[int, str, str, str | None]] = []

        def fake_do_claim(
            issue: int, agent_id: str, worktree_path: str, issue_title: str | None
        ) -> int:
            captured.append((issue, agent_id, worktree_path, issue_title))
            return 0

        monkeypatch.setattr(task_claim, "do_claim", fake_do_claim)
        rc = task_claim.main(
            [
                "claim",
                "--issue",
                "2866",
                "--agent-id",
                "abc",
                "--worktree-path",
                "/tmp/wt",
            ]
        )
        assert rc == 0
        assert captured == [(2866, "abc", "/tmp/wt", None)]

    def test_terminal_subcommand_dispatches_to_do_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[str, str, int | None]] = []

        def fake_do_terminal(agent_id: str, status: str, pr_number: int | None) -> int:
            captured.append((agent_id, status, pr_number))
            return 0

        monkeypatch.setattr(task_claim, "do_terminal", fake_do_terminal)
        rc = task_claim.main(
            [
                "terminal",
                "--agent-id",
                "abc",
                "--status",
                "succeeded",
                "--pr-number",
                "42",
            ]
        )
        assert rc == 0
        assert captured == [("abc", "succeeded", 42)]

    def test_terminal_rejects_invalid_status(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            task_claim.main(
                [
                    "terminal",
                    "--agent-id",
                    "abc",
                    "--status",
                    "crashed",  # not in VALID_TERMINAL_STATUSES
                ]
            )
