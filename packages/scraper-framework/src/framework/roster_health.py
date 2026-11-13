"""Per-court judge-roster fetch telemetry (issue #4872).

``framework.runner._fetch_rosters`` refreshes every court's dept→judge
directory before the scrapers run. A failed fetch used to be logged only as a
warning, so broken rosters went unnoticed for months (SF #4733, Santa Clara
#4732, LA since 2026-07). This module records one
``telemetry.roster_fetch_attempts`` row per attempt, success or failure, so
``/admin/data-quality`` can show each court's last successful fetch, last
outcome and error class.

A deduplicated (unchanged) fetch inserts no ``court_directory_snapshots`` row,
so snapshot timestamps alone cannot say when a roster last fetched
successfully. The attempt row is written on every success, deduplicated or not.

Recording is best-effort: a telemetry write failure is logged and swallowed so
it can never stop the scrapers from running.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)

# Error classes surfaced on the dashboard. Keep in sync with the
# telemetry.roster_fetch_attempts.error_class column comment (migration 68).
ERROR_HTTP_403 = "http_403"
ERROR_HTTP_4XX = "http_4xx"
ERROR_HTTP_5XX = "http_5xx"
ERROR_REDIRECT = "redirect"
ERROR_NETWORK = "network"
ERROR_EMPTY_ROSTER = "empty_roster"
ERROR_MISSING_COURT_ID = "missing_court_id"
ERROR_IMPORT = "import_error"
ERROR_OTHER = "other"

_MAX_ERROR_MESSAGE_CHARS = 2000


class EmptyRosterError(Exception):
    """The directory fetched without error but parsed to zero departments."""


class MissingCourtIdError(Exception):
    """The CourtDirectory subclass has no ``COURT_ID`` class attribute."""


@dataclass(frozen=True)
class RosterFetchResult:
    """Outcome of one roster fetch attempt."""

    court_id: str
    directory_class: str
    outcome: str  # "success" | "failure"
    department_count: int | None = None
    snapshot_changed: bool | None = None
    error_class: str | None = None
    http_status: int | None = None
    error_message: str | None = None


def classify_roster_error(exc: BaseException) -> tuple[str, int | None]:
    """Map a roster-fetch exception to ``(error_class, http_status)``."""
    if isinstance(exc, MissingCourtIdError):
        return ERROR_MISSING_COURT_ID, None
    if isinstance(exc, EmptyRosterError):
        return ERROR_EMPTY_ROSTER, None
    if isinstance(exc, ImportError):
        return ERROR_IMPORT, None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 403:
            return ERROR_HTTP_403, status
        if 300 <= status < 400:
            return ERROR_REDIRECT, status
        if status >= 500:
            return ERROR_HTTP_5XX, status
        return ERROR_HTTP_4XX, status
    if isinstance(exc, httpx.TooManyRedirects):
        return ERROR_REDIRECT, None
    if isinstance(exc, httpx.TransportError):
        # Covers timeouts, DNS failures, connection resets.
        return ERROR_NETWORK, None
    return ERROR_OTHER, None


_LOOKUP_FAILED = object()


def _latest_snapshot_id(
    db_conn: psycopg.Connection,  # type: ignore[type-arg]
    court_id: str,
) -> object:
    """Return the newest snapshot id for the court, or ``_LOOKUP_FAILED``.

    Only used to tell a changed snapshot from a deduplicated one, so a lookup
    error degrades ``snapshot_changed`` to NULL rather than failing the fetch.
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(id) FROM court_directory_snapshots WHERE court_id = %s",
                (court_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Snapshot id lookup failed", court_id=court_id, error=str(exc))
        return _LOOKUP_FAILED


def _snapshot_changed(before: object, after: object) -> bool | None:
    if before is _LOOKUP_FAILED or after is _LOOKUP_FAILED:
        return None
    return after is not None and after != before


def fetch_roster(
    module_path: str,
    class_name: str,
    court_id: str,
    *,
    s3_client: object,
    bucket: str,
    db_conn: psycopg.Connection,  # type: ignore[type-arg]
) -> RosterFetchResult:
    """Fetch and snapshot one court's roster; never raises.

    Returns a :class:`RosterFetchResult` describing the outcome. Zero parsed
    departments counts as a failure (``empty_roster``): a directory page that
    parses to nothing is broken, even if it returned HTTP 200.
    """
    try:
        mod = importlib.import_module(module_path)
        cls: Any = getattr(mod, class_name)
        if getattr(cls, "COURT_ID", None) is None:
            raise MissingCourtIdError(f"{class_name} has no COURT_ID class attribute")
        before = _latest_snapshot_id(db_conn, court_id)
        directory = cls(s3_client=s3_client, s3_bucket=bucket, db_conn=db_conn)
        mapping = directory.fetch_and_snapshot(court_id)
        department_count = len(mapping) if mapping else 0
        if department_count == 0:
            raise EmptyRosterError(f"{class_name} parsed 0 departments")
        after = _latest_snapshot_id(db_conn, court_id)
        return RosterFetchResult(
            court_id=court_id,
            directory_class=class_name,
            outcome="success",
            department_count=department_count,
            snapshot_changed=_snapshot_changed(before, after),
        )
    except Exception as exc:  # noqa: BLE001 — every failure must be recorded
        error_class, http_status = classify_roster_error(exc)
        message = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_MESSAGE_CHARS]
        return RosterFetchResult(
            court_id=court_id,
            directory_class=class_name,
            outcome="failure",
            department_count=0 if isinstance(exc, EmptyRosterError) else None,
            error_class=error_class,
            http_status=http_status,
            error_message=message,
        )


def record_roster_attempt(
    db_conn: psycopg.Connection,  # type: ignore[type-arg]
    result: RosterFetchResult,
) -> bool:
    """Insert one ``telemetry.roster_fetch_attempts`` row; never raises.

    Returns True when the row was written.
    """
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO telemetry.roster_fetch_attempts
                    (court_id, directory_class, outcome, department_count,
                     snapshot_changed, error_class, http_status, error_message)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    result.court_id,
                    result.directory_class,
                    result.outcome,
                    result.department_count,
                    result.snapshot_changed,
                    result.error_class,
                    result.http_status,
                    result.error_message,
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001 — telemetry must not stop scrapers
        logger.warning(
            "Failed to record roster fetch attempt",
            court_id=result.court_id,
            error=str(exc),
        )
        return False
