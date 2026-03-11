#!/usr/bin/env python3
"""Standalone Telegram responder daemon.

Polls the Telegram inbound SQS queue every few seconds and interprets all
messages as free text using a lightweight Claude API call (Haiku).  The
interpreter understands the current orchestrator state and can respond
naturally to any question, while also extracting actionable commands
(start, pause, resume, stop) for the orchestrator.

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
    The Anthropic API key must be available via the ``ANTHROPIC_API_KEY``
    environment variable or via Secrets Manager
    (``judgemind/anthropic/api-key``).
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
from typing import Any

try:
    import boto3
    import httpx
except ModuleNotFoundError as exc:
    _pkg_dir = Path(__file__).resolve().parents[1] / "packages" / "telegram-bridge"
    print(
        f"ERROR: Missing dependency: {exc.name}\n"
        f"\n"
        f"The telegram-bridge venv exists but is missing required packages.\n"
        f"Install them with:\n"
        f"\n"
        f"    {_pkg_dir}/.venv/bin/pip install -e \"{_pkg_dir}[dev]\" --quiet\n",
        file=sys.stderr,
    )
    sys.exit(1)

# Add the packages dir to sys.path so we can import telegram_bridge.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BRIDGE_SRC = _REPO_ROOT / "packages" / "telegram-bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

try:
    from telegram_bridge.formatting import linkify_github_refs  # noqa: E402
    from telegram_bridge.interpreter import (  # noqa: E402
        RateLimitError,
        RateLimiter,
        interpret_message,
    )
except ModuleNotFoundError as exc:
    _pkg_dir2 = Path(__file__).resolve().parents[1] / "packages" / "telegram-bridge"
    print(
        f"ERROR: Missing dependency: {exc.name}\n"
        f"\n"
        f"The telegram-bridge package is not installed in the venv.\n"
        f"Install it with:\n"
        f"\n"
        f"    {_pkg_dir2}/.venv/bin/pip install -e \"{_pkg_dir2}[dev]\" --quiet\n",
        file=sys.stderr,
    )
    sys.exit(1)

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


def load_anthropic_api_key(
    *,
    secret_id: str = "judgemind/anthropic/api-key",
    region: str = DEFAULT_REGION,
) -> str | None:
    """Load the Anthropic API key from env var or Secrets Manager.

    Returns ``None`` if the key is not available (Claude interpretation
    will be skipped and a fallback response sent).
    """
    # Check env var first.
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key

    # Try Secrets Manager.
    try:
        client = boto3.client("secretsmanager", region_name=region)
        resp = client.get_secret_value(SecretId=secret_id)
        secret_str = resp["SecretString"]
        # The secret may be a raw key string or JSON with an "api_key" field.
        try:
            data = json.loads(secret_str)
            return data.get("api_key", secret_str)
        except (json.JSONDecodeError, ValueError):
            return secret_str
    except Exception:
        logger.info("Anthropic API key not found — Claude interpretation disabled.")
        return None


# ── Telegram replies ────────────────────────────────────────────────────


def send_telegram_reply(
    text: str,
    *,
    bot_token: str,
    chat_ids: list[int],
    parse_mode: str | None = None,
) -> None:
    """Send a message to all chat IDs via the Telegram Bot API.

    Args:
        text: The message text to send.
        bot_token: Telegram bot token.
        chat_ids: List of chat IDs to send to.
        parse_mode: Optional Telegram parse mode (e.g. ``"HTML"``).
            When ``None``, the message is sent as plain text.

    If Telegram returns 400 (e.g. due to formatting issues), the message
    is retried without ``parse_mode`` so it is delivered as plain text
    rather than silently dropped.

    Errors are logged but not raised — the daemon should keep running even
    if a single reply fails.
    """
    with httpx.Client(timeout=15.0) as client:
        for chat_id in chat_ids:
            payload: dict[str, object] = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            try:
                resp = client.post(
                    f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
                    json=payload,
                )
                # Fallback: if Telegram rejects the formatted message (400),
                # retry without parse_mode so the message is delivered.
                if resp.status_code == 400 and parse_mode is not None:
                    logger.warning(
                        "Telegram returned 400 for chat %d — retrying as plain text.",
                        chat_id,
                    )
                    fallback_payload: dict[str, object] = {
                        "chat_id": chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    }
                    resp = client.post(
                        f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
                        json=fallback_payload,
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


def read_orchestrator_status(status_file: str) -> dict[str, Any] | None:
    """Read the orchestrator status JSON file.

    This is the rich status file written by OrchestratorBridge.write_status(),
    containing active agents, open PRs, queue, etc.

    Returns ``None`` if the file is missing or corrupt.
    """
    path = Path(status_file)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        logger.warning("Corrupt orchestrator status file — ignoring.")
        return None


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
                    f"--- {w.get('issue_title', '?')}"
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
            lines.append(f"{worker_name}: {issue} ({phase}) --- {summary}")

    result = "\n".join(lines) if lines else "No active issues."
    if paused:
        result += "\n\n(paused --- not spawning new work)"
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


def dispatch_message(
    *,
    message: dict[str, object],
    bot_token: str,
    chat_ids: list[int],
    state_file: str,
    status_file: str,
    agent_status_dir: str,
    stop_requests_file: str,
    inbox_file: str,
    anthropic_api_key: str | None = None,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """Interpret and dispatch a single inbound message using Claude API.

    All messages are sent to the Claude interpreter for natural-language
    understanding.  The interpreter returns a reply and optional actions
    (start, pause, resume, stop) which are executed by the daemon.

    If the Anthropic API key is not available, falls back to a simple
    acknowledgment.

    Args:
        rate_limiter: Optional rate limiter passed through to
            :func:`interpret_message`.  When ``None``, no rate limiting
            is applied at the dispatch level (the interpreter's own
            default limiter still applies unless explicitly disabled).
    """
    text = str(message.get("text", ""))

    if not anthropic_api_key:
        # No API key — fall back to simple acknowledgment and queue.
        queue_to_inbox(dict(message), inbox_file)
        send_telegram_reply(
            f"Received your message (interpreter unavailable): {text}",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )
        return

    # Read orchestrator status for context.
    orchestrator_status = read_orchestrator_status(status_file)

    # If no status file exists, build a basic context from agent status files
    # and orchestrator state.
    if orchestrator_status is None:
        state = read_orchestrator_state(state_file)
        agent_statuses = read_agent_status_files(agent_status_dir)
        orchestrator_status = {
            "active_agents": agent_statuses,
            "paused": state.get("paused", False),
            "workers": state.get("workers", {}),
        }

    # Call the Claude interpreter.
    try:
        result = interpret_message(
            text=text,
            orchestrator_status=orchestrator_status,
            api_key=anthropic_api_key,
            rate_limiter=rate_limiter,
        )
    except RateLimitError as exc:
        logger.info("Rate limit exceeded — queuing message for orchestrator.")
        queue_to_inbox(dict(message), inbox_file)
        send_telegram_reply(
            f"Rate limit reached. {exc.retry_after:.0f}s until next Claude interpretation. "
            f"Your message has been queued.",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )
        return
    except Exception:
        logger.warning("Claude interpreter failed — falling back", exc_info=True)
        queue_to_inbox(dict(message), inbox_file)
        send_telegram_reply(
            f"Received your message (interpreter error): {text}",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )
        return

    # Format the reply for Telegram: convert #N refs to clickable HTML links
    # and escape special characters for HTML parse mode.
    formatted_reply = linkify_github_refs(result.reply)
    send_telegram_reply(
        formatted_reply,
        bot_token=bot_token,
        chat_ids=chat_ids,
        parse_mode="HTML",
    )

    # Execute any actions.
    for action in result.actions:
        action_type = action.get("type", "")

        if action_type == "pause":
            handle_pause(state_file)
            logger.info("Action: pause")

        elif action_type == "resume":
            handle_resume(state_file)
            logger.info("Action: resume")

        elif action_type == "stop":
            issue_num = action.get("issue")
            if isinstance(issue_num, int):
                handle_stop(issue_num, stop_requests_file)
                logger.info("Action: stop #%d", issue_num)

        elif action_type == "start":
            issue_num = action.get("issue")
            if isinstance(issue_num, int):
                queue_to_inbox(
                    {"text": f"start #{issue_num}", "user_id": message.get("user_id")},
                    inbox_file,
                )
                logger.info("Action: start #%d (queued for orchestrator)", issue_num)


# Keep dispatch_command as a backwards-compatible alias for tests that use it.
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
    """Legacy dispatch using the old command parser.

    .. deprecated::
        Use :func:`dispatch_message` instead, which uses Claude API
        interpretation for natural-language understanding.
    """
    dispatch_message(
        message=message,
        bot_token=bot_token,
        chat_ids=chat_ids,
        state_file=state_file,
        status_file="tmp/orchestrator_status.json",
        agent_status_dir=agent_status_dir,
        stop_requests_file=stop_requests_file,
        inbox_file=inbox_file,
        anthropic_api_key=None,  # Falls back to simple acknowledgment.
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
    logger.info("Received signal %d --- shutting down.", signum)
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
        logger.info("Stop file detected (%s) --- shutting down.", stop_path)
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
    status_file: str,
    agent_status_dir: str,
    stop_requests_file: str,
    inbox_file: str,
    secret_id: str = "judgemind/telegram/bot",
    anthropic_secret_id: str = "judgemind/anthropic/api-key",
    no_llm: bool = False,
    rate_limit_calls: int = 20,
    rate_limit_window: float = 60.0,
) -> None:
    """Main daemon loop: poll SQS, interpret messages via Claude, repeat.

    Args:
        no_llm: If ``True``, disable Claude interpretation entirely.
            Messages are queued with a simple acknowledgment instead.
        rate_limit_calls: Max Claude API calls within the rate limit window.
        rate_limit_window: Rate limit window duration in seconds.
    """
    global _shutdown_requested  # noqa: PLW0603

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    write_pid_file(pid_file)

    # Load secrets at startup.
    bot_token, chat_ids = load_secret(secret_id=secret_id, region=region)

    if no_llm:
        anthropic_api_key = None
        logger.info("Claude interpreter disabled via --no-llm flag.")
    else:
        anthropic_api_key = load_anthropic_api_key(
            secret_id=anthropic_secret_id, region=region
        )

    if anthropic_api_key:
        logger.info("Claude interpreter enabled (Haiku).")
    elif not no_llm:
        logger.warning(
            "Claude interpreter disabled — will fall back to simple acknowledgment."
        )

    # Create a rate limiter for Claude API calls.
    limiter: RateLimiter | None = None
    if anthropic_api_key:
        limiter = RateLimiter(
            max_calls=rate_limit_calls,
            window_seconds=rate_limit_window,
        )
        logger.info(
            "Rate limiter: %d call(s) per %ds window.",
            rate_limit_calls,
            int(rate_limit_window),
        )

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
                    dispatch_message(
                        message=msg,
                        bot_token=bot_token,
                        chat_ids=chat_ids,
                        state_file=state_file,
                        status_file=status_file,
                        agent_status_dir=agent_status_dir,
                        stop_requests_file=stop_requests_file,
                        inbox_file=inbox_file,
                        anthropic_api_key=anthropic_api_key,
                        rate_limiter=limiter,
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
        "--status-file",
        default="tmp/orchestrator_status.json",
        help="Path to orchestrator status JSON file (written by OrchestratorBridge)",
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
    parser.add_argument(
        "--anthropic-secret-id",
        default="judgemind/anthropic/api-key",
        help="Secrets Manager secret ID for Anthropic API key",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help="Disable Claude interpretation entirely (messages get simple acknowledgments)",
    )
    parser.add_argument(
        "--rate-limit-calls",
        type=int,
        default=20,
        help="Max Claude API calls within the rate limit window (default: 20)",
    )
    parser.add_argument(
        "--rate-limit-window",
        type=float,
        default=60.0,
        help="Rate limit window duration in seconds (default: 60)",
    )
    args = parser.parse_args()

    run_daemon(
        pid_file=Path(args.pid_file),
        queue_url=args.queue_url,
        region=args.region,
        interval=args.interval,
        state_file=args.state_file,
        status_file=args.status_file,
        agent_status_dir=args.agent_status_dir,
        stop_requests_file=args.stop_requests_file,
        inbox_file=args.inbox_file,
        secret_id=args.secret_id,
        anthropic_secret_id=args.anthropic_secret_id,
        no_llm=args.no_llm,
        rate_limit_calls=args.rate_limit_calls,
        rate_limit_window=args.rate_limit_window,
    )


if __name__ == "__main__":
    main()
