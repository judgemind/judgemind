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
from botocore.exceptions import ClientError

from .hashing import sha256_hex

if TYPE_CHECKING:
    import psycopg

logger = structlog.get_logger(__name__)

# Base-class methods whose side effects every court depends on: S3 archival,
# content-hash dedup, the snapshot insert, the roster-cache clear and the
# derived.judges seed.  A subclass copy of any of them silently drifts as the
# base gains steps: SB and SF copies of save_snapshot skipped the judge seed
# for months (#5161).  ``CourtDirectory.__init_subclass__`` rejects overrides
# (#5163).
SEALED_METHODS: frozenset[str] = frozenset({"save_snapshot", "_seed_judges", "_is_duplicate"})


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

    Subclasses must not override ``save_snapshot``, ``_seed_judges`` or
    ``_is_duplicate`` (see ``SEALED_METHODS``); defining one raises
    ``TypeError`` at class creation.  Court-specific fetching and parsing
    belongs in ``fetch_current()``.
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        overridden = sorted(SEALED_METHODS.intersection(vars(cls)))
        if overridden:
            names = ", ".join(overridden)
            raise TypeError(
                f"{cls.__module__}.{cls.__qualname__} overrides {names}. "
                "CourtDirectory.save_snapshot and its helpers own S3 archival, "
                "dedup, the snapshot insert, the roster-cache clear and the "
                "derived.judges seed, and a subclass copy drifts out of date "
                "(#5161). Put court-specific fetching and parsing in "
                "fetch_current(). If a court needs different storage, add a "
                "hook to CourtDirectory.save_snapshot in "
                "framework/court_directory.py so every court keeps the full "
                "contract (#5163)."
            )

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

        Uses content-addressed S3 keys (``directories/{court_id}/{content_hash}.html``,
        or ``.pdf`` with ``application/pdf`` when ``raw`` is a PDF)
        so the same directory content always maps to the same key, making
        uploads idempotent. A HeadObject check skips the upload when the
        content is already archived.

        Skips the DB insert if the content hash matches the most recent
        snapshot for this court (dedup).

        Seeds ``derived.judges`` from the court's latest snapshot on both
        paths: after a new insert, and when the roster is unchanged (#5161).
        The seed is idempotent, so an unchanged refresh catches up a court
        whose snapshot was never seeded or whose judges were rebuilt.  Only
        a new insert clears the roster name cache.

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
        # PDF directories (e.g. LA's weekly Judicial Directory, #4874) keep a
        # .pdf key and content type so the archived raw opens as what it is.
        is_pdf = raw.startswith(b"%PDF")
        extension, content_type = ("pdf", "application/pdf") if is_pdf else ("html", "text/html")
        s3_key = f"directories/{court_id}/{content_hash}.{extension}"

        # Archive raw to S3 only if not already present (content-addressed).
        try:
            self._s3.head_object(Bucket=self._bucket, Key=s3_key)
            logger.debug(
                "Directory already archived in S3, skipping upload",
                court_id=court_id,
                s3_key=s3_key,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "404":
                logger.error("S3 HeadObject failed for %s: %s", s3_key, exc)
                raise
            # Object doesn't exist — upload it.
            self._s3.put_object(
                Bucket=self._bucket,
                Key=s3_key,
                Body=raw,
                ContentType=content_type,
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
            # Still seed: the stored snapshot may never have been seeded (SB's
            # old save_snapshot override skipped it) or derived.judges may have
            # been rebuilt since.  The seed is idempotent (#5161).  The roster
            # cache is not cleared: the roster itself did not change.
            judges_seeded = self._seed_judges(court_id)
            logger.info(
                "Directory unchanged — skipping DB insert",
                court_id=court_id,
                content_hash=content_hash[:12],
                judges_seeded=judges_seeded,
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

        # Invalidate the roster name cache so that resolve_judge() picks up
        # the new snapshot immediately instead of waiting for TTL expiry.
        # Lazy import to avoid circular dependency (framework -> ingestion).
        from ingestion.db import clear_roster_cache

        clear_roster_cache()

        judges_seeded = self._seed_judges(court_id)

        logger.info(
            "Saved directory snapshot",
            court_id=court_id,
            departments=len(mapping),
            content_hash=content_hash[:12],
            judges_seeded=judges_seeded,
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

    def get_mapping_for_date(
        self,
        court_id: str,
        as_of: datetime,
        *,
        fallback: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        """Get the date-appropriate directory mapping with optional fallback.

        Looks up the historical snapshot closest to (but not after) ``as_of``.
        If no snapshot predates the given datetime, returns ``fallback`` (which
        defaults to ``None``).

        This is the preferred method for scrapers to use when looking up judge
        names for a ruling — it ensures each ruling uses the directory that was
        active at the time of the hearing, not the current directory.

        Parameters
        ----------
        court_id : str
            The court identifier (e.g. ``"ca_los_angeles"``).
        as_of : datetime
            The hearing date or capture date of the ruling.
        fallback : dict[str, str] | None
            Mapping to return when no snapshot predates ``as_of``.
            Typically the mapping from the most recent ``fetch_and_snapshot()``.

        Returns
        -------
        dict[str, str] | None
            The date-appropriate {department: judge_name} mapping, or
            ``fallback`` if no historical snapshot exists.
        """
        snapshot = self.get_snapshot(court_id, as_of)
        if snapshot is not None:
            return snapshot
        return fallback

    def fetch_and_snapshot(self, court_id: str) -> dict[str, str]:
        """Fetch the live directory and save a snapshot.

        Convenience method that calls ``fetch_current()``, then
        ``save_snapshot()``, and returns the parsed mapping.

        A mapping with zero departments is never saved (#4880). The page
        fetched but parsed to nothing (usually a layout change), so saving it
        would make an empty roster the court's newest snapshot and wipe judge
        attribution. Lookups keep using the last good snapshot, and
        ``framework.roster_health.fetch_roster`` records the attempt as an
        ``empty_roster`` failure. The check lives here, at the fetch step,
        so ``save_snapshot`` stays a plain archive-and-store primitive.

        Parameters
        ----------
        court_id : str
            The court identifier.

        Returns
        -------
        dict[str, str]
            The parsed {department: judge_name} mapping from the live
            directory (empty when the page parsed to nothing).
        """
        raw, mapping = self.fetch_current()
        if not mapping:
            logger.warning(
                "Directory parsed to 0 departments; not saving snapshot",
                court_id=court_id,
                raw_bytes=len(raw),
                content_hash=sha256_hex(raw)[:12],
            )
            return mapping
        self.save_snapshot(raw, mapping, court_id)
        return mapping

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _seed_judges(self, court_id: str) -> int:
        """Seed ``derived.judges`` from the court's latest snapshot.

        Gives the single-word surname expansion helper (#4297) multi-word
        canonical-name candidates to expand against (#4370).  Idempotent, so
        it runs on every refresh, changed or not (#5161).  Best-effort: a
        seed failure is logged and rolled back, never raised, so it cannot
        lose a snapshot insert that already committed.

        Returns
        -------
        int
            The number of judges inserted (0 when the seed failed).
        """
        # Lazy import to avoid circular dependency (framework -> ingestion).
        from ingestion.judge_seed import seed_judges_from_directory_snapshots

        try:
            seed_stats = seed_judges_from_directory_snapshots(self._conn, only_court_id=court_id)
            self._conn.commit()
        except Exception:
            logger.exception(
                "Judge seed from snapshot failed — continuing",
                court_id=court_id,
            )
            try:
                self._conn.rollback()
            except Exception:  # pragma: no cover
                pass
            return 0
        return seed_stats.get("inserted", 0)

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
