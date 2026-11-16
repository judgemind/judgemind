"""Reusable helper for mirroring local files to S3 as telemetry artifacts.

Designed to be imported by scripts that want to upload a local file to S3
without failing hard when S3 is unavailable (network issues, missing creds,
boto3 not installed in the current venv, etc.).

Typical usage::

    from telemetry_upload import mirror_to_s3
    ok = mirror_to_s3(Path("/tmp/review-log.jsonl"), "ralph-reviews/2026-01-01/agent-abc-123.jsonl")

The function is best-effort: all errors are swallowed, logged as warnings,
and the return value indicates success/failure without raising.

The upload bounds its own run time (#5116), so callers need no external
``timeout(1)``, which macOS does not ship. The boto3 client gets short
connect/read timeouts and a small retry budget, and the CLI entry point
runs the whole upload (boto3 import included) under an overall deadline.
The CLI prints exactly one outcome line on stdout: ``ok: ...`` or
``failed: <reason>``.
"""
# permanent: true

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_BUCKET = "judgemind-document-archive-dev"

# Per-request bounds for the boto3 client. Two attempts at 5s connect + 10s
# read stay inside the CLI's overall deadline.
_CONNECT_TIMEOUT_SECS = 5
_READ_TIMEOUT_SECS = 10
_MAX_ATTEMPTS = 2

# Overall wall-clock bound for the CLI, covering boto3 import, credential
# resolution and every retry. Override with JUDGEMIND_TELEMETRY_DEADLINE_SECS.
_DEFAULT_DEADLINE_SECS = 20.0


def _upload(local_path: Path, s3_key: str, bucket: str) -> tuple[bool, str]:
    """Upload one file. Returns ``(ok, message)`` and never raises."""
    try:
        import boto3  # type: ignore[import]
        from botocore.config import Config  # type: ignore[import]
    except ImportError:
        return False, (
            f"boto3 not available — skipping S3 upload of {local_path} "
            f"to s3://{bucket}/{s3_key}"
        )

    try:
        data = local_path.read_bytes()
    except OSError as exc:
        return False, f"cannot read {local_path} for S3 upload: {exc}"

    try:
        config = Config(
            connect_timeout=_CONNECT_TIMEOUT_SECS,
            read_timeout=_READ_TIMEOUT_SECS,
            retries={"max_attempts": _MAX_ATTEMPTS, "mode": "standard"},
        )
        client = boto3.client("s3", config=config)
        client.put_object(Bucket=bucket, Key=s3_key, Body=data)
    except Exception as exc:  # noqa: BLE001 — telemetry must never raise
        return False, (
            f"S3 upload of {local_path} to s3://{bucket}/{s3_key} failed: "
            f"{type(exc).__name__}: {exc}"
        )
    return True, (
        f"uploaded {local_path} to s3://{bucket}/{s3_key} ({len(data)} bytes)"
    )


def _resolve_bucket(bucket: str | None) -> str:
    if bucket is None:
        return os.environ.get("JUDGEMIND_TELEMETRY_BUCKET", _DEFAULT_BUCKET)
    return bucket


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
    ok, message = _upload(local_path, s3_key, _resolve_bucket(bucket))
    if ok:
        logger.info("%s", message)
    else:
        logger.warning("%s", message)
    return ok


def mirror_with_deadline(
    local_path: Path,
    s3_key: str,
    deadline_secs: float,
    bucket: str | None = None,
) -> tuple[bool, str]:
    """Run the upload under an overall wall-clock deadline.

    The upload runs on a daemon thread, so a hang anywhere in it (import,
    DNS, credential lookup, a stalled socket) cannot hold the caller past
    ``deadline_secs``. Returns ``(ok, message)`` and never raises.
    """
    resolved = _resolve_bucket(bucket)
    result: list[tuple[bool, str]] = []

    def _run() -> None:
        result.append(_upload(local_path, s3_key, resolved))

    worker = threading.Thread(target=_run, name="telemetry-upload", daemon=True)
    worker.start()
    worker.join(deadline_secs)
    if worker.is_alive() or not result:
        return False, (
            f"S3 upload of {local_path} to s3://{resolved}/{s3_key} "
            f"did not finish within {deadline_secs:g}s"
        )
    return result[0]


def _deadline_from_env() -> float:
    raw = os.environ.get("JUDGEMIND_TELEMETRY_DEADLINE_SECS", "")
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_DEADLINE_SECS
    return value if value > 0 else _DEFAULT_DEADLINE_SECS


def main(argv: list[str]) -> int:
    """CLI entry point. Prints one ``ok:``/``failed:`` line on stdout."""
    import sys

    if len(argv) != 2:
        print(
            "Usage: python3 telemetry_upload.py <local-path> <s3-key>",
            file=sys.stderr,
        )
        return 2
    ok, message = mirror_with_deadline(Path(argv[0]), argv[1], _deadline_from_env())
    print(f"{'ok' if ok else 'failed'}: {message}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    # basic-config-allow: #4400 — telemetry_upload.py is a tiny CLI shim
    # whose only job is to upload one local file to S3.  It does not call
    # ``logger.<level>(..., extra={...})``; the WARNING-level stderr stream
    # is sufficient for the operator-only failure paths.  Migrating to
    # ``configure_structlog`` here would add a framework dependency the
    # script does not otherwise need.  See #4400 for the guard that
    # forbids the pattern in every other ``scripts/*.py``.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    code = main(sys.argv[1:])
    # os._exit, not sys.exit: a timed-out upload thread is still running, and
    # interpreter shutdown could otherwise wait on boto3 internals.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
