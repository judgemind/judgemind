"""Shared helpers for parsing flat-hash S3 keys and detecting mislabels.

A "flat-hash" key has shape ``{state}/{county}/{court}/raw/{hex64}.{ext}``
— the content-addressed scheme where the filename is the SHA-256 of the
bytes. The 2026-03-28 migration in commit ``fbf8e38`` (archived as
``scripts/archive/migrate_s3_keys.py``) rewrote objects under this shape;
its ``CopyObject`` step preserved the original metadata ``content-hash``,
so a mislabel is detectable by comparing filename hex64 to metadata
``content-hash``.

These helpers were duplicated verbatim across
``scripts/cleanup_mislabeled_s3_2661.py`` and
``scripts/repoint_mislabeled_documents_4439.py`` (both archived in
#4565 after their runtime applies landed; the post-#4447 import shape
is preserved verbatim at ``scripts/archive/cleanup_mislabeled_s3_2661.py``
and ``scripts/archive/repoint_mislabeled_documents_4439.py``) — both
are ECS oneshot scripts run via ``scripts/ecs-run-task.sh`` which
uploads only the single requested script to S3 at run time, so
peer-script imports are not available. Importing from ``framework.*``
IS available because the helpers are bundled into the ingestion-worker
/ scraper-framework Docker image and reachable inside the oneshot
container without extra ``COPY`` steps (the existing
``from framework.logging import configure_structlog`` imports in those
scripts are precedent).

The helpers in this module are pure functions over inputs the caller has
already obtained (the S3 key string and the metadata ``content-hash``
read via ``HeadObject``). Use ``framework.s3_integrity`` for the heavier
three-way bytes-level integrity check that GETs the object.

Note: the existing ``framework.s3_integrity.KEY_PATTERN`` regex matches
content-addressed keys generally (``.*/raw/<hex64>.<ext>``) without
capturing state/county/court. The flat-hash regex here is **stricter** —
it requires the full ``{state}/{county}/{court}/raw/`` shape and captures
each segment. Both regexes coexist intentionally: ``s3_integrity`` is
used to verify any content-addressed key (e.g. court_directory snapshots
under different prefixes), while ``s3_keys`` is used to enumerate and
group the flat-hash subset by county.
"""

from __future__ import annotations

import re

from botocore.exceptions import ClientError

# Matches content-addressed flat-hash keys:
# ``{state}/{county}/{court}/raw/{hex64}.{ext}``.
#
# Captures state, county, court, the filename hash, and the extension so
# callers can group by county and rebuild twin keys without re-parsing.
# Lowercase hex is required by the upstream migration (the writers always
# emit lowercase); uppercase hex is intentionally not accepted here so
# that an audit caller does not silently treat capitalised legacy keys
# as flat-hash matches.
KEY_PATTERN = re.compile(
    r"^(?P<state>[a-z]{2})/(?P<county>[^/]+)/(?P<court>[^/]+)/raw/"
    r"(?P<hash>[0-9a-f]{64})\.(?P<ext>\w+)$"
)


def parse_flat_hash_key(key: str) -> dict[str, str] | None:
    """Parse a flat-hash content-addressed key.

    Returns a dict with keys ``state``, ``county``, ``court``, ``hash``,
    ``ext`` on match, or ``None`` if the key does not match the expected
    shape.

    Anything that does not match (e.g. date-partitioned legacy keys,
    court_directory snapshots, non-raw paths) is silently skipped — the
    return value of ``None`` is the canonical "not in scope" signal that
    callers use to skip the row.

    Examples::

        parse_flat_hash_key("ca/orange/superior_court/raw/aabb...64.pdf")
        # -> {"state": "ca", "county": "orange", "court": "superior_court",
        #     "hash": "aabb...64", "ext": "pdf"}

        parse_flat_hash_key("ca/orange/superior_court/raw/2026/04/01/x.pdf")
        # -> None
    """
    m = KEY_PATTERN.match(key)
    if m is None:
        return None
    return dict(m.groupdict())


def is_mislabel(filename_hash: str, metadata_hash: str | None) -> bool:
    """Return ``True`` if *filename_hash* differs from *metadata_hash*.

    The mislabel signature from the 2026-03-28 migration is:
    filename hex64 ≠ metadata ``content-hash``. Both must be hex64
    strings (this function does not normalise — the caller is expected
    to have read the metadata via ``HeadObject`` which preserves the
    original casing).

    A missing metadata hash (``None`` or empty string) does **not**
    count as a mislabel — that is a separate problem class handled by
    the audit script (``scripts/audit_s3_raw_mislabels.py``). This
    function is narrowly scoped to "filename ≠ metadata" (issue #2661).
    """
    if not metadata_hash:
        return False
    return filename_hash != metadata_hash


def head_object_metadata_hash(s3_client: object, bucket: str, key: str) -> str | None:
    """Return the metadata ``content-hash`` for *key*, or ``None`` if missing.

    Returns ``None`` for **both** a missing metadata field AND a 404 —
    callers must treat both cases as "not a mislabel" / "no twin
    available." Both cleanup and repoint scripts rely on this collapsed
    semantics; surfacing missing-metadata vs 404 separately is the
    responsibility of the audit script.

    Other ``ClientError`` codes (e.g. ``AccessDenied``) propagate to the
    caller — silently swallowing them would mask a real problem.
    """
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    metadata: dict[str, str] = head.get("Metadata", {}) or {}
    return metadata.get("content-hash") or None


def build_twin_key(mislabel_key: str, metadata_hash: str) -> str | None:
    """Build the correctly-named twin key for *mislabel_key*.

    The twin key reuses the same ``{state}/{county}/{court}/raw/``
    prefix and file extension but replaces the filename hash with
    *metadata_hash* — i.e. the location at which a correctly-named
    copy of the same bytes would live if one exists.

    Returns ``None`` if *mislabel_key* does not parse as a flat-hash
    key.

    Used by the repoint flow
    (``scripts/archive/repoint_mislabeled_documents_4439.py`` —
    archived in #4565 after its runtime apply landed) to compute the
    candidate twin from a mislabel's metadata, then HEAD the twin to
    confirm it exists and is itself correctly-labelled.
    """
    parsed = parse_flat_hash_key(mislabel_key)
    if parsed is None:
        return None
    return (
        f"{parsed['state']}/{parsed['county']}/{parsed['court']}/raw/"
        f"{metadata_hash}.{parsed['ext']}"
    )
