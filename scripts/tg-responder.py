#!/usr/bin/env python3
"""Standalone Telegram responder daemon.

Polls the Telegram inbound SQS queue every few seconds and handles simple
commands (status, pause, resume, stop) directly — replying via the Telegram
Bot API within seconds.  Complex commands (start, free text) are queued to
an inbox file for the orchestrator to pick up.

This daemon **replaces** ``scripts/tg-poll-daemon.py``, which only queued
messages without responding.

Usage::

    scripts/tg-responder.py [--interval 5] [--pid-file tmp/tg_responder.pid]

To stop the daemon gracefully, create the stop file::

    touch tmp/tg_responder.stop

The daemon checks for the stop file each iteration and exits, cleaning up
both the PID file and the stop file.

Environment:
    AWS credentials must be available (profile, env vars, or instance role).
    The bot token and chat IDs are read from Secrets Manager
    (``judgemind/telegram/bot``) at startup.
"""

from __future__ import annotations

# Ensure we are running inside the telegram-bridge venv (re-execs if not).
from _venv_helper import ensure_venv  # noqa: E402 — must run before non-stdlib imports

ensure_venv("telegram-bridge")

import argparse
import datetime
import fcntl
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import boto3
import httpx

# Add the packages dir to sys.path so we can import telegram_bridge.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BRIDGE_SRC = _REPO_ROOT / "packages" / "telegram-bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

from telegram_bridge.orchestrator import CommandKind, parse_command  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [tg-responder] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# Defaults.
DEFAULT_QUEUE_URL = (
    "https://sqs.us-west-2.amazonaws.com/155326049300/judgemind-telegram-inbound-dev"
)
DEFAULT_REGION = "us-west-2"
TELEGRAM_API_BASE = "https://api.telegram.org"

_shutdown_requested = False


# ── Secret loading ──────────────────────────────────────────────────────


def load_secret(
    *,
    secret_id: str = "judgemind/telegram/bot",
    region: str = DEFAULT_REGION,
) -> tuple[str, list[int]]:
    """Load bot token and chat IDs from Secrets Manager.

    Returns:
        A tuple of (bot_token, chat_ids).

    Raises:
        Exception: If the secret cannot be fetched or parsed.
    """
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_id)
    secret = json.loads(resp["SecretString"])
    token = secret.get("bot_token", "")
    if not token:
        raise ValueError("Bot token is empty in secret")
    chat_ids = [int(uid) for uid in secret.get("allowed_user_ids", [])]
    return token, chat_ids


# ── Telegram replies ────────────────────────────────────────────────────


def send_telegram_reply(
    text: str,
    *,
    bot_token: str,
    chat_ids: list[int],
) -> None:
    """Send a plain-text message to all chat IDs via the Telegram Bot API.

    Errors are logged but not raised — the daemon should keep running even
    if a single reply fails.
    """
    with httpx.Client(timeout=15.0) as client:
        for chat_id in chat_ids:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            try:
                resp = client.post(
                    f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
                    json=payload,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Telegram API returned %d for chat %d",
                        resp.status_code,
                        chat_id,
                    )
            except Exception:
                logger.warning(
                    "Failed to send Telegram message to chat %d", chat_id, exc_info=True
                )


# ── State file helpers ──────────────────────────────────────────────────


def read_orchestrator_state(state_file: str) -> dict[str, object]:
    """Read the orchestrator state file. Returns default state if missing/corrupt."""
    default: dict[str, object] = {"paused": False, "workers": {}}
    path = Path(state_file)
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text())
        return data
    except (json.JSONDecodeError, ValueError):
        logger.warning("Corrupt orchestrator state file — returning defaults.")
        return default


def _atomic_json_update(
    file_path: str,
    update_fn: object,
    *,
    default: object = None,
) -> None:
    """Atomically read, update, and write a JSON file using file locking.

    Uses ``fcntl.flock`` to prevent race conditions with the orchestrator
    process that may read or write the same file concurrently.

    Args:
        file_path: Path to the JSON file.
        update_fn: A callable that takes the current data and returns the
            updated data to write back.
        default: Default value if the file is missing or corrupt (default: {}).
    """
    if default is None:
        default = {}
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    try:
        with os.fdopen(fd, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                content = f.read()
                data = json.loads(content) if content.strip() else default
            except (json.JSONDecodeError, ValueError):
                data = default

            updated = update_fn(data)  # type: ignore[operator]

            f.seek(0)
            f.truncate()
            json.dump(updated, f, indent=2, default=str)
            f.write("\n")
    except Exception:
        logger.warning("Failed to update file %s", file_path, exc_info=True)


def read_agent_status_files(status_dir: str) -> list[dict[str, str]]:
    """Read all worker-N.txt files from the agent status directory.

    Returns a list of dicts with keys: worker, issue, phase, summary, updated.
    """
    path = Path(status_dir)
    if not path.exists():
        return []

    statuses: list[dict[str, str]] = []
    for f in sorted(path.glob("worker-*.txt")):
        try:
            content = f.read_text()
            entry: dict[str, str] = {"worker": f.stem}
            for line in content.strip().splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    entry[key.strip()] = value.strip()
            statuses.append(entry)
        except OSError:
            continue
    return statuses


# ── Command handlers ────────────────────────────────────────────────────


def format_status_reply(
    state: dict[str, object],
    *,
    agent_statuses: list[dict[str, str]],
) -> str:
    """Format a human-readable status summary."""
    workers = state.get("workers", {})
    paused = state.get("paused", False)
    lines: list[str] = []

    if not workers and not agent_statuses:
        msg = "No active issues."
        if paused:
            msg += " (paused — not spawning new work)"
        return msg

    # Workers from orchestrator state.
    if isinstance(workers, dict):
        for _key, w in sorted(workers.items()):
            if isinstance(w, dict):
                lines.append(
                    f"Worker-{w.get('worker_number', '?')}: "
                    f"#{w.get('issue_number', '?')} ({w.get('phase', '?')}) "
                    f"— {w.get('issue_title', '?')}"
                )

    # Supplement with agent-status files (may have workers not in orchestrator state).
    seen_workers = {
        f"worker-{w.get('worker_number')}"
        for w in workers.values()
        if isinstance(w, dict)
    }
    for s in agent_statuses:
        worker_name = s.get("worker", "")
        if worker_name not in seen_workers:
            issue = s.get("issue", "?")
            phase = s.get("phase", "?")
            summary = s.get("summary", "")
            lines.append(f"{worker_name}: {issue} ({phase}) — {summary}")

    result = "\n".join(lines) if lines else "No active issues."
    if paused:
        result += "\n\n(paused — not spawning new work)"
    return result


def handle_pause(state_file: str) -> None:
    """Set paused=True in the orchestrator state file (with file locking)."""

    def _set_paused(state: dict[str, object]) -> dict[str, object]:
        state["paused"] = True
        return state

    _atomic_json_update(
        state_file, _set_paused, default={"paused": False, "workers": {}}
    )


def handle_resume(state_file: str) -> None:
    """Set paused=False in the orchestrator state file (with file locking)."""

    def _clear_paused(state: dict[str, object]) -> dict[str, object]:
        state["paused"] = False
        return state

    _atomic_json_update(
        state_file, _clear_paused, default={"paused": False, "workers": {}}
    )


def handle_stop(issue_number: int, stop_requests_file: str) -> None:
    """Append a stop request for *issue_number* to the stop requests file (with file locking)."""

    def _append_stop(data: list[dict[str, object]]) -> list[dict[str, object]]:
        data.append(
            {
                "issue_number": issue_number,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )
        return data

    _atomic_json_update(stop_requests_file, _append_stop, default=[])


def queue_to_inbox(message: dict[str, object], inbox_file: str) -> None:
    """Append a raw message to the inbox file for the orchestrator.

    Uses file-level locking to prevent races with the orchestrator reading.
    """
    path = Path(inbox_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
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

            existing.append(message)
            f.seek(0)
            f.truncate()
            json.dump(existing, f, indent=2, default=str)
            f.write("\n")
    except Exception:
        logger.warning("Failed to write inbox file", exc_info=True)


# ── Dispatch ────────────────────────────────────────────────────────────


def dispatch_command(
    *,
    message: dict[str, object],
    bot_token: str,
    chat_ids: list[int],
    state_file: str,
    agent_status_dir: str,
    stop_requests_file: str,
    inbox_file: str,
) -> None:
    """Parse and dispatch a single inbound message."""
    text = str(message.get("text", ""))
    cmd = parse_command(text)

    if cmd.kind == CommandKind.STATUS:
        state = read_orchestrator_state(state_file)
        agent_statuses = read_agent_status_files(agent_status_dir)
        reply = format_status_reply(state, agent_statuses=agent_statuses)
        send_telegram_reply(reply, bot_token=bot_token, chat_ids=chat_ids)

    elif cmd.kind == CommandKind.PAUSE:
        handle_pause(state_file)
        send_telegram_reply(
            "Paused. No new issues will be spawned.",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )

    elif cmd.kind == CommandKind.RESUME:
        handle_resume(state_file)
        send_telegram_reply(
            "Resumed. Will spawn issues as normal.",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )

    elif cmd.kind == CommandKind.STOP:
        handle_stop(cmd.issue_number, stop_requests_file)
        send_telegram_reply(
            f"Stop request noted for #{cmd.issue_number}. "
            "Will not spawn more work for this issue.",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )

    elif cmd.kind == CommandKind.START:
        queue_to_inbox(dict(message), inbox_file)
        send_telegram_reply(
            f"Acknowledged: will start issue #{cmd.issue_number}. "
            "Queued for orchestrator.",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )

    elif cmd.kind == CommandKind.FREE_TEXT:
        queue_to_inbox(dict(message), inbox_file)
        send_telegram_reply(
            f"Received your message. Queued for orchestrator: {text}",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )


# ── SQS polling ─────────────────────────────────────────────────────────


def poll_sqs(
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


# ── Daemon lifecycle ────────────────────────────────────────────────────


def _handle_signal(signum: int, _frame: object) -> None:
    """Set the shutdown flag on SIGTERM/SIGINT."""
    global _shutdown_requested  # noqa: PLW0603
    logger.info("Received signal %d — shutting down.", signum)
    _shutdown_requested = True


def write_pid_file(path: Path) -> None:
    """Write the current PID to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def check_stop_file(pid_file: Path) -> bool:
    """Check whether the stop file exists.

    Convention: ``tmp/foo.pid`` -> ``tmp/foo.stop``.
    Returns ``True`` if the stop file was found.
    """
    stop_path = pid_file.with_suffix(".stop")
    if stop_path.exists():
        logger.info("Stop file detected (%s) — shutting down.", stop_path)
        return True
    return False


def cleanup_daemon_files(pid_file: Path) -> None:
    """Remove the PID file and stop file if they exist."""
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass
    stop_path = pid_file.with_suffix(".stop")
    try:
        stop_path.unlink(missing_ok=True)
    except OSError:
        pass


# ── Main daemon loop ────────────────────────────────────────────────────


def run_daemon(
    *,
    pid_file: Path,
    queue_url: str,
    region: str,
    interval: float,
    state_file: str,
    agent_status_dir: str,
    stop_requests_file: str,
    inbox_file: str,
    secret_id: str = "judgemind/telegram/bot",
) -> None:
    """Main daemon loop: poll SQS, dispatch commands, repeat."""
    global _shutdown_requested  # noqa: PLW0603

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    write_pid_file(pid_file)

    # Load secret at startup.
    bot_token, chat_ids = load_secret(secret_id=secret_id, region=region)
    logger.info(
        "Started (PID %d). Polling every %ds. Chat IDs: %s",
        os.getpid(),
        int(interval),
        chat_ids,
    )

    sqs = boto3.client("sqs", region_name=region)

    try:
        while not _shutdown_requested:
            try:
                messages = poll_sqs(sqs, queue_url)
                for msg in messages:
                    dispatch_command(
                        message=msg,
                        bot_token=bot_token,
                        chat_ids=chat_ids,
                        state_file=state_file,
                        agent_status_dir=agent_status_dir,
                        stop_requests_file=stop_requests_file,
                        inbox_file=inbox_file,
                    )
                if messages:
                    logger.info("Processed %d message(s).", len(messages))
            except Exception:
                logger.warning("Poll cycle failed", exc_info=True)

            # Sleep in small increments for responsive shutdown.
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline and not _shutdown_requested:
                if check_stop_file(pid_file):
                    _shutdown_requested = True
                    break
                time.sleep(min(1.0, deadline - time.monotonic()))
    finally:
        cleanup_daemon_files(pid_file)
        logger.info("Daemon stopped.")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Standalone Telegram responder daemon")
    parser.add_argument(
        "--pid-file",
        default="tmp/tg_responder.pid",
        help="Path to the PID file (default: tmp/tg_responder.pid)",
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
        default=5.0,
        help="Polling interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--state-file",
        default="tmp/orchestrator_state.json",
        help="Path to orchestrator state file",
    )
    parser.add_argument(
        "--agent-status-dir",
        default="tmp/agent-status",
        help="Directory containing worker-N.txt status files",
    )
    parser.add_argument(
        "--stop-requests-file",
        default="tmp/stop_requests.json",
        help="Path to stop requests JSON file",
    )
    parser.add_argument(
        "--inbox-file",
        default="tmp/tg_inbox.json",
        help="Path to inbox file for orchestrator (default: tmp/tg_inbox.json)",
    )
    parser.add_argument(
        "--secret-id",
        default="judgemind/telegram/bot",
        help="Secrets Manager secret ID for bot token",
    )
    args = parser.parse_args()

    run_daemon(
        pid_file=Path(args.pid_file),
        queue_url=args.queue_url,
        region=args.region,
        interval=args.interval,
        state_file=args.state_file,
        agent_status_dir=args.agent_status_dir,
        stop_requests_file=args.stop_requests_file,
        inbox_file=args.inbox_file,
        secret_id=args.secret_id,
    )


if __name__ == "__main__":
    main()
