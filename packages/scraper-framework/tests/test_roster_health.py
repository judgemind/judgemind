"""Tests for per-court roster fetch telemetry (issue #4872)."""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from framework import runner
from framework.court_directory import CourtDirectory
from framework.roster_health import (
    ERROR_EMPTY_ROSTER,
    ERROR_HTTP_4XX,
    ERROR_HTTP_5XX,
    ERROR_HTTP_403,
    ERROR_IMPORT,
    ERROR_MISSING_COURT_ID,
    ERROR_NETWORK,
    ERROR_OTHER,
    ERROR_REDIRECT,
    EmptyRosterError,
    MissingCourtIdError,
    RosterFetchResult,
    classify_roster_error,
    fetch_roster,
    record_roster_attempt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://court.example/directory")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _FakeDb:
    """Minimal psycopg stand-in: serves MAX(id) lookups, records INSERTs."""

    def __init__(
        self, snapshot_ids: list[int | None] | None = None, fail_insert: bool = False
    ) -> None:
        self._snapshot_ids = list(snapshot_ids or [])
        self._fail_insert = fail_insert
        self.inserts: list[tuple[Any, ...]] = []

    def cursor(self) -> _FakeDb:
        return self

    def __enter__(self) -> _FakeDb:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        if "INSERT INTO telemetry.roster_fetch_attempts" in sql:
            if self._fail_insert:
                raise RuntimeError("relation does not exist")
            self.inserts.append(params)
            return
        self._last_select = self._snapshot_ids.pop(0) if self._snapshot_ids else None

    def fetchone(self) -> tuple[int | None]:
        return (self._last_select,)


def _directory_module(fetch: Any, *, court_id: str | None = "ca_test") -> types.ModuleType:
    """Build a fake module holding a CourtDirectory subclass whose fetch is ``fetch``."""

    attrs: dict[str, Any] = {
        "fetch_current": lambda self: (b"", {}),
        "fetch_and_snapshot": lambda self, cid: fetch(cid),
    }
    if court_id is not None:
        attrs["COURT_ID"] = court_id
    cls = type("FakeCourtDirectory", (CourtDirectory,), attrs)
    mod = types.ModuleType("fake_mod")
    mod.FakeCourtDirectory = cls  # type: ignore[attr-defined]
    return mod


def _run(mod: types.ModuleType, db: _FakeDb) -> RosterFetchResult:
    with patch("framework.roster_health.importlib.import_module", return_value=mod):
        return fetch_roster(
            "fake_mod",
            "FakeCourtDirectory",
            "ca_test",
            s3_client=MagicMock(),
            bucket="bucket",
            db_conn=db,
        )


# ---------------------------------------------------------------------------
# classify_roster_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(403), (ERROR_HTTP_403, 403)),
        (_http_error(404), (ERROR_HTTP_4XX, 404)),
        (_http_error(503), (ERROR_HTTP_5XX, 503)),
        (_http_error(301), (ERROR_REDIRECT, 301)),
        (httpx.TooManyRedirects("loop"), (ERROR_REDIRECT, None)),
        (httpx.ConnectTimeout("timed out"), (ERROR_NETWORK, None)),
        (httpx.ConnectError("dns"), (ERROR_NETWORK, None)),
        (EmptyRosterError("0 departments"), (ERROR_EMPTY_ROSTER, None)),
        (MissingCourtIdError("no COURT_ID"), (ERROR_MISSING_COURT_ID, None)),
        (ModuleNotFoundError("no module"), (ERROR_IMPORT, None)),
        (ValueError("parse blew up"), (ERROR_OTHER, None)),
    ],
)
def test_classify_roster_error(exc: BaseException, expected: tuple[str, int | None]) -> None:
    assert classify_roster_error(exc) == expected


# ---------------------------------------------------------------------------
# fetch_roster
# ---------------------------------------------------------------------------


def test_fetch_roster_success_with_new_snapshot() -> None:
    db = _FakeDb(snapshot_ids=[10, 11])
    result = _run(_directory_module(lambda cid: {"1": "Judge A", "2": "Judge B"}), db)
    assert result.outcome == "success"
    assert result.court_id == "ca_test"
    assert result.directory_class == "FakeCourtDirectory"
    assert result.department_count == 2
    assert result.snapshot_changed is True
    assert result.error_class is None


def test_fetch_roster_success_deduplicated_is_still_success() -> None:
    """An unchanged roster inserts no snapshot row but is a successful fetch."""
    db = _FakeDb(snapshot_ids=[10, 10])
    result = _run(_directory_module(lambda cid: {"1": "Judge A"}), db)
    assert result.outcome == "success"
    assert result.snapshot_changed is False


def test_fetch_roster_snapshot_lookup_error_keeps_success() -> None:
    """A failing MAX(id) lookup must not turn a good fetch into a failure."""

    class _BrokenLookupDb(_FakeDb):
        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            if "MAX(id)" in sql:
                raise RuntimeError("connection reset")
            super().execute(sql, params)

    result = _run(_directory_module(lambda cid: {"1": "Judge A"}), _BrokenLookupDb())
    assert result.outcome == "success"
    assert result.department_count == 1
    assert result.snapshot_changed is None


def test_fetch_roster_passes_registry_court_id() -> None:
    seen: list[str] = []

    def fetch(cid: str) -> dict[str, str]:
        seen.append(cid)
        return {"1": "Judge A"}

    _run(_directory_module(fetch), _FakeDb(snapshot_ids=[None, 1]))
    assert seen == ["ca_test"]


def test_fetch_roster_http_503_is_failure() -> None:
    def fetch(cid: str) -> dict[str, str]:
        raise _http_error(503)

    result = _run(_directory_module(fetch), _FakeDb())
    assert result.outcome == "failure"
    assert result.error_class == ERROR_HTTP_5XX
    assert result.http_status == 503
    assert result.error_message is not None
    assert "HTTPStatusError" in result.error_message
    assert result.department_count is None
    assert result.snapshot_changed is None


def test_fetch_roster_zero_departments_is_failure() -> None:
    result = _run(_directory_module(lambda cid: {}), _FakeDb(snapshot_ids=[1, 2]))
    assert result.outcome == "failure"
    assert result.error_class == ERROR_EMPTY_ROSTER
    assert result.department_count == 0


def test_fetch_roster_missing_court_id_is_failure() -> None:
    called: list[str] = []

    def fetch(cid: str) -> dict[str, str]:
        called.append(cid)
        return {"1": "Judge A"}

    result = _run(_directory_module(fetch, court_id=None), _FakeDb())
    assert result.outcome == "failure"
    assert result.error_class == ERROR_MISSING_COURT_ID
    assert result.court_id == "ca_test"
    assert called == []


def test_fetch_roster_import_error_is_failure() -> None:
    with patch(
        "framework.roster_health.importlib.import_module",
        side_effect=ModuleNotFoundError("No module named 'courts.ca.gone'"),
    ):
        result = fetch_roster(
            "courts.ca.gone",
            "GoneCourtDirectory",
            "ca_gone",
            s3_client=MagicMock(),
            bucket="bucket",
            db_conn=_FakeDb(),
        )
    assert result.outcome == "failure"
    assert result.error_class == ERROR_IMPORT
    assert result.court_id == "ca_gone"


def test_fetch_roster_truncates_long_error_message() -> None:
    def fetch(cid: str) -> dict[str, str]:
        raise ValueError("x" * 10_000)

    result = _run(_directory_module(fetch), _FakeDb())
    assert result.error_message is not None
    assert len(result.error_message) == 2000


# ---------------------------------------------------------------------------
# record_roster_attempt
# ---------------------------------------------------------------------------


def test_record_roster_attempt_writes_failure_with_error() -> None:
    db = _FakeDb()
    result = RosterFetchResult(
        court_id="ca_los_angeles",
        directory_class="LACourtDirectory",
        outcome="failure",
        error_class=ERROR_HTTP_5XX,
        http_status=503,
        error_message="HTTPStatusError: Server error '503 Service Unavailable'",
    )
    assert record_roster_attempt(db, result) is True
    assert db.inserts == [
        (
            "ca_los_angeles",
            "LACourtDirectory",
            "failure",
            None,
            None,
            ERROR_HTTP_5XX,
            503,
            "HTTPStatusError: Server error '503 Service Unavailable'",
        )
    ]


def test_record_roster_attempt_swallows_db_errors() -> None:
    db = _FakeDb(fail_insert=True)
    result = RosterFetchResult(court_id="ca_x", directory_class="X", outcome="success")
    assert record_roster_attempt(db, result) is False


# ---------------------------------------------------------------------------
# runner._fetch_rosters
# ---------------------------------------------------------------------------


def test_runner_fetch_rosters_records_one_attempt_per_court() -> None:
    """Every registry court gets a telemetry row, including failures."""

    def fake_fetch(module_path: str, class_name: str, court_id: str, **_: Any) -> RosterFetchResult:
        if court_id == "ca_los_angeles":
            return RosterFetchResult(
                court_id=court_id,
                directory_class=class_name,
                outcome="failure",
                error_class=ERROR_HTTP_5XX,
                http_status=503,
                error_message="HTTPStatusError: 503",
            )
        return RosterFetchResult(
            court_id=court_id,
            directory_class=class_name,
            outcome="success",
            department_count=5,
            snapshot_changed=False,
        )

    db = _FakeDb()
    with (
        patch("framework.runner.fetch_roster", side_effect=fake_fetch),
        patch("framework.runner.time.sleep"),
    ):
        runner._fetch_rosters(MagicMock(), "bucket", db)

    recorded = {row[0]: row for row in db.inserts}
    assert set(recorded) == {cid for _, _, cid in runner._ROSTER_DIRECTORIES}
    la = recorded["ca_los_angeles"]
    assert la[2] == "failure"
    assert la[5] == ERROR_HTTP_5XX
    assert "503" in la[7]
    assert recorded["ca_orange"][2] == "success"


def test_runner_fetch_rosters_survives_real_directory_failure() -> None:
    """A stub CourtDirectory.fetch_current that raises yields a failure row."""

    class BrokenDirectory(CourtDirectory):
        COURT_ID = "ca_broken"

        def fetch_current(self) -> tuple[bytes, dict[str, str]]:
            raise RuntimeError("directory page layout changed")

    mod = types.ModuleType("broken_mod")
    mod.BrokenDirectory = BrokenDirectory  # type: ignore[attr-defined]
    db = _FakeDb()
    with (
        # Patch sleep before import_module: mock resolves dotted targets via
        # importlib, so it must run while import_module is still real.
        patch("framework.runner.time.sleep"),
        patch.object(
            runner, "_ROSTER_DIRECTORIES", [("broken_mod", "BrokenDirectory", "ca_broken")]
        ),
        patch("framework.roster_health.importlib.import_module", return_value=mod),
    ):
        runner._fetch_rosters(MagicMock(), "bucket", db)

    assert len(db.inserts) == 1
    row = db.inserts[0]
    assert row[0] == "ca_broken"
    assert row[2] == "failure"
    assert row[5] == ERROR_OTHER
    assert "directory page layout changed" in row[7]
