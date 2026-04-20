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

# A valid UUID to use as the ``agent_id`` in tests that don't care
# about the specific value — matches the ``str(uuid.uuid4())`` shape
# ``dispatcher.agents.agent_id`` expects (issue #2892 — bare hex
# strings like ``"abc"`` or ``"agent-a56f2e57"`` fail the real DB
# column's ``InvalidTextRepresentation`` check).
_VALID_AGENT_ID = "aabbccdd-eeff-0011-2233-445566778899"


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
            issue_priority="p1",
        )

        assert len(cur.executed) == 1
        sql, params = cur.executed[0]
        assert "INSERT INTO dispatcher.agents" in sql
        assert "priority" in sql
        assert params[0] == "aabbccdd-eeff-0011-2233-445566778899"
        assert params[1] == task_claim.TASK_SKILL_KIND
        assert params[2] == 2866
        assert params[3] == "title"
        assert params[4] == "/tmp/wt"
        # New in #2899 — priority lands as the last positional param.
        assert params[5] == "p1"
        assert conn.commits == 1

    def test_happy_path_with_null_priority(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2899 — priority is nullable. Pre-migration-33 rows and
        issues with no priority/pN label both land here.
        """
        cur = _FakeCursor()
        _patch_psycopg_connect(monkeypatch, cur)

        task_claim._claim_via_psycopg(
            "postgres://fake",
            issue_number=2866,
            agent_id="aabbccdd-eeff-0011-2233-445566778899",
            worktree_path="/tmp/wt",
            issue_title="title",
            issue_priority=None,
        )

        sql, params = cur.executed[0]
        assert params[5] is None

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
                issue_priority=None,
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
            issue_priority="p1",
        )
        assert len(captured) == 1
        # #2899 — the INSERT SQL includes a ``priority`` column and the
        # literal value is rendered inline (wrapped in single quotes).
        sql_blob = captured[0][-1]
        assert "priority" in sql_blob
        assert "'p1'" in sql_blob
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
                issue_priority=None,
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
                issue_priority=None,
            )

    def test_null_priority_renders_sql_NULL_not_quoted_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issue #2899 — priority is nullable.

        When the /task skill doesn't know the priority (no priority/pN
        label on the issue, or a pre-migration-33 call path), the
        INSERT must render a bare ``NULL`` literal, not the string
        ``'NULL'`` (which would be accepted by PostgreSQL as a valid
        non-null TEXT value).
        """

        captured: list[str] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd[-1])
            r = MagicMock()
            r.returncode = 0
            r.stdout = '{"rowcount": 1}'
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        task_claim._claim_via_db_query_sh(
            issue_number=2866,
            agent_id="abc",
            worktree_path="/tmp/wt",
            issue_title=None,
            issue_priority=None,
        )
        sql = captured[0]
        # The priority clause renders as the bare SQL literal NULL.
        assert ", NULL)" in sql
        # It is NOT a quoted string ``'NULL'``.
        assert ", 'NULL')" not in sql

    def test_invalid_uuid_error_raises_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for issue #2892.

        The old classifier treated ``InvalidTextRepresentation`` (caller
        bug — non-UUID agent_id) as a generic RuntimeError, which the
        /task skill was supposed to treat as "stop" but in practice
        silently swallowed. Surface it as a distinct
        :class:`ClaimConfigurationError` so ``do_claim`` can emit exit
        code 3.
        """

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            # Exact error text reproduced from running the helper
            # against dev RDS with agent_id='agent-a5d7e546':
            r.stderr = (
                "Database error: invalid input syntax for type uuid: "
                '"agent-a5d7e546"\n'
                "LINE 1: ...ssue_title, worktree_path, phase, status) "
                "VALUES ('agent-a5d...\n"
            )
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(task_claim.ClaimConfigurationError):
            task_claim._claim_via_db_query_sh(
                issue_number=2866,
                agent_id="agent-a5d7e546",
                worktree_path="/tmp/wt",
                issue_title=None,
                issue_priority=None,
            )

    def test_json_parser_strips_ecs_exec_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for issue #2892.

        ``aws ecs execute-command`` wraps command output with a session
        preamble and a ``Cannot perform start session: EOF`` trailer.
        The old parser's fallback used ``stdout[stdout.find("{"):]``
        which leaves the trailer in the slice, so ``json.loads`` fails
        and the whole claim comes back as a generic RuntimeError.
        Use :meth:`json.JSONDecoder.raw_decode` to read exactly one
        JSON value and ignore the trailer.
        """

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = (
                "\n"
                "The Session Manager plugin was installed successfully. "
                "Use the AWS CLI to start a session.\n"
                "\n"
                "\n"
                "Starting session with SessionId: "
                "ecs-execute-command-abc123\n"
                '{"rowcount": 1}\n'
                "Cannot perform start session: EOF\n"
            )
            r.stderr = "Running query on dev database via ECS Exec...\n"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = task_claim._run_sql_via_db_query_sh("INSERT INTO x VALUES (1)")
        assert result == {"rowcount": 1}

    def test_json_parser_handles_select_list_payload_with_trailer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same as above but for SELECT-shape payloads (a JSON array).

        The owner-lookup code path reads a list of row dicts; it hits
        the same ECS Exec trailer and must parse just the array.
        """

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = (
                "Starting session with SessionId: ecs-abc\n"
                '[{"owner_agent_id": "daemon-uuid", "owner_kind": "task"}]\n'
                "Cannot perform start session: EOF\n"
            )
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = task_claim._run_sql_via_db_query_sh("SELECT 1")
        assert isinstance(result, list)
        assert result[0]["owner_agent_id"] == "daemon-uuid"


class TestExtractFirstJsonValue:
    """Direct tests for the raw-decode helper — fast path + trailing-noise."""

    def test_pure_json_object(self) -> None:
        assert task_claim._extract_first_json_value('{"rowcount": 1}') == {
            "rowcount": 1
        }

    def test_pure_json_array(self) -> None:
        assert task_claim._extract_first_json_value("[1, 2, 3]") == [1, 2, 3]

    def test_strips_ecs_exec_trailer(self) -> None:
        payload = (
            "Starting session with SessionId: ecs-xyz\n"
            '{"rowcount": 1}\n'
            "Cannot perform start session: EOF\n"
        )
        assert task_claim._extract_first_json_value(payload) == {"rowcount": 1}

    def test_raw_decode_prefers_outer_object_over_inner_string_literal(
        self,
    ) -> None:
        """Ensure the raw_decode scan prefers the outer wrapping object.

        If someone embeds a ``{`` inside a string literal earlier in the
        payload, we want the outer object — not the start of a string
        value. ``raw_decode`` is naturally greedy-forward so the first
        successful raw_decode at the first ``[``/``{`` wins, which is
        exactly what we want.
        """
        payload = '{"outer": {"inner": 1}}'
        assert task_claim._extract_first_json_value(payload) == {"outer": {"inner": 1}}

    def test_no_json_raises(self) -> None:
        with pytest.raises(RuntimeError, match="non-JSON stdout"):
            task_claim._extract_first_json_value("no json here at all")


class TestIsUuid:
    """Regression tests for the UUID validator added in #2892."""

    def test_accepts_canonical_uuid(self) -> None:
        assert task_claim._is_uuid("aabbccdd-eeff-0011-2233-445566778899")

    def test_rejects_short_hex(self) -> None:
        # This is the exact bug shape from #2892 — worktree basenames
        # are ``agent-<8 hex chars>`` which is NOT a UUID.
        assert not task_claim._is_uuid("agent-a56f2e57")

    def test_rejects_bare_hex(self) -> None:
        assert not task_claim._is_uuid("abc")

    def test_rejects_empty(self) -> None:
        assert not task_claim._is_uuid("")

    def test_accepts_uuid_without_hyphens(self) -> None:
        # ``uuid.UUID`` accepts no-hyphen form too; we don't care as
        # long as Postgres's UUID type would accept it.
        assert task_claim._is_uuid("aabbccddeeff00112233445566778899")


class TestAgentIdSidecar:
    """The claim→terminal handshake via ``<worktree>/.task-agent-id``."""

    def test_write_then_read_round_trips(self, tmp_path: Path) -> None:
        task_claim._write_agent_id_sidecar(str(tmp_path), _VALID_AGENT_ID)
        recovered = task_claim._read_agent_id_sidecar(str(tmp_path))
        assert recovered == _VALID_AGENT_ID

    def test_read_returns_none_when_missing(self, tmp_path: Path) -> None:
        # tmp_path exists but no sidecar written yet.
        assert task_claim._read_agent_id_sidecar(str(tmp_path)) is None

    def test_read_rejects_non_uuid_contents(self, tmp_path: Path) -> None:
        """A sidecar with non-UUID contents is treated as missing.

        Defends against ancient sidecars that predate the UUID fix
        (e.g. containing ``agent-a56f2e57``) — don't propagate the bad
        value into an INSERT that would fail again.
        """
        (tmp_path / task_claim.AGENT_ID_SIDECAR_NAME).write_text(
            "agent-a56f2e57\n", encoding="utf-8"
        )
        assert task_claim._read_agent_id_sidecar(str(tmp_path)) is None

    def test_write_creates_parent_dir(self, tmp_path: Path) -> None:
        # Nested non-existent path — write should create it.
        nested = tmp_path / "nested"
        task_claim._write_agent_id_sidecar(str(nested), _VALID_AGENT_ID)
        assert (nested / task_claim.AGENT_ID_SIDECAR_NAME).exists()


class TestDoClaimUuidGeneration:
    """``do_claim`` generates a UUID when ``agent_id`` is None — #2892."""

    def test_none_agent_id_generates_uuid_and_writes_sidecar(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Pass ``agent_id=None``; verify a UUID was generated, passed
        to the DB path, emitted in the JSON response, and persisted to
        the sidecar."""
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        captured_agent_ids: list[str] = []

        captured_priorities: list[str | None] = []

        def fake_claim(
            database_url: str,
            issue_number: int,
            agent_id: str,
            worktree_path: str,
            issue_title: str | None,
            issue_priority: str | None,
        ) -> None:
            captured_agent_ids.append(agent_id)
            captured_priorities.append(issue_priority)

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", fake_claim)
        rc = task_claim.do_claim(2866, None, str(tmp_path), None, "p1")
        assert rc == 0
        # #2899 — do_claim forwards issue_priority through to the DB call.
        assert captured_priorities == ["p1"]
        assert len(captured_agent_ids) == 1
        generated = captured_agent_ids[0]
        assert task_claim._is_uuid(generated)

        # Sidecar round-trips.
        sidecar = tmp_path / task_claim.AGENT_ID_SIDECAR_NAME
        assert sidecar.exists()
        assert sidecar.read_text(encoding="utf-8").strip() == generated

        # JSON response carries the agent_id.
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["agent_id"] == generated

    def test_non_uuid_agent_id_returns_three(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Regression test: ``agent-a5d7e546`` (the exact shape #2892
        documents in SKILL.md) must fail fast at the validator, not
        round-trip to the DB."""
        rc = task_claim.do_claim(2892, "agent-a5d7e546", "/tmp/wt", None, None)
        assert rc == 3
        err = capsys.readouterr().err
        assert "agent-a5d7e546" in err
        assert "UUID" in err

    def test_configuration_error_from_db_returns_three(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If the DB returns InvalidTextRepresentation despite client-
        side UUID validation (edge case — e.g. caller passed a valid
        UUID but the psycopg path tripped something else), surface as
        exit 3 not exit 1."""
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")

        def raise_cfg(*_a: Any, **_k: Any) -> None:
            raise task_claim.ClaimConfigurationError("invalid input syntax")

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", raise_cfg)
        rc = task_claim.do_claim(2866, _VALID_AGENT_ID, "/tmp/wt", None, None)
        assert rc == 3
        err = capsys.readouterr().err
        assert "configuration error" in err.lower()


class TestDoTerminalSidecarRecovery:
    """``do_terminal`` recovers ``agent_id`` from the sidecar — #2892."""

    def test_sidecar_recovery_round_trip(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Write a sidecar, then call ``do_terminal`` with only a
        worktree path — it should recover the agent_id and pass it to
        the terminal DB path."""
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        task_claim._write_agent_id_sidecar(str(tmp_path), _VALID_AGENT_ID)

        captured: list[str] = []

        def fake_terminal(
            database_url: str,
            agent_id: str,
            status: str,
            pr_number: int | None,
        ) -> int:
            captured.append(agent_id)
            return 1

        monkeypatch.setattr(task_claim, "_terminal_via_psycopg", fake_terminal)
        rc = task_claim.do_terminal(None, "succeeded", None, str(tmp_path))
        assert rc == 0
        assert captured == [_VALID_AGENT_ID]

    def test_missing_sidecar_and_no_agent_id_returns_three(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        # tmp_path has no sidecar.
        rc = task_claim.do_terminal(None, "succeeded", None, str(tmp_path))
        assert rc == 3
        err = capsys.readouterr().err
        assert "sidecar" in err.lower()

    def test_neither_agent_id_nor_worktree_returns_three(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = task_claim.do_terminal(None, "succeeded", None, None)
        assert rc == 3
        err = capsys.readouterr().err
        assert "either --agent-id or --worktree-path" in err

    def test_explicit_agent_id_wins_over_sidecar(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """If the caller passes ``--agent-id`` explicitly, use that and
        ignore the sidecar. Sidecar is a fallback, not an override."""
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        task_claim._write_agent_id_sidecar(str(tmp_path), _VALID_AGENT_ID)

        # Different UUID for the explicit arg.
        other_uuid = "00112233-4455-6677-8899-aabbccddeeff"
        captured: list[str] = []

        def fake_terminal(
            database_url: str,
            agent_id: str,
            status: str,
            pr_number: int | None,
        ) -> int:
            captured.append(agent_id)
            return 1

        monkeypatch.setattr(task_claim, "_terminal_via_psycopg", fake_terminal)
        rc = task_claim.do_terminal(other_uuid, "succeeded", None, str(tmp_path))
        assert rc == 0
        assert captured == [other_uuid]

    def test_invalid_explicit_agent_id_returns_three(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = task_claim.do_terminal("not-a-uuid", "succeeded", None, None)
        assert rc == 3
        err = capsys.readouterr().err
        assert "UUID" in err


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
        rc = task_claim.do_claim(2866, _VALID_AGENT_ID, "/tmp/wt", None)
        assert rc == 0
        assert len(called) == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip())
        # Payload now includes ``agent_id`` so the terminal call can
        # recover it if the sidecar somehow fails to write.
        assert payload["status"] == "claimed"
        assert payload["issue_number"] == 2866
        assert payload["agent_id"] == _VALID_AGENT_ID

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

        rc = task_claim.do_claim(2866, _VALID_AGENT_ID, "/tmp/wt", None)
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

        rc = task_claim.do_claim(2866, _VALID_AGENT_ID, "/tmp/wt", None)
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
        rc = task_claim.do_claim(2866, _VALID_AGENT_ID, "/tmp/wt", None)
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
        rc = task_claim.do_claim(2866, _VALID_AGENT_ID, "/tmp/wt", None)
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
        rc = task_claim.do_terminal(_VALID_AGENT_ID, "succeeded", 42, None)
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
        rc = task_claim.do_terminal(_VALID_AGENT_ID, "failed", None, None)
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
        rc = task_claim.do_terminal(_VALID_AGENT_ID, "succeeded", None, None)
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
        captured: list[tuple[int, str, str, str | None, str | None]] = []

        def fake_do_claim(
            issue: int,
            agent_id: str,
            worktree_path: str,
            issue_title: str | None,
            issue_priority: str | None,
        ) -> int:
            captured.append(
                (issue, agent_id, worktree_path, issue_title, issue_priority)
            )
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
                "--issue-priority",
                "p1",
            ]
        )
        assert rc == 0
        assert captured == [(2866, "abc", "/tmp/wt", None, "p1")]

    def test_claim_subcommand_without_issue_priority_passes_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2899 — ``--issue-priority`` is optional. Omitting it should
        result in ``do_claim`` receiving ``issue_priority=None``.
        """
        captured: list[str | None] = []

        def fake_do_claim(
            issue: int,
            agent_id: str,
            worktree_path: str,
            issue_title: str | None,
            issue_priority: str | None,
        ) -> int:
            captured.append(issue_priority)
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
        assert captured == [None]

    def test_claim_subcommand_rejects_invalid_priority(self) -> None:
        """#2899 — ``--issue-priority`` must be one of p0|p1|p2|p3."""
        with pytest.raises(SystemExit):
            task_claim.main(
                [
                    "claim",
                    "--issue",
                    "2866",
                    "--worktree-path",
                    "/tmp/wt",
                    "--issue-priority",
                    "urgent",  # not a valid priority label
                ]
            )

    def test_terminal_subcommand_dispatches_to_do_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[tuple[str, str, int | None, str | None]] = []

        def fake_do_terminal(
            agent_id: str,
            status: str,
            pr_number: int | None,
            worktree_path: str | None,
        ) -> int:
            captured.append((agent_id, status, pr_number, worktree_path))
            return 0

        monkeypatch.setattr(task_claim, "do_terminal", fake_do_terminal)
        rc = task_claim.main(
            [
                "terminal",
                "--agent-id",
                _VALID_AGENT_ID,
                "--status",
                "succeeded",
                "--pr-number",
                "42",
            ]
        )
        assert rc == 0
        assert captured == [(_VALID_AGENT_ID, "succeeded", 42, None)]

    def test_terminal_with_worktree_path_threads_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--worktree-path`` on ``terminal`` threads through to do_terminal.

        Guards the sidecar-recovery code path — the /task skill's
        terminal call passes ``--worktree-path`` (not ``--agent-id``)
        after the ``claim`` wrote the sidecar.
        """
        captured: list[tuple[Any, ...]] = []

        def fake_do_terminal(
            agent_id: Any,
            status: str,
            pr_number: int | None,
            worktree_path: str | None,
        ) -> int:
            captured.append((agent_id, status, pr_number, worktree_path))
            return 0

        monkeypatch.setattr(task_claim, "do_terminal", fake_do_terminal)
        rc = task_claim.main(
            [
                "terminal",
                "--worktree-path",
                "/tmp/wt",
                "--status",
                "succeeded",
            ]
        )
        assert rc == 0
        assert captured == [(None, "succeeded", None, "/tmp/wt")]

    def test_terminal_rejects_invalid_status(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            task_claim.main(
                [
                    "terminal",
                    "--agent-id",
                    _VALID_AGENT_ID,
                    "--status",
                    "crashed",  # not in VALID_TERMINAL_STATUSES
                ]
            )


# --------------------------------------------------------------------------
# Issue-title auto-fetch — #2923
# --------------------------------------------------------------------------


class TestFetchIssueTitleViaGh:
    """Unit tests for ``_fetch_issue_title_via_gh`` in isolation."""

    def test_happy_path_returns_stripped_title(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "  Real Title  \n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        title = task_claim._fetch_issue_title_via_gh(2923)
        assert title == "Real Title"
        # Command shape matches the SKILL.md documentation.
        assert captured[0][0] == "gh"
        assert captured[0][1:4] == ["issue", "view", "2923"]
        assert "--repo" in captured[0]
        assert task_claim.GH_REPO_SLUG in captured[0]
        assert "--json" in captured[0]
        assert "title" in captured[0]
        assert "-q" in captured[0]
        assert ".title" in captured[0]

    def test_non_zero_exit_returns_none_and_logs_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def fake_run(_cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            r.stderr = "GraphQL: Could not resolve to an Issue\n"
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        title = task_claim._fetch_issue_title_via_gh(999999)
        assert title is None
        err = capsys.readouterr().err
        assert "gh issue view" in err.lower()
        assert "999999" in err

    def test_gh_not_on_path_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def fake_run(_cmd: list[str], **_kwargs: Any) -> Any:
            raise FileNotFoundError("gh")

        monkeypatch.setattr(subprocess, "run", fake_run)
        title = task_claim._fetch_issue_title_via_gh(2923)
        assert title is None
        err = capsys.readouterr().err
        assert "gh CLI not on PATH" in err

    def test_timeout_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def fake_run(_cmd: list[str], **_kwargs: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="gh", timeout=15)

        monkeypatch.setattr(subprocess, "run", fake_run)
        title = task_claim._fetch_issue_title_via_gh(2923)
        assert title is None
        err = capsys.readouterr().err
        assert "timed out" in err.lower()

    def test_empty_stdout_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A zero-exit with empty stdout still means we don't have a title.

        Falls through to NULL so the admin page renders the same
        "(title unavailable)" as before — but with a warning log that
        makes it easy to diagnose if it ever happens in practice.
        """

        def fake_run(_cmd: list[str], **_kwargs: Any) -> Any:
            r = MagicMock()
            r.returncode = 0
            r.stdout = "\n"
            r.stderr = ""
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        title = task_claim._fetch_issue_title_via_gh(2923)
        assert title is None
        err = capsys.readouterr().err
        assert "empty title" in err.lower()


class TestDoClaimIssueTitleFallback:
    """``do_claim`` auto-fetches issue_title when the caller omits it — #2923.

    Covers the three AC scenarios from the issue body:

    * ``--issue-title ""`` + ``gh`` returns ``Real Title`` → row stored
      with ``Real Title``.
    * ``--issue-title "explicit"`` → row stored with ``explicit``
      regardless of what ``gh`` would return.
    * ``--issue-title`` omitted + ``gh`` exits non-zero → row stored with
      NULL, warning logged, no exception.
    """

    def test_empty_string_title_triggers_gh_fetch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        captured_titles: list[str | None] = []

        def fake_claim(
            _database_url: str,
            _issue_number: int,
            _agent_id: str,
            _worktree_path: str,
            issue_title: str | None,
            _issue_priority: str | None = None,
        ) -> None:
            captured_titles.append(issue_title)

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", fake_claim)

        def fake_fetch(_issue_number: int) -> str | None:
            return "Real Title"

        monkeypatch.setattr(task_claim, "_fetch_issue_title_via_gh", fake_fetch)

        rc = task_claim.do_claim(2923, _VALID_AGENT_ID, str(tmp_path), "", None)
        assert rc == 0
        assert captured_titles == ["Real Title"]

    def test_whitespace_only_title_triggers_gh_fetch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A whitespace-only ``--issue-title "   "`` should be treated
        the same as empty — otherwise the admin page still renders
        a blank chip.
        """
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        captured_titles: list[str | None] = []

        def fake_claim(
            _database_url: str,
            _issue_number: int,
            _agent_id: str,
            _worktree_path: str,
            issue_title: str | None,
            _issue_priority: str | None = None,
        ) -> None:
            captured_titles.append(issue_title)

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", fake_claim)
        monkeypatch.setattr(
            task_claim, "_fetch_issue_title_via_gh", lambda _n: "Fetched Title"
        )

        rc = task_claim.do_claim(2923, _VALID_AGENT_ID, str(tmp_path), "   ", None)
        assert rc == 0
        assert captured_titles == ["Fetched Title"]

    def test_none_title_triggers_gh_fetch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        captured_titles: list[str | None] = []
        fetch_calls: list[int] = []

        def fake_claim(
            _database_url: str,
            _issue_number: int,
            _agent_id: str,
            _worktree_path: str,
            issue_title: str | None,
            _issue_priority: str | None = None,
        ) -> None:
            captured_titles.append(issue_title)

        def fake_fetch(issue_number: int) -> str | None:
            fetch_calls.append(issue_number)
            return "Auto-fetched"

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", fake_claim)
        monkeypatch.setattr(task_claim, "_fetch_issue_title_via_gh", fake_fetch)

        rc = task_claim.do_claim(2923, _VALID_AGENT_ID, str(tmp_path), None, None)
        assert rc == 0
        assert captured_titles == ["Auto-fetched"]
        assert fetch_calls == [2923]

    def test_explicit_title_wins_over_gh_fetch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """CLI arg takes precedence — the fetch must NOT run.

        A caller that passes ``--issue-title "explicit"`` gets exactly
        ``"explicit"`` stored, even if the fetch would return something
        else. The fetch is a fallback, not an override.
        """
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        captured_titles: list[str | None] = []

        def fake_claim(
            _database_url: str,
            _issue_number: int,
            _agent_id: str,
            _worktree_path: str,
            issue_title: str | None,
            _issue_priority: str | None = None,
        ) -> None:
            captured_titles.append(issue_title)

        def fake_fetch(_issue_number: int) -> str | None:
            raise AssertionError(
                "_fetch_issue_title_via_gh must not run when the caller "
                "passed an explicit non-empty --issue-title"
            )

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", fake_claim)
        monkeypatch.setattr(task_claim, "_fetch_issue_title_via_gh", fake_fetch)

        rc = task_claim.do_claim(2923, _VALID_AGENT_ID, str(tmp_path), "explicit", None)
        assert rc == 0
        assert captured_titles == ["explicit"]

    def test_gh_fetch_failure_falls_back_to_null(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When ``gh`` fails, the claim still succeeds with NULL title.

        The helper logs a warning (covered by ``TestFetchIssueTitleViaGh``
        above) and returns None; ``do_claim`` forwards that None to the
        DB path so the row lands with ``issue_title IS NULL``. No
        exception bubbles up — the claim is never blocked on the fetch.
        """
        monkeypatch.setenv("DATABASE_URL", "postgres://fake")
        captured_titles: list[str | None] = []

        def fake_claim(
            _database_url: str,
            _issue_number: int,
            _agent_id: str,
            _worktree_path: str,
            issue_title: str | None,
            _issue_priority: str | None = None,
        ) -> None:
            captured_titles.append(issue_title)

        monkeypatch.setattr(task_claim, "_claim_via_psycopg", fake_claim)
        # Simulate the fetch returning None (gh exit non-zero, timeout,
        # gh not on PATH — any failure path).
        monkeypatch.setattr(task_claim, "_fetch_issue_title_via_gh", lambda _n: None)

        rc = task_claim.do_claim(2923, _VALID_AGENT_ID, str(tmp_path), None, None)
        # Claim still succeeds (exit 0) with NULL title.
        assert rc == 0
        assert captured_titles == [None]
