"""S3 content-hash integrity helpers.

Provides verify/assert helpers and an exception type for detecting mislabeled
S3 objects — objects whose filename hash (the content-addressed key) does not
match the actual bytes stored at that key.

Key shape supported: <prefix>/raw/<hex64>.<ext>
"""

from __future__ import annotations

import logging
import re

from botocore.exceptions import ClientError

from .hashing import sha256_hex

logger = logging.getLogger(__name__)

# Matches content-addressed keys: .../raw/{64-hex-char-hash}.{ext}
# Mirrors the shape in scripts/archive/migrate_s3_keys.py:40 but captures
# the hash group for extraction.
KEY_PATTERN = re.compile(r".*/raw/([0-9a-f]{64})\.\w+$")


class S3MislabelError(ValueError):
    """Raised when an S3 object's filename hash does not match its stored bytes
    or when required content-hash metadata is missing."""


def extract_filename_hash(key: str) -> str | None:
    """Return the 64-char hex hash embedded in a content-addressed S3 key.

    Returns None if the key does not match the expected shape.

    Examples::

        extract_filename_hash("ca/orange/superior_court/raw/aabbcc...64.html")
        # -> "aabbcc...64"

        extract_filename_hash("ca/orange/superior_court/raw/not_a_hash.html")
        # -> None
    """
    m = KEY_PATTERN.match(key)
    if m is None:
        return None
    return m.group(1)


def verify_key_matches_bytes(s3_client: object, bucket: str, key: str) -> bool:
    """Verify that the S3 object at *key* is labelled correctly.

    Three-way integrity check:
    1. Filename hash (extracted from the content-addressed key)
    2. Metadata ``content-hash`` header stored on the S3 object
    3. SHA-256 of the object's actual bytes

    All three must agree.

    Returns:
        True if the object exists and all three hashes agree.
        False if the object does not exist (404).

    Raises:
        S3MislabelError: if any hash mismatch is detected, or if the
            ``content-hash`` metadata field is absent.
        S3MislabelError: if the key cannot be parsed (no embedded hash).
        ClientError: for non-404 S3 errors (propagated to caller).
    """
    # Step 1: HEAD to check existence and read metadata.
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "404":
            return False
        raise

    # Step 2: Extract filename hash from the key.
    filename_hash = extract_filename_hash(key)
    if filename_hash is None:
        raise S3MislabelError(
            f"Cannot parse content-hash from S3 key: {key!r}. Expected shape .../raw/<hex64>.<ext>"
        )

    # Step 3: Read metadata content-hash.
    metadata_hash: str | None = head.get("Metadata", {}).get("content-hash")
    if not metadata_hash:
        raise S3MislabelError(
            f"S3 object at {key!r} is missing Metadata['content-hash']. "
            f"Cannot verify integrity without the stored hash."
        )

    # Step 4: GET the bytes and compute the actual hash.
    body_bytes: bytes = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    bytes_hash = sha256_hex(body_bytes)

    # Step 5: All three must agree.
    mismatches: list[str] = []
    if filename_hash != bytes_hash:
        mismatches.append(f"filename_hash={filename_hash!r} != bytes_hash={bytes_hash!r}")
    if metadata_hash != bytes_hash:
        mismatches.append(f"metadata_hash={metadata_hash!r} != bytes_hash={bytes_hash!r}")

    if mismatches:
        raise S3MislabelError(
            f"S3 object mislabel detected for key {key!r}: " + "; ".join(mismatches)
        )

    return True


def assert_key_matches_bytes(s3_client: object, bucket: str, key: str) -> None:
    """Assert that the S3 object at *key* is labelled correctly.

    Calls :func:`verify_key_matches_bytes` and raises :class:`S3MislabelError`
    if the object fails integrity checks or does not exist.

    This is the assert variant — use it when you need a hard failure rather
    than a boolean result (e.g. post-copy verification in migration scripts).

    Raises:
        S3MislabelError: if any hash mismatch is detected, metadata is
            missing, the key cannot be parsed, or the object does not exist.
        ClientError: for non-404 S3 errors (propagated to caller).
    """
    exists = verify_key_matches_bytes(s3_client, bucket, key)
    if not exists:
        raise S3MislabelError(f"S3 object does not exist at key {key!r} in bucket {bucket!r}.")
