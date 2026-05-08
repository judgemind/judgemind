"""Seed ``derived.judges`` from ``derived.court_directory_snapshots`` (#4370).

Background
----------
LA County tentative-ruling HTML uses a ``JUDGE/DEPT: <Surname>/<dept>``
form-layout header that genuinely carries only the surname.  Issue #4297
landed a single-word surname-expansion helper in
:func:`ingestion.db._expand_single_word_judge_surname` that maps a
single-word surname to a canonical judge name by:

1. Looking the department up in ``derived.court_directory_snapshots``
   for the hearing-date-effective canonical name, and
2. Falling back to a surname-suffix search against ``derived.judges``
   when (1) misses.

The fallback in (2) only works when the canonical-name **already
exists** in ``derived.judges``.  But in practice, judges who only ever
appear in tentative rulings as a bare surname never get created in
``derived.judges`` — :func:`_looks_like_valid_judge_name` (db.py:760)
rejects single-word names from creating new judge rows (a defensive
guard against truncated/garbage entries).  So the helper has nothing
to expand against, and rulings continue to store ``judge_id = NULL``.

This module breaks the chicken-and-egg by seeding ``derived.judges``
from the canonical mapping in ``court_directory_snapshots``.  The
snapshot mapping is authoritative — entries like
``{"25": "Karine Mkrtchyan"}`` come from the LA judicial-officer
directory scrape — so seeding from it is safe.

Behaviour
---------
- INSERT-only (idempotent).  Never UPDATEs an existing row, never
  removes rows.  Uses ``ON CONFLICT (canonical_name, court_id) DO
  NOTHING`` against the constraint defined in migration #10.
- Skips entries that fail :func:`_looks_like_valid_judge_name` (e.g.
  empty strings, single-word values, garbage).  These should never
  appear in a directory mapping but we defend in depth.
- Resolves the snapshot's text ``court_id`` (e.g. ``"ca_los_angeles"``)
  to the ``derived.courts.id`` UUID via ``court_code`` (with the
  ``_`` -> ``-`` translation that mirrors
  :func:`resolve_judge_from_department`).
- Reads the **latest** snapshot per ``court_id``.  Historical snapshots
  are not read here — they would already have produced a seed when they
  were the latest, and re-reading them only re-confirms current state.

Wired into:

- :func:`framework.court_directory.CourtDirectory.save_snapshot` after
  every new-snapshot insert, so each daily directory refresh seeds new
  judges for its court.
- :mod:`scripts.seed_judges_from_directory_snapshots` for the
  retroactive backfill across all courts.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)


def _resolve_court_uuid(
    conn: psycopg.Connection,
    snapshot_court_id: str,
) -> str | None:
    """Resolve a snapshot's text ``court_id`` to ``derived.courts.id`` UUID.

    Snapshots use ``court_id`` strings like ``"ca_los_angeles"`` while
    ``derived.courts.court_code`` uses the dashed form ``"ca-los-angeles"``.
    Mirrors the conversion in :func:`resolve_judge_from_department`.

    Returns ``None`` if no court row matches (rare — would mean a snapshot
    for a court that has been removed from the courts table).
    """
    court_code = snapshot_court_id.replace("_", "-")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM courts WHERE court_code = %s LIMIT 1",
            (court_code,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return str(row[0])


def _latest_snapshots(
    conn: psycopg.Connection,
    *,
    only_court_id: str | None = None,
) -> list[tuple[str, dict[str, str]]]:
    """Return ``[(snapshot_court_id, mapping), ...]`` for the latest
    snapshot of each court.

    When ``only_court_id`` is provided, restricts the result to that one
    court (used by :meth:`CourtDirectory.save_snapshot` after inserting
    a new snapshot for a single court).
    """
    with conn.cursor() as cur:
        if only_court_id is not None:
            cur.execute(
                """
                SELECT court_id, mapping
                FROM court_directory_snapshots
                WHERE court_id = %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (only_court_id,),
            )
        else:
            # DISTINCT ON (court_id) returns the latest row per court_id
            # given the ORDER BY.
            cur.execute(
                """
                SELECT DISTINCT ON (court_id) court_id, mapping
                FROM court_directory_snapshots
                ORDER BY court_id, captured_at DESC
                """
            )
        rows = cur.fetchall()

    out: list[tuple[str, dict[str, str]]] = []
    for court_id, mapping in rows:
        if isinstance(mapping, dict):
            parsed = mapping
        else:
            try:
                parsed = json.loads(mapping)
            except (TypeError, ValueError):
                logger.warning(
                    "seed_judges_from_directory_snapshots: skipping court %r — "
                    "could not parse mapping JSON",
                    court_id,
                )
                continue
        out.append((court_id, parsed))
    return out


def seed_judges_from_directory_snapshots(
    conn: psycopg.Connection,
    *,
    only_court_id: str | None = None,
) -> dict[str, int]:
    """Seed ``derived.judges`` from the latest court_directory_snapshot
    of each court.

    INSERT-only and idempotent.  For each canonical judge name in each
    snapshot's ``mapping`` values, INSERTs a row into ``derived.judges``
    with ``ON CONFLICT (canonical_name, court_id) DO NOTHING``.  Names
    that fail :func:`_looks_like_valid_judge_name` are skipped with a
    debug log.

    Parameters
    ----------
    conn : psycopg.Connection
        Database connection.  The caller is responsible for committing
        the transaction.
    only_court_id : str | None
        When set, restricts the seed to a single ``court_id`` (the
        snapshot's text id, e.g. ``"ca_los_angeles"``).  Used by
        :meth:`CourtDirectory.save_snapshot` after a single-court insert.
        When ``None``, seeds across the latest snapshot of every court.

    Returns
    -------
    dict[str, int]
        Stats: ``{"courts": N, "candidates": N, "inserted": N,
        "skipped_existing": N, "skipped_invalid": N, "skipped_no_court": N}``.
        - ``courts``: number of courts whose snapshots were processed.
        - ``candidates``: number of unique (court_id, canonical_name) pairs
          considered for insertion.
        - ``inserted``: number of new judge rows actually inserted.
        - ``skipped_existing``: candidates that were already present.
        - ``skipped_invalid``: names that failed the validity guard.
        - ``skipped_no_court``: snapshots whose ``court_id`` did not
          resolve to a row in ``derived.courts``.
    """
    # Lazy import to avoid circular dependency with ingestion.db.
    from ingestion.db import _looks_like_valid_judge_name

    stats = {
        "courts": 0,
        "candidates": 0,
        "inserted": 0,
        "skipped_existing": 0,
        "skipped_invalid": 0,
        "skipped_no_court": 0,
    }

    snapshots = _latest_snapshots(conn, only_court_id=only_court_id)

    for snapshot_court_id, mapping in snapshots:
        stats["courts"] += 1
        court_uuid = _resolve_court_uuid(conn, snapshot_court_id)
        if court_uuid is None:
            stats["skipped_no_court"] += 1
            logger.warning(
                "seed_judges_from_directory_snapshots: no derived.courts row "
                "for snapshot court_id=%r — skipping",
                snapshot_court_id,
            )
            continue

        # Canonical names in the mapping values.  Use a set so that
        # multiple departments assigned to the same judge produce one
        # candidate, not N.
        unique_names = {name for name in mapping.values() if name}

        for name in unique_names:
            stats["candidates"] += 1
            if not _looks_like_valid_judge_name(name):
                stats["skipped_invalid"] += 1
                logger.debug(
                    "seed_judges_from_directory_snapshots: skipping invalid name %r for court %r",
                    name,
                    snapshot_court_id,
                )
                continue

            with conn.cursor() as cur:
                # ON CONFLICT DO NOTHING relies on migration #10's
                # UNIQUE (canonical_name, court_id) constraint.  We
                # check rowcount instead of using RETURNING because
                # ON CONFLICT DO NOTHING with RETURNING returns no row
                # on conflict — rowcount is the simpler path.
                cur.execute(
                    """
                    INSERT INTO judges (canonical_name, court_id)
                    VALUES (%s, %s::uuid)
                    ON CONFLICT (canonical_name, court_id) DO NOTHING
                    """,
                    (name, court_uuid),
                )
                if cur.rowcount > 0:
                    stats["inserted"] += 1
                    logger.info(
                        "seed_judges_from_directory_snapshots: inserted judge %r for court %r",
                        name,
                        snapshot_court_id,
                    )
                else:
                    stats["skipped_existing"] += 1

    return stats
