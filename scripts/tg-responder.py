#!/usr/bin/env python3
# venv: telegram-bridge
"""Standalone Telegram responder daemon.

Polls the Telegram inbound SQS queue every few seconds and interprets all
messages as free text using a lightweight Claude API call (Haiku).  The
interpreter understands the current orchestrator state and can respond
naturally to any question, while also extracting actionable commands
(start, pause, resume, stop) for the orchestrator.

This daemon **replaces** ``scripts/tg-poll-daemon.py``, which only queued
messages without responding.

Usage::

    scripts/tg-responder.py [--interval 1] [--pid-file tmp/tg_responder.pid]

To stop the daemon gracefully::

    scripts/tg-stop-responder.sh

This sends SIGTERM to the daemon process, waits for graceful shutdown, and
cleans up PID/stop files. The daemon also checks for a ``tmp/tg_responder.stop``
file as a fallback shutdown mechanism.

If a daemon is already running, the script refuses to start (prints an
error).  Use ``--force`` to request the existing daemon to shut down (via
the stop file) and wait up to 10 seconds before starting a new one.

Environment:
    AWS credentials must be available (profile, env vars, or instance role).
    The bot token and chat IDs are read from Secrets Manager
    (``judgemind/telegram/bot``) at startup.
    The Anthropic API key must be available via the ``ANTHROPIC_API_KEY``
    environment variable or via Secrets Manager
    (``judgemind/anthropic/api-key``).
"""

from __future__ import annotations

import argparse
import datetime
from dataclasses import dataclass
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
    from botocore.config import Config as BotoConfig
except ModuleNotFoundError as exc:
    _pkg_dir = Path(__file__).resolve().parents[1] / "packages" / "telegram-bridge"
    print(
        f"ERROR: Missing dependency: {exc.name}\n"
        f"\n"
        f"The telegram-bridge venv exists but is missing required packages.\n"
        f"Install them with:\n"
        f"\n"
        f'    {_pkg_dir}/.venv/bin/pip install -e "{_pkg_dir}[dev]" --quiet\n',
        file=sys.stderr,
    )
    sys.exit(1)

# Add the packages dir to sys.path so we can import telegram_bridge.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_BRIDGE_SRC = _REPO_ROOT / "packages" / "telegram-bridge" / "src"
if str(_BRIDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_BRIDGE_SRC))

try:
    from telegram_bridge.formatting import linkify_github_refs, split_message  # noqa: E402
    from telegram_bridge.interpreter import (  # noqa: E402
        RateLimitError,
        RateLimiter,
        interpret_message,
        interpret_message_with_tools,
    )
except ModuleNotFoundError as exc:
    _pkg_dir2 = Path(__file__).resolve().parents[1] / "packages" / "telegram-bridge"
    print(
        f"ERROR: Missing dependency: {exc.name}\n"
        f"\n"
        f"The telegram-bridge package is not installed in the venv.\n"
        f"Install it with:\n"
        f"\n"
        f'    {_pkg_dir2}/.venv/bin/pip install -e "{_pkg_dir2}[dev]" --quiet\n',
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


# ── Webhook health check ───────────────────────────────────────────────


def check_webhook_health(
    *,
    bot_token: str,
    expected_url_substring: str = "execute-api",
) -> None:
    """Check the registered webhook URL and warn if it looks wrong.

    Calls Telegram's ``getWebhookInfo`` endpoint at startup and logs a
    warning if:
    - No webhook URL is registered
    - The registered URL does not end with ``/webhook``
    - The registered URL does not contain the expected substring
      (default: ``execute-api`` for AWS API Gateway)

    This is a best-effort check — failures are logged as warnings,
    never raised as exceptions, so the daemon always starts.
    """
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{TELEGRAM_API_BASE}/bot{bot_token}/getWebhookInfo")
            if resp.status_code != 200:
                logger.warning(
                    "Webhook health check: Telegram API returned %d", resp.status_code
                )
                return

            data = resp.json()
            result = data.get("result", {})
            url = result.get("url", "")

            if not url:
                logger.warning(
                    "Webhook health check: no webhook URL is registered. "
                    "Run scripts/tg-set-webhook.sh to set it."
                )
                return

            if not url.endswith("/webhook"):
                logger.warning(
                    "Webhook health check: registered URL does not end with /webhook: %s — "
                    "messages will be dropped (404). "
                    "Run scripts/tg-set-webhook.sh to fix.",
                    url,
                )
                return

            if expected_url_substring and expected_url_substring not in url:
                logger.warning(
                    "Webhook health check: registered URL does not contain '%s': %s — "
                    "this may indicate a misconfiguration.",
                    expected_url_substring,
                    url,
                )
                return

            # Check for pending errors reported by Telegram.
            last_error = result.get("last_error_message", "")
            if last_error:
                logger.warning(
                    "Webhook health check: Telegram reports an error: %s",
                    last_error,
                )
                return

            logger.info("Webhook health check: OK (%s)", url)
    except Exception:
        logger.warning("Webhook health check failed (non-fatal)", exc_info=True)


# ── Telegram replies ────────────────────────────────────────────────────


def send_telegram_reply(
    text: str,
    *,
    bot_token: str,
    chat_ids: list[int],
    parse_mode: str | None = None,
) -> None:
    """Send a message to all chat IDs via the Telegram Bot API.

    If *text* exceeds Telegram's 4096-character limit, it is split at
    paragraph boundaries and sent as multiple messages.

    Args:
        text: The message text to send.
        bot_token: Telegram bot token.
        chat_ids: List of chat IDs to send to.
        parse_mode: Optional Telegram parse mode (e.g. ``"HTML"``).
            When ``None``, the message is sent as plain text.

    If Telegram returns 400 (Bad Request) — typically caused by formatting
    issues — the message is retried as plain text so it is delivered rather
    than silently dropped.

    Errors are logged but not raised — the daemon should keep running even
    if a single reply fails.
    """
    # Split long messages at paragraph boundaries to avoid truncation.
    chunks = split_message(text)

    with httpx.Client(timeout=15.0) as client:
        for chat_id in chat_ids:
            for chunk in chunks:
                payload: dict[str, object] = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                }
                if parse_mode is not None:
                    payload["parse_mode"] = parse_mode
                try:
                    resp = client.post(
                        f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
                        json=payload,
                    )
                    # On 400 (bad formatting), retry without parse_mode as plain text.
                    if resp.status_code == 400 and parse_mode is not None:
                        logger.warning(
                            "Telegram returned 400 for chat %d — retrying as plain text.",
                            chat_id,
                        )
                        fallback_payload: dict[str, object] = {
                            "chat_id": chat_id,
                            "text": chunk,
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
                        "Failed to send Telegram message to chat %d",
                        chat_id,
                        exc_info=True,
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

    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
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

    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
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


# ── File download helper ───────────────────────────────────────────────


def download_telegram_file(
    *,
    file_id: str,
    bot_token: str,
    save_dir: str,
    message_id: int | None = None,
    default_ext: str = ".bin",
    filename_hint: str | None = None,
) -> str | None:
    """Download a file from Telegram and save it locally.

    Uses the Telegram Bot API ``getFile`` endpoint to resolve the file path,
    then downloads the file content.

    Args:
        file_id: Telegram file_id of the file to download.
        bot_token: Telegram bot token for API authentication.
        save_dir: Directory to save the downloaded file.
        message_id: Optional message ID for the filename.
        default_ext: Default file extension if none can be determined
            from the Telegram file path (default: ``.bin``).
        filename_hint: Optional original filename (e.g. from a document
            message).  When provided, the extension is taken from this
            filename if the Telegram file path has none.

    Returns:
        The absolute path to the saved file, or ``None`` if the download failed.
    """
    save_path = Path(save_dir)
    save_path.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.Client(timeout=30.0) as client:
            # Step 1: Get the file path from Telegram.
            get_file_resp = client.get(
                f"{TELEGRAM_API_BASE}/bot{bot_token}/getFile",
                params={"file_id": file_id},
            )
            if get_file_resp.status_code != 200:
                logger.warning(
                    "Telegram getFile returned %d for file_id %s",
                    get_file_resp.status_code,
                    file_id,
                )
                return None

            file_data = get_file_resp.json()
            file_path = file_data.get("result", {}).get("file_path", "")
            if not file_path:
                logger.warning("Telegram getFile returned no file_path for %s", file_id)
                return None

            # Step 2: Download the file content.
            download_url = f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}"
            download_resp = client.get(download_url)
            if download_resp.status_code != 200:
                logger.warning(
                    "Telegram file download returned %d for %s",
                    download_resp.status_code,
                    file_path,
                )
                return None

            # Determine file extension: prefer Telegram file_path, fall back
            # to filename_hint, then default_ext.
            ext = Path(file_path).suffix
            if not ext and filename_hint:
                ext = Path(filename_hint).suffix
            if not ext:
                ext = default_ext
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y%m%d_%H%M%S"
            )
            msg_suffix = f"_{message_id}" if message_id is not None else ""
            filename = f"{timestamp}{msg_suffix}{ext}"
            local_path = save_path / filename

            local_path.write_bytes(download_resp.content)
            logger.info(
                "Saved file to %s (%d bytes)", local_path, len(download_resp.content)
            )
            return str(local_path)

    except Exception:
        logger.warning("Failed to download file %s", file_id, exc_info=True)
        return None


# Keep as a backwards-compatible alias.
def download_telegram_photo(
    *,
    file_id: str,
    bot_token: str,
    save_dir: str,
    message_id: int | None = None,
) -> str | None:
    """Download a photo from Telegram and save it locally.

    Thin wrapper around :func:`download_telegram_file` with ``.jpg`` as
    the default extension.
    """
    return download_telegram_file(
        file_id=file_id,
        bot_token=bot_token,
        save_dir=save_dir,
        message_id=message_id,
        default_ext=".jpg",
    )


# ── Media message handlers ─────────────────────────────────────────────


def handle_photo_message(
    *,
    message: dict[str, object],
    bot_token: str,
    chat_ids: list[int],
    inbox_file: str,
    photos_dir: str = "tmp/tg_photos",
) -> None:
    """Handle an inbound photo message.

    Downloads the largest photo variant, saves it locally, creates an inbox
    entry for the orchestrator, and replies to the user confirming receipt.

    Args:
        message: The parsed SQS message body containing ``photo`` (list of
            PhotoSize dicts), optional ``caption``, ``message_id``, etc.
        bot_token: Telegram bot token for API calls.
        chat_ids: Chat IDs to send the confirmation reply to.
        inbox_file: Path to the inbox JSON file.
        photos_dir: Directory to save photos (default: ``tmp/tg_photos``).
    """
    photo_sizes = message.get("photo", [])
    if not isinstance(photo_sizes, list) or not photo_sizes:
        logger.warning("Photo message has no photo sizes — skipping.")
        return

    # Use the largest photo (last in the array per Telegram API convention).
    largest = photo_sizes[-1]
    file_id = largest.get("file_id", "")
    if not file_id:
        logger.warning("Photo message has no file_id — skipping.")
        return

    message_id = message.get("message_id")
    caption = str(message.get("caption", "") or message.get("text", "") or "")

    # Download the photo.
    local_path = download_telegram_photo(
        file_id=str(file_id),
        bot_token=bot_token,
        save_dir=photos_dir,
        message_id=int(message_id) if message_id is not None else None,
    )

    if local_path is None:
        send_telegram_reply(
            "Failed to download photo. Please try again.",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )
        return

    # Create inbox entry for the orchestrator.
    inbox_entry: dict[str, object] = {
        "action": "photo",
        "file_path": local_path,
        "reply_to": message_id,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if caption:
        inbox_entry["caption"] = caption

    queue_to_inbox(inbox_entry, inbox_file)

    # Reply to user confirming receipt.
    reply_text = "Photo received, forwarded to orchestrator."
    if caption:
        reply_text = f"Photo received (caption: {caption}), forwarded to orchestrator."
    send_telegram_reply(
        reply_text,
        bot_token=bot_token,
        chat_ids=chat_ids,
    )


def handle_document_message(
    *,
    message: dict[str, object],
    bot_token: str,
    chat_ids: list[int],
    inbox_file: str,
    documents_dir: str = "tmp/tg_documents",
) -> None:
    """Handle an inbound document message.

    Downloads the document, saves it locally, creates an inbox entry for
    the orchestrator, and replies to the user confirming receipt.

    Args:
        message: The parsed SQS message body containing ``document`` (dict
            with ``file_id``, ``file_name``, ``mime_type``, etc.), optional
            ``caption``, ``message_id``, etc.
        bot_token: Telegram bot token for API calls.
        chat_ids: Chat IDs to send the confirmation reply to.
        inbox_file: Path to the inbox JSON file.
        documents_dir: Directory to save documents (default: ``tmp/tg_documents``).
    """
    doc = message.get("document")
    if not isinstance(doc, dict) or not doc:
        logger.warning("Document message has no document metadata — skipping.")
        return

    file_id = doc.get("file_id", "")
    if not file_id:
        logger.warning("Document message has no file_id — skipping.")
        return

    message_id = message.get("message_id")
    caption = str(message.get("caption", "") or message.get("text", "") or "")
    file_name = str(doc.get("file_name", ""))

    # Download the document.
    local_path = download_telegram_file(
        file_id=str(file_id),
        bot_token=bot_token,
        save_dir=documents_dir,
        message_id=int(message_id) if message_id is not None else None,
        filename_hint=file_name or None,
    )

    if local_path is None:
        send_telegram_reply(
            "Failed to download document. Please try again.",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )
        return

    # Create inbox entry for the orchestrator.
    inbox_entry: dict[str, object] = {
        "action": "document",
        "file_path": local_path,
        "reply_to": message_id,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if file_name:
        inbox_entry["file_name"] = file_name
    if doc.get("mime_type"):
        inbox_entry["mime_type"] = doc["mime_type"]
    if caption:
        inbox_entry["caption"] = caption

    queue_to_inbox(inbox_entry, inbox_file)

    # Reply to user confirming receipt.
    name_part = f" ({file_name})" if file_name else ""
    reply_text = f"Document{name_part} received, forwarded to orchestrator."
    if caption:
        reply_text = (
            f"Document{name_part} received (caption: {caption}), "
            f"forwarded to orchestrator."
        )
    send_telegram_reply(
        reply_text,
        bot_token=bot_token,
        chat_ids=chat_ids,
    )


def handle_voice_message(
    *,
    message: dict[str, object],
    bot_token: str,
    chat_ids: list[int],
    inbox_file: str,
    voice_dir: str = "tmp/tg_voice",
) -> None:
    """Handle an inbound voice message.

    Downloads the voice note, saves it locally, creates an inbox entry for
    the orchestrator, and replies to the user confirming receipt.

    Args:
        message: The parsed SQS message body containing ``voice`` (dict
            with ``file_id``, ``duration``, ``mime_type``, etc.),
            ``message_id``, etc.
        bot_token: Telegram bot token for API calls.
        chat_ids: Chat IDs to send the confirmation reply to.
        inbox_file: Path to the inbox JSON file.
        voice_dir: Directory to save voice files (default: ``tmp/tg_voice``).
    """
    voice = message.get("voice")
    if not isinstance(voice, dict) or not voice:
        logger.warning("Voice message has no voice metadata — skipping.")
        return

    file_id = voice.get("file_id", "")
    if not file_id:
        logger.warning("Voice message has no file_id — skipping.")
        return

    message_id = message.get("message_id")
    duration = voice.get("duration")

    # Download the voice note.
    local_path = download_telegram_file(
        file_id=str(file_id),
        bot_token=bot_token,
        save_dir=voice_dir,
        message_id=int(message_id) if message_id is not None else None,
        default_ext=".ogg",
    )

    if local_path is None:
        send_telegram_reply(
            "Failed to download voice message. Please try again.",
            bot_token=bot_token,
            chat_ids=chat_ids,
        )
        return

    # Create inbox entry for the orchestrator.
    inbox_entry: dict[str, object] = {
        "action": "voice",
        "file_path": local_path,
        "reply_to": message_id,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if duration is not None:
        inbox_entry["duration"] = duration
    if voice.get("mime_type"):
        inbox_entry["mime_type"] = voice["mime_type"]

    queue_to_inbox(inbox_entry, inbox_file)

    # Reply to user confirming receipt.
    duration_part = f" ({duration}s)" if duration is not None else ""
    send_telegram_reply(
        f"Voice message{duration_part} received, forwarded to orchestrator.",
        bot_token=bot_token,
        chat_ids=chat_ids,
    )


# ── Dispatch ────────────────────────────────────────────────────────────


def _parse_iso_timestamp(ts: str) -> datetime.datetime | None:
    """Parse an ISO-8601 timestamp string, returning ``None`` on failure."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts)
        # Ensure timezone-aware (assume UTC if naive).
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _staleness_seconds(ts: str) -> float | None:
    """Return the number of seconds since *ts*, or ``None`` if unparseable."""
    dt = _parse_iso_timestamp(ts)
    if dt is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - dt).total_seconds()


# ── Proactive staleness alert ──────────────────────────────────────────

#: Default threshold (seconds) before sending a proactive stale alert.
#: Set to 60 minutes — agents routinely take 5–30+ minutes per task,
#: so shorter thresholds cause false-positive warnings.
STALE_ALERT_THRESHOLD_SECONDS: float = 3600.0  # 60 minutes


@dataclass
class StalenessTracker:
    """Tracks whether a proactive stale alert has been sent.

    The tracker ensures only one alert is sent per stale period.  When the
    orchestrator status becomes fresh again (``updated_at`` changes to a
    recent value), the tracker resets so a future stale period can trigger
    a new alert.

    Attributes:
        alert_sent: ``True`` if a stale alert has been sent for the
            current stale period.
        last_seen_updated_at: The ``updated_at`` value from the status
            file when the alert was sent.  Used to detect when the
            orchestrator has written a fresh update (which resets the
            tracker).
    """

    alert_sent: bool = False
    last_seen_updated_at: str = ""


def format_stale_alert(
    staleness_secs: float,
    orchestrator_status: dict[str, Any] | None,
) -> str:
    """Format a proactive stale-orchestrator alert message.

    Args:
        staleness_secs: How many seconds the status has been stale.
        orchestrator_status: The last-read orchestrator status dict,
            or ``None`` if the status file is missing.

    Returns:
        A plain-text alert message suitable for Telegram.
    """
    minutes = staleness_secs / 60.0
    lines: list[str] = [
        f"Warning: orchestrator status has not been updated for "
        f"{minutes:.0f} minute(s). This likely means the orchestrator "
        f"session has expired or crashed — normal agent work cycles "
        f"update the status much more frequently than this.",
    ]

    if orchestrator_status:
        # Include last known state.
        paused = orchestrator_status.get("paused", False)
        active_agents = orchestrator_status.get("active_agents", [])
        recently_completed = orchestrator_status.get("recently_completed", [])

        if paused:
            lines.append("Last known state: paused.")
        elif active_agents:
            agent_lines = []
            for agent in active_agents:
                issue = agent.get("issue_number", "?")
                phase = agent.get("phase", "?")
                title = agent.get("issue_title", "")
                agent_lines.append(f"  #{issue} ({phase}) - {title}")
            lines.append("Last known active agents:")
            lines.extend(agent_lines)
        else:
            lines.append("Last known state: no active agents.")

        # Show recent completions/failures for context.
        if recently_completed:
            last_few = recently_completed[-3:]
            completion_lines = []
            for entry in last_few:
                issue = entry.get("issue_number", "?")
                outcome = entry.get("outcome", "?")
                completion_lines.append(f"  #{issue} ({outcome})")
            lines.append("Recent tasks:")
            lines.extend(completion_lines)

        updated_at = orchestrator_status.get("updated_at", "unknown")
        lines.append(f"Last heartbeat: {updated_at}")

    lines.append("")
    lines.append("Suggestions:")
    lines.append("  - Check if the orchestrator session is still running")
    lines.append("  - Restart with /orchestrator if needed")

    return "\n".join(lines)


def check_orchestrator_staleness(
    *,
    status_file: str,
    tracker: StalenessTracker,
    threshold_seconds: float = STALE_ALERT_THRESHOLD_SECONDS,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Check whether the orchestrator status is stale and an alert should be sent.

    This function handles de-duplication: it only returns ``True`` the
    first time staleness is detected.  If the status file is updated
    (``updated_at`` changes), the tracker resets so a future stale period
    can trigger a new alert.

    Args:
        status_file: Path to the orchestrator status JSON file.
        tracker: Mutable :class:`StalenessTracker` that persists across
            poll cycles.
        threshold_seconds: Number of seconds after which the status is
            considered stale.

    Returns:
        A tuple of ``(should_alert, alert_text, orchestrator_status)``.
        ``should_alert`` is ``True`` only when an alert needs to be sent.
        ``alert_text`` is the formatted alert message (empty string if
        no alert).  ``orchestrator_status`` is the parsed status dict
        (or ``None`` if the file is missing/corrupt).
    """
    orchestrator_status = read_orchestrator_status(status_file)

    if orchestrator_status is None:
        # No status file — nothing to check.  Don't alert about a missing
        # file; the orchestrator may not have started yet.
        return False, "", None

    updated_at = str(orchestrator_status.get("updated_at", ""))

    # If the updated_at has changed since we last checked, the orchestrator
    # is alive — reset the tracker.
    if updated_at != tracker.last_seen_updated_at:
        tracker.alert_sent = False
        tracker.last_seen_updated_at = updated_at

    staleness = _staleness_seconds(updated_at)
    if staleness is None:
        return False, "", orchestrator_status

    if staleness <= threshold_seconds:
        # Status is fresh — no alert needed.
        return False, "", orchestrator_status

    # Status is stale.  Only alert if we haven't already.
    if tracker.alert_sent:
        return False, "", orchestrator_status

    tracker.alert_sent = True
    alert_text = format_stale_alert(staleness, orchestrator_status)
    return True, alert_text, orchestrator_status


def merge_agent_status_into_orchestrator(
    orchestrator_status: dict[str, Any],
    agent_statuses: list[dict[str, str]],
    *,
    staleness_threshold_seconds: float = 3600.0,
) -> dict[str, Any]:
    """Merge per-worker agent-status files into the orchestrator status.

    For each worker found in *agent_statuses*, if the agent-status file has
    a more recent ``updated`` timestamp than the corresponding entry in
    ``orchestrator_status["active_agents"]``, the agent-status data is
    preferred.  Workers only present in agent-status files are added.

    If the overall ``updated_at`` field on the orchestrator status is older
    than *staleness_threshold_seconds*, a ``staleness_warning`` key is added
    to the returned dict.

    The input *orchestrator_status* dict is **not** mutated; a shallow copy
    is returned.

    Args:
        orchestrator_status: The status dict read from
            ``orchestrator_status.json``.
        agent_statuses: List of dicts from :func:`read_agent_status_files`.
        staleness_threshold_seconds: Threshold (in seconds) above which
            a staleness warning is emitted.  Defaults to 3600 (60 minutes).

    Returns:
        A (possibly enriched) copy of *orchestrator_status*.
    """
    result = dict(orchestrator_status)
    active_agents: list[dict[str, Any]] = list(result.get("active_agents", []))

    # Build a lookup of existing agents by worker name/number for comparison.
    agent_by_worker: dict[str, dict[str, Any]] = {}
    for agent in active_agents:
        # orchestrator_status entries use "worker_number" as an int
        worker_key = f"worker-{agent.get('worker_number', '')}"
        agent_by_worker[worker_key] = agent

    for status in agent_statuses:
        worker_name = status.get("worker", "")
        if not worker_name:
            continue

        existing = agent_by_worker.get(worker_name)

        # Parse timestamps for comparison.
        status_ts = _parse_iso_timestamp(status.get("updated", ""))
        existing_ts = _parse_iso_timestamp(
            existing.get("updated", "") if existing else ""
        )

        # Prefer agent-status file if it is more recent or if no existing entry.
        if existing is None:
            # Only add if the worker is not in a terminal phase.
            phase = status.get("phase", "")
            if phase == "done":
                continue
            active_agents.append(
                {
                    "worker_number": worker_name.replace("worker-", ""),
                    "issue_number": status.get("issue", "?"),
                    "issue_title": status.get("summary", ""),
                    "phase": phase,
                    "updated": status.get("updated", ""),
                    "source": "agent-status-file",
                }
            )
        elif status_ts is not None and (existing_ts is None or status_ts > existing_ts):
            # Update existing entry with fresher data from agent-status file.
            existing["phase"] = status.get("phase", existing.get("phase", ""))
            existing["updated"] = status.get("updated", existing.get("updated", ""))
            if status.get("summary"):
                existing["summary"] = status["summary"]
            existing["source"] = "agent-status-file"

    result["active_agents"] = active_agents

    # Check overall staleness of orchestrator_status.json.
    overall_updated = result.get("updated_at", "")
    staleness = _staleness_seconds(overall_updated)
    if staleness is not None and staleness > staleness_threshold_seconds:
        minutes = staleness / 60.0
        result["staleness_warning"] = (
            f"Note: orchestrator status may be stale — "
            f"last updated {minutes:.0f} minute(s) ago. "
            f"Agent-status files have been merged for fresher per-worker data."
        )

    return result


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
    use_opus: bool = False,
    repo_root: Path | None = None,
) -> None:
    """Interpret and dispatch a single inbound message using Claude API.

    All messages are sent to the Claude interpreter for natural-language
    understanding.  The interpreter returns a reply and optional actions
    (start, pause, resume, stop) which are executed by the daemon.

    If the Anthropic API key is not available, falls back to a simple
    acknowledgment.

    Agent-status files (``worker-N.txt``) are always read and merged with
    the orchestrator status, preferring whichever source has the more
    recent timestamp per worker.  This ensures the interpreter always has
    the freshest possible view of each worker's state.

    Args:
        rate_limiter: Optional rate limiter passed through to
            :func:`interpret_message`.  When ``None``, no rate limiting
            is applied at the dispatch level (the interpreter's own
            default limiter still applies unless explicitly disabled).
        use_opus: If ``True``, use the Opus agent with tool access for
            message interpretation.  Requires *repo_root* to be set.
        repo_root: Absolute path to the repository root.  Required when
            *use_opus* is ``True`` so the agent can access files.
    """
    # Check for media messages — handle them separately.
    if message.get("photo"):
        handle_photo_message(
            message=message,
            bot_token=bot_token,
            chat_ids=chat_ids,
            inbox_file=inbox_file,
        )
        return

    if message.get("document"):
        handle_document_message(
            message=message,
            bot_token=bot_token,
            chat_ids=chat_ids,
            inbox_file=inbox_file,
        )
        return

    if message.get("voice"):
        handle_voice_message(
            message=message,
            bot_token=bot_token,
            chat_ids=chat_ids,
            inbox_file=inbox_file,
        )
        return

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

    # Always read agent-status files for per-worker freshness.
    agent_statuses = read_agent_status_files(agent_status_dir)

    # If no status file exists, build a basic context from agent status files
    # and orchestrator state.
    if orchestrator_status is None:
        state = read_orchestrator_state(state_file)
        orchestrator_status = {
            "active_agents": agent_statuses,
            "paused": state.get("paused", False),
            "workers": state.get("workers", {}),
        }
    else:
        # Merge agent-status data into orchestrator status, preferring
        # whichever source is more recent per worker.
        orchestrator_status = merge_agent_status_into_orchestrator(
            orchestrator_status, agent_statuses
        )

    # Call the Claude interpreter.
    try:
        if use_opus and repo_root is not None:
            result = interpret_message_with_tools(
                text=text,
                repo_root=repo_root,
                orchestrator_status=orchestrator_status,
                api_key=anthropic_api_key,
                rate_limiter=rate_limiter,
            )
        else:
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
    # and escape special characters for HTML.
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
        user_id = message.get("user_id")

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
                    {"text": f"start #{issue_num}", "user_id": user_id},
                    inbox_file,
                )
                logger.info("Action: start #%d (queued for orchestrator)", issue_num)

        elif action_type == "file_issue":
            queue_to_inbox(
                {
                    "action": "file_issue",
                    "description": action.get("description", ""),
                    "priority": action.get("priority", "p2"),
                    "labels": action.get("labels", []),
                    "reply_to": user_id,
                    "timestamp": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                },
                inbox_file,
            )
            logger.info("Action: file_issue (queued for orchestrator)")

        elif action_type == "discuss":
            queue_to_inbox(
                {
                    "action": "discuss",
                    "message": action.get("message", ""),
                    "reply_to": user_id,
                    "timestamp": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                },
                inbox_file,
            )
            logger.info("Action: discuss (queued for orchestrator)")

        elif action_type == "do":
            queue_to_inbox(
                {
                    "action": "do",
                    "instruction": action.get("instruction", ""),
                    "reply_to": user_id,
                    "timestamp": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                },
                inbox_file,
            )
            logger.info("Action: do (queued for orchestrator)")


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
    *,
    long_poll_seconds: int = 20,
) -> list[dict[str, object]]:
    """Receive and delete all pending messages from *queue_url*.

    The first ``receive_message`` call uses SQS long polling (blocks up to
    *long_poll_seconds*) to avoid empty short-poll API calls.  Subsequent
    calls to drain any remaining messages use ``WaitTimeSeconds=0``.

    Returns a list of parsed message bodies (dicts).
    """
    messages: list[dict[str, object]] = []
    first_call = True

    while True:
        wait_time = long_poll_seconds if first_call else 0
        first_call = False

        resp = sqs_client.receive_message(  # type: ignore[union-attr]
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=wait_time,
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


def is_process_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running.

    Uses ``os.kill(pid, 0)`` which checks process existence without
    sending a signal.  Returns ``False`` for stale PIDs or permission
    errors (the process belongs to a different user, which means it's
    not our daemon).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to another user — not our daemon.
        return False
    return True


def check_stale_pid(pid_file: Path, *, force: bool = False) -> None:
    """Check for an existing PID file and handle stale/active processes.

    If a PID file exists:
      - If the process is still alive and ``force`` is False, raise
        ``SystemExit`` with a clear error message.
      - If the process is still alive and ``force`` is True, write the
        stop file and wait up to 10 seconds for it to exit.  If it
        doesn't exit, raise ``SystemExit``.
      - If the process is dead (stale PID), clean up and proceed.

    Args:
        pid_file: Path to the PID file (e.g. ``tmp/tg_responder.pid``).
        force: If ``True``, attempt to stop the existing process via the
            stop file before starting.
    """
    if not pid_file.exists():
        return

    try:
        old_pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        # Corrupt or unreadable PID file — clean up and proceed.
        logger.warning("Corrupt PID file %s — removing.", pid_file)
        cleanup_daemon_files(pid_file)
        return

    if not is_process_alive(old_pid):
        logger.info("Stale PID file (pid %d is dead) — cleaning up.", old_pid)
        cleanup_daemon_files(pid_file)
        return

    # Process is alive.
    if not force:
        logger.error(
            "Another responder is already running (pid %d). "
            "Use --force to stop it and start a new one.",
            old_pid,
        )
        raise SystemExit(1)

    # Force mode: write stop file and wait.
    stop_path = pid_file.with_suffix(".stop")
    logger.info("Force mode: requesting shutdown of existing daemon (pid %d).", old_pid)
    stop_path.write_text("")

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not is_process_alive(old_pid):
            logger.info("Old daemon (pid %d) has exited.", old_pid)
            cleanup_daemon_files(pid_file)
            return
        time.sleep(0.5)

    logger.error(
        "Old daemon (pid %d) did not exit within 10 seconds. "
        "Cannot start — manual intervention required.",
        old_pid,
    )
    raise SystemExit(1)


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


def _should_stop(pid_file: Path) -> bool:
    """Return ``True`` if shutdown has been requested (signal or stop file).

    Checks both the global ``_shutdown_requested`` flag (set by signal
    handlers) and the stop file.  This function is designed to be called
    frequently — before and after every network call — so the daemon
    stays responsive to shutdown requests even during error retry loops.
    """
    global _shutdown_requested  # noqa: PLW0603
    if _shutdown_requested:
        return True
    if check_stop_file(pid_file):
        _shutdown_requested = True
        return True
    return False


# Botocore configuration with bounded timeouts so the daemon stays
# responsive to shutdown requests even when the network is unreliable.
# Default botocore retries can block for 30s+; these settings cap each
# SQS call to ~35s worst-case (2 attempts * (5s connect + 25s read) / 2).
# read_timeout must exceed WaitTimeSeconds (20s) + buffer for response
# transmission, otherwise every long-poll cycle times out before SQS
# can respond.
SQS_BOTO_CONFIG = BotoConfig(
    connect_timeout=5,
    read_timeout=25,
    retries={"max_attempts": 2, "mode": "standard"},
)


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
    model: str = "opus",
    rate_limit_calls: int = 20,
    rate_limit_window: float = 60.0,
    force: bool = False,
) -> None:
    """Main daemon loop: poll SQS, interpret messages via Claude, repeat.

    Args:
        no_llm: If ``True``, disable Claude interpretation entirely.
            Messages are queued with a simple acknowledgment instead.
        model: Which Claude model to use: ``"opus"`` for the full agent
            with tool access, ``"haiku"`` for lightweight classification.
        rate_limit_calls: Max Claude API calls within the rate limit window.
        rate_limit_window: Rate limit window duration in seconds.
        force: If ``True``, stop any existing daemon process before starting.
    """
    global _shutdown_requested  # noqa: PLW0603

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Check for stale or active PID file before starting.
    check_stale_pid(pid_file, force=force)

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

    use_opus = model == "opus"

    if anthropic_api_key:
        model_label = "Opus (agent with tools)" if use_opus else "Haiku (lightweight)"
        logger.info("Claude interpreter enabled (%s).", model_label)
    elif not no_llm:
        logger.warning(
            "Claude interpreter disabled — will fall back to simple acknowledgment."
        )

    # Check that the webhook URL is correctly registered with Telegram.
    check_webhook_health(bot_token=bot_token)

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

    # Use bounded timeouts so the daemon stays responsive during errors.
    sqs = boto3.client("sqs", region_name=region, config=SQS_BOTO_CONFIG)

    # Staleness tracker for proactive alerts (persists across poll cycles).
    staleness_tracker = StalenessTracker()

    try:
        while not _should_stop(pid_file):
            try:
                messages = poll_sqs(sqs, queue_url)

                # Check for stop between poll and dispatch — the poll
                # itself may have taken several seconds if retrying.
                if _should_stop(pid_file):
                    break

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
                        use_opus=use_opus,
                        repo_root=_REPO_ROOT,
                    )
                    # Check for stop between each message dispatch.
                    if _should_stop(pid_file):
                        break

                if messages and not _shutdown_requested:
                    logger.info("Processed %d message(s).", len(messages))

                # Proactive staleness check — runs every cycle regardless
                # of whether any messages were received.
                should_alert, alert_text, _ = check_orchestrator_staleness(
                    status_file=status_file,
                    tracker=staleness_tracker,
                )
                if should_alert:
                    logger.warning("Orchestrator status is stale — sending alert.")
                    send_telegram_reply(
                        alert_text,
                        bot_token=bot_token,
                        chat_ids=chat_ids,
                    )
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
        default=1.0,
        help="Polling interval in seconds between long-poll cycles (default: 1)",
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
        "--model",
        choices=["opus", "haiku"],
        default="opus",
        help=(
            "Which Claude model to use: 'opus' for full agent with tool access, "
            "'haiku' for lightweight classification (default: opus)"
        ),
    )
    parser.add_argument(
        "--rate-limit-calls",
        type=int,
        default=10,
        help="Max Claude API calls within the rate limit window (default: 10)",
    )
    parser.add_argument(
        "--rate-limit-window",
        type=float,
        default=60.0,
        help="Rate limit window duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Stop any existing daemon process before starting (waits up to 10s)",
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
        model=args.model,
        rate_limit_calls=args.rate_limit_calls,
        rate_limit_window=args.rate_limit_window,
        force=args.force,
    )


if __name__ == "__main__":
    main()
