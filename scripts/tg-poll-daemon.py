#!/usr/bin/env python3
"""Background Telegram SQS polling daemon.

Polls the Telegram inbound SQS queue on a configurable interval and writes
received commands to a JSON inbox file.  The orchestrator reads this file
periodically instead of running the poll inline, saving context tokens and
letting the orchestrator do useful work between checks.

Usage:
    scripts/tg-poll-daemon.py --inbox tmp/tg_inbox.json [--interval 30] [--pid-file tmp/tg_poll.pid]

The daemon writes its PID to ``--pid-file`` (default ``tmp/tg_poll.pid``) for
graceful shutdown.  To stop the daemon, create the corresponding stop file
(same path with ``.stop`` instead of ``.pid``)::

    touch tmp/tg_poll.stop

The daemon checks for the stop file each iteration and exits gracefully,
cleaning up both the PID file and the stop file on exit.  This avoids
``kill`` which is blocked in sandboxed environments like Claude Code.

Environment:
    AWS credentials must be available (profile, env vars, or instance role).
    The SQS queue URL is derived from the Secrets Manager secret
    ``judgemind/telegram/bot`` or can be overridden with ``--queue-url``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import time
from pathlib import Path

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [tg-poll] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Default SQS queue URL (dev environment).
DEFAULT_QUEUE_URL = (
    "https://sqs.us-west-2.amazonaws.com/155326049300/judgemind-telegram-inbound-dev"
)
DEFAULT_REGION = "us-west-2"

_shutdown_requested = False


def _handle_signal(signum: int, _frame: object) -> None:
    """Set the shutdown flag on SIGTERM/SIGINT."""
    global _shutdown_requested  # noqa: PLW0603
    logger.info("Received signal %d — shutting down.", signum)
    _shutdown_requested = True


def _write_pid_file(path: Path) -> None:
    """Write the current PID to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def _remove_pid_file(path: Path) -> None:
    """Remove the PID file if it exists."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _stop_file_path(pid_file: Path) -> Path:
    """Derive the stop-file path from a PID file path.

    Convention: ``tmp/foo.pid`` -> ``tmp/foo.stop``.
    """
    return pid_file.with_suffix(".stop")


def _check_stop_file(pid_file: Path) -> bool:
    """Check whether the stop file exists, and if so set the shutdown flag.

    Returns ``True`` if the stop file was found (shutdown requested).
    """
    global _shutdown_requested  # noqa: PLW0603
    stop_path = _stop_file_path(pid_file)
    if stop_path.exists():
        logger.info("Stop file detected (%s) — shutting down.", stop_path)
        _shutdown_requested = True
        return True
    return False


def _remove_stop_file(pid_file: Path) -> None:
    """Remove the stop file derived from *pid_file* if it exists."""
    stop_path = _stop_file_path(pid_file)
    try:
        stop_path.unlink(missing_ok=True)
    except OSError:
        pass


def _poll_sqs(
    sqs_client: object,
    queue_url: str,
) -> list[dict[str, object]]:
    """Receive and delete all pending messages from *queue_url*.

    Returns a list of parsed message bodies (dicts).
    """
    messages: list[dict[str, object]] = []

    while True:
        resp = sqs_client.receive_message(  # type: ignore[union-attr]
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
        )
        batch = resp.get("Messages", [])
        if not batch:
            break

        entries_to_delete = []
        for raw in batch:
            try:
                body = json.loads(raw["Body"])
                messages.append(body)
            except (json.JSONDecodeError, KeyError):
                logger.warning("Failed to parse SQS message: %s", raw.get("Body"))
            entries_to_delete.append(
                {"Id": raw["MessageId"], "ReceiptHandle": raw["ReceiptHandle"]}
            )

        if entries_to_delete:
            sqs_client.delete_message_batch(  # type: ignore[union-attr]
                QueueUrl=queue_url,
                Entries=entries_to_delete,
            )

    return messages


def _append_to_inbox(inbox_path: Path, messages: list[dict[str, object]]) -> None:
    """Atomically append *messages* to the JSON-array inbox file.

    Uses file-level locking (``fcntl.flock``) to prevent races with the
    orchestrator reading and clearing the file concurrently.
    """
    if not messages:
        return

    inbox_path.parent.mkdir(parents=True, exist_ok=True)

    # Open (or create) the file in read-write mode.
    fd = os.open(str(inbox_path), os.O_RDWR | os.O_CREAT)
    try:
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                content = f.read()
                existing: list[dict[str, object]] = (
                    json.loads(content) if content.strip() else []
                )
            except (json.JSONDecodeError, ValueError):
                existing = []

            existing.extend(messages)

            f.seek(0)
            f.truncate()
            json.dump(existing, f, indent=2, default=str)
            f.write("\n")
    except Exception:
        # fd is already closed by fdopen context manager on success;
        # on exception before fdopen, close manually.
        logger.warning("Failed to write inbox file", exc_info=True)


def run_daemon(
    *,
    inbox_path: Path,
    pid_file: Path,
    queue_url: str,
    region: str,
    interval: float,
) -> None:
    """Main daemon loop: poll SQS and append to inbox on *interval*."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _write_pid_file(pid_file)
    logger.info(
        "Started (PID %d). Polling every %ds. Inbox: %s",
        os.getpid(),
        int(interval),
        inbox_path,
    )

    sqs = boto3.client("sqs", region_name=region)

    try:
        while not _shutdown_requested:
            try:
                messages = _poll_sqs(sqs, queue_url)
                if messages:
                    _append_to_inbox(inbox_path, messages)
                    logger.info("Wrote %d message(s) to inbox.", len(messages))
            except Exception:
                logger.warning("Poll cycle failed", exc_info=True)

            # Sleep in small increments so we can respond to signals and
            # stop-file requests promptly.
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline and not _shutdown_requested:
                _check_stop_file(pid_file)
                if _shutdown_requested:
                    break
                time.sleep(min(1.0, deadline - time.monotonic()))
    finally:
        _remove_pid_file(pid_file)
        _remove_stop_file(pid_file)
        logger.info("Daemon stopped.")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Background Telegram SQS polling daemon"
    )
    parser.add_argument(
        "--inbox",
        default="tmp/tg_inbox.json",
        help="Path to the inbox JSON file (default: tmp/tg_inbox.json)",
    )
    parser.add_argument(
        "--pid-file",
        default="tmp/tg_poll.pid",
        help="Path to the PID file (default: tmp/tg_poll.pid)",
    )
    parser.add_argument(
        "--queue-url",
        default=DEFAULT_QUEUE_URL,
        help="SQS queue URL to poll",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        help="AWS region (default: us-west-2)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Polling interval in seconds (default: 30)",
    )
    args = parser.parse_args()

    run_daemon(
        inbox_path=Path(args.inbox),
        pid_file=Path(args.pid_file),
        queue_url=args.queue_url,
        region=args.region,
        interval=args.interval,
    )


if __name__ == "__main__":
    main()
