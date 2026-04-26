"""Reusable helper for mirroring local files to S3 as telemetry artifacts.

Designed to be imported by scripts that want to upload a local file to S3
without failing hard when S3 is unavailable (network issues, missing creds,
boto3 not installed in the current venv, etc.).

Typical usage::

    from telemetry_upload import mirror_to_s3
    ok = mirror_to_s3(Path("/tmp/review-log.jsonl"), "ralph-reviews/2026-01-01/agent-abc-123.jsonl")

The function is best-effort: all errors are swallowed, logged as warnings,
and the return value indicates success/failure without raising.
"""
# permanent: true

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_BUCKET = "judgemind-document-archive-dev"


def mirror_to_s3(
    local_path: Path,
    s3_key: str,
    bucket: str | None = None,
) -> bool:
    """Upload a local file to S3 as a telemetry artifact.

    All errors (network, credentials, missing boto3) are swallowed and logged
    as warnings so callers never fail hard on a telemetry upload.

    Args:
        local_path: Path to the local file to upload.
        s3_key: S3 object key (no leading slash).
        bucket: Target S3 bucket name.  Defaults to the ``JUDGEMIND_TELEMETRY_BUCKET``
            environment variable, falling back to ``judgemind-document-archive-dev``.

    Returns:
        ``True`` on a successful upload; ``False`` on any failure.
    """
    if bucket is None:
        bucket = os.environ.get("JUDGEMIND_TELEMETRY_BUCKET", _DEFAULT_BUCKET)

    try:
        import boto3  # type: ignore[import]
        from botocore.exceptions import (  # type: ignore[import]
            ClientError,
            EndpointConnectionError,
            NoCredentialsError,
        )
    except ImportError:
        logger.warning(
            "boto3 not available — skipping S3 upload of %s to s3://%s/%s",
            local_path,
            bucket,
            s3_key,
        )
        return False

    try:
        data = local_path.read_bytes()
    except OSError as exc:
        logger.warning(
            "Cannot read %s for S3 upload: %s",
            local_path,
            exc,
        )
        return False

    try:
        client = boto3.client("s3")
        client.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=data,
        )
        logger.info(
            "Uploaded %s to s3://%s/%s (%d bytes)",
            local_path,
            bucket,
            s3_key,
            len(data),
        )
        return True
    except (ClientError, EndpointConnectionError, NoCredentialsError) as exc:
        logger.warning(
            "S3 upload of %s to s3://%s/%s failed: %s",
            local_path,
            bucket,
            s3_key,
            exc,
        )
        return False


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if len(sys.argv) != 3:
        print(
            "Usage: python3 telemetry_upload.py <local-path> <s3-key>",
            file=sys.stderr,
        )
        sys.exit(1)
    ok = mirror_to_s3(Path(sys.argv[1]), sys.argv[2])
    sys.exit(0 if ok else 1)
