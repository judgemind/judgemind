"""Court directory snapshot infrastructure for historical department-to-judge lookups.

Courts publish department-to-judge directories that change over time as judges
rotate, retire, or get reassigned. This module provides a base class for
snapshotting these directories so that historical lookups are accurate during
backfills.

Architecture:
  - Raw directory HTML/JSON is archived to S3 for re-parsing if needed.
  - Parsed {department: judge_name} mappings are stored in the
    ``court_directory_snapshots`` DB table with a content hash for dedup.
  - ``get_snapshot(court_id, as_of)`` retrieves the closest snapshot on or
    before a given datetime, enabling accurate historical judge assignment.

See: https://github.com/judgemind/judgemind/issues/412
"""

from __future__ import annotations

import abc
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from .hashing import sha256_hex

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)


class CourtDirectory(abc.ABC):
    """Abstract base class for court department-to-judge directory snapshots.

    Subclasses must implement ``fetch_current()`` to fetch the live directory
    from a court website. The base class handles S3 archival, DB storage,
    content-hash deduplication, and historical lookups.

    Parameters
    ----------
    s3_client : object
        A boto3 S3 client for archiving raw directory responses.
    s3_bucket : str
        The S3 bucket name for archival.
    db_conn : psycopg.Connection
        A psycopg3 connection for reading/writing snapshots.
    """

    def __init__(
        self,
        s3_client: object,
        s3_bucket: str,
        db_conn: psycopg.Connection,
    ) -> None:
        self._s3 = s3_client
        self._bucket = s3_bucket
        self._conn = db_conn

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def fetch_current(self) -> tuple[bytes, dict[str, str]]:
        """Fetch the live court directory.

        Returns
        -------
        tuple[bytes, dict[str, str]]
            A tuple of (raw_response_bytes, parsed_mapping) where
            parsed_mapping maps normalized department strings to judge
            full names.
        """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_snapshot(
        self,
        raw: bytes,
        mapping: dict[str, str],
        court_id: str,
    ) -> bool:
        """Archive a directory snapshot to S3 and DB.

        Skips the DB insert if the content hash matches the most recent
        snapshot for this court (dedup). The S3 upload always happens
        so we have a complete timeline of raw responses.

        Parameters
        ----------
        raw : bytes
            The raw directory response (HTML, JSON, etc.).
        mapping : dict[str, str]
            The parsed {department: judge_name} mapping.
        court_id : str
            The court identifier (e.g. ``"ca_los_angeles"``).

        Returns
        -------
        bool
            True if a new snapshot was inserted, False if deduplicated.
        """
        content_hash = sha256_hex(raw)
        now = datetime.now(UTC)
        s3_key = f"directories/{court_id}/{now.strftime('%Y%m%dT%H%M%SZ')}.html"

        # Always archive raw to S3
        self._s3.put_object(
            Bucket=self._bucket,
            Key=s3_key,
            Body=raw,
            ContentType="text/html",
            Metadata={
                "court-id": court_id,
                "content-hash": content_hash,
                "captured-at": now.isoformat(),
            },
        )
        logger.info(
            "Archived directory to S3",
            court_id=court_id,
            s3_key=s3_key,
            size=len(raw),
        )

        # Check if the most recent snapshot has the same content hash
        if self._is_duplicate(court_id, content_hash):
            logger.info(
                "Directory unchanged — skipping DB insert",
                court_id=court_id,
                content_hash=content_hash[:12],
            )
            return False

        # Insert new snapshot
        mapping_json = json.dumps(mapping, sort_keys=True)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO court_directory_snapshots
                    (court_id, captured_at, s3_key, mapping, content_hash)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (court_id, now, s3_key, mapping_json, content_hash),
            )
        self._conn.commit()

        logger.info(
            "Saved directory snapshot",
            court_id=court_id,
            departments=len(mapping),
            content_hash=content_hash[:12],
        )
        return True

    def get_snapshot(
        self,
        court_id: str,
        as_of: datetime,
    ) -> dict[str, str] | None:
        """Get the directory snapshot closest to (but not after) a given datetime.

        Parameters
        ----------
        court_id : str
            The court identifier.
        as_of : datetime
            The point in time to look up. Returns the most recent snapshot
            captured on or before this datetime.

        Returns
        -------
        dict[str, str] | None
            The parsed {department: judge_name} mapping, or None if no
            snapshot exists for this court before the given datetime.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT mapping FROM court_directory_snapshots
                WHERE court_id = %s AND captured_at <= %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (court_id, as_of),
            )
            row = cur.fetchone()

        if row is None:
            logger.debug(
                "No directory snapshot found",
                court_id=court_id,
                as_of=as_of.isoformat(),
            )
            return None

        mapping: dict[str, str] = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        logger.debug(
            "Found directory snapshot",
            court_id=court_id,
            as_of=as_of.isoformat(),
            departments=len(mapping),
        )
        return mapping

    def fetch_and_snapshot(self, court_id: str) -> dict[str, str]:
        """Fetch the live directory and save a snapshot.

        Convenience method that calls ``fetch_current()``, then
        ``save_snapshot()``, and returns the parsed mapping.

        Parameters
        ----------
        court_id : str
            The court identifier.

        Returns
        -------
        dict[str, str]
            The parsed {department: judge_name} mapping from the live
            directory.
        """
        raw, mapping = self.fetch_current()
        self.save_snapshot(raw, mapping, court_id)
        return mapping

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_duplicate(self, court_id: str, content_hash: str) -> bool:
        """Check if the most recent snapshot for this court has the same content hash."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT content_hash FROM court_directory_snapshots
                WHERE court_id = %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (court_id,),
            )
            row = cur.fetchone()

        if row is None:
            return False
        return row[0] == content_hash
