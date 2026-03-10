"""Telegram bridge client — the primary interface for agents."""

from __future__ import annotations

import asyncio
import collections
import datetime
import json
import logging
import os
import threading
from typing import TYPE_CHECKING, Any

import boto3
import httpx

from .formatting import DEFAULT_GITHUB_REPO, escape_mdv2, format_status_card, linkify_github_refs
from .models import Message, SentMessage

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"

# Maximum number of sent messages retained in the debug ring buffer.
_DEFAULT_DEBUG_BUFFER_SIZE = 50


class TelegramBridge:
    """Async client for sending Telegram notifications and polling for commands.

    On first use the client fetches the bot token and chat ID from AWS Secrets
    Manager (``judgemind/telegram/bot``).  If the secret is missing or the token
    is empty, all methods silently become no-ops so that the bridge is opt-in.

    Thread-safe: multiple agents in the same process can share one instance.

    **Debug / inspection support:**

    Every successful ``sendMessage`` call stores the Telegram API response in an
    internal ring buffer.  Use :meth:`last_sent_messages` to retrieve recent
    messages with their rendered text and entities.

    Set the ``DEBUG_TELEGRAM=1`` environment variable to enable verbose logging
    of raw request and response payloads.
    """

    def __init__(
        self,
        *,
        secret_id: str = "judgemind/telegram/bot",
        sqs_queue_url: str | None = None,
        region_name: str = "us-west-2",
        debug_buffer_size: int = _DEFAULT_DEBUG_BUFFER_SIZE,
    ) -> None:
        self._secret_id = secret_id
        self._sqs_queue_url = sqs_queue_url
        self._region_name = region_name

        # Lazily initialised on first use.
        self._bot_token: str | None = None
        self._chat_ids: list[int] = []
        self._initialised = False
        self._disabled = False
        self._init_lock = threading.Lock()

        # Reusable HTTP client for Telegram API calls.
        self._http: httpx.AsyncClient | None = None

        # Debug ring buffer for sent messages.
        self._sent_messages: collections.deque[SentMessage] = collections.deque(
            maxlen=debug_buffer_size
        )
        self._debug = os.environ.get("DEBUG_TELEGRAM", "") == "1"

    # ── Lazy initialisation ──────────────────────────────────────────────

    def _ensure_initialised(self) -> None:
        """Fetch secret on first call.  Sets ``_disabled`` on failure."""
        if self._initialised:
            return
        with self._init_lock:
            if self._initialised:
                return
            try:
                client = boto3.client("secretsmanager", region_name=self._region_name)
                resp = client.get_secret_value(SecretId=self._secret_id)
                secret = json.loads(resp["SecretString"])
                token = secret.get("bot_token", "")
                if not token:
                    logger.info("Telegram bot token is empty — bridge disabled (opt-in).")
                    self._disabled = True
                else:
                    self._bot_token = token
                    raw_ids = secret.get("allowed_user_ids", [])
                    self._chat_ids = [int(uid) for uid in raw_ids]
            except Exception:
                logger.warning(
                    "Failed to fetch Telegram secret — bridge disabled.",
                    exc_info=True,
                )
                self._disabled = True
            finally:
                self._initialised = True

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    # ── Public API ───────────────────────────────────────────────────────

    async def notify(self, text: str, *, repo: str = DEFAULT_GITHUB_REPO) -> None:
        """Send a plain-text notification to all allowed users.

        GitHub references (``#N``, ``PR #N``) are converted to clickable links.
        No-op if the bridge is disabled (missing or empty bot token).
        """
        self._ensure_initialised()
        if self._disabled:
            return
        formatted = linkify_github_refs(text, repo=repo)
        await self._send_to_all(formatted, parse_mode="MarkdownV2")

    async def status_update(
        self,
        *,
        task: str,
        state: str,
        details: str,
        repo: str = DEFAULT_GITHUB_REPO,
    ) -> None:
        """Send a formatted status card to all allowed users.

        No-op if the bridge is disabled.
        """
        self._ensure_initialised()
        if self._disabled:
            return
        card = format_status_card(task=task, state=state, details=details, repo=repo)
        await self._send_to_all(card, parse_mode="MarkdownV2")

    async def ask(
        self,
        question: str,
        *,
        options: list[str],
        timeout: float = 300.0,
    ) -> str | None:
        """Send *question* with inline keyboard buttons and wait for a tap.

        Returns the tapped option string, or ``None`` on timeout.  Only the
        first allowed user's response is accepted.  No-op (returns ``None``)
        if the bridge is disabled.
        """
        self._ensure_initialised()
        if self._disabled:
            return None

        # Build inline keyboard markup.
        keyboard = {"inline_keyboard": [[{"text": opt, "callback_data": opt}] for opt in options]}

        # Send the question to the first chat ID (interactive Q&A is 1:1).
        if not self._chat_ids:
            logger.warning("No chat IDs configured — cannot send ask().")
            return None

        chat_id = self._chat_ids[0]
        http = await self._get_http()
        escaped_question = escape_mdv2(question)
        payload = {
            "chat_id": chat_id,
            "text": escaped_question,
            "parse_mode": "MarkdownV2",
            "reply_markup": keyboard,
            "disable_web_page_preview": True,
        }
        if self._debug:
            logger.debug("Telegram ask() request payload: %s", json.dumps(payload))

        resp = await http.post(
            f"{_TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage",
            json=payload,
        )
        resp.raise_for_status()
        resp_data = resp.json()
        if self._debug:
            logger.debug("Telegram ask() response: %s", json.dumps(resp_data))

        self._record_sent_message(chat_id, escaped_question, "MarkdownV2", resp_data)
        message_id = resp_data.get("result", {}).get("message_id")

        # Poll SQS for a callback_query response matching this message.
        return await self._poll_for_answer(message_id, options, timeout)

    def last_sent_messages(self, n: int = 5) -> list[SentMessage]:
        """Return the last *n* sent messages, most recent first.

        Each :class:`SentMessage` includes the rendered text and entities as
        returned by the Telegram API, allowing the caller to inspect exactly
        what Telegram displayed.

        Args:
            n: Number of messages to return (default 5).

        Returns:
            A list of up to *n* :class:`SentMessage` objects.
        """
        # deque is ordered oldest-first; reverse for most-recent-first.
        items = list(self._sent_messages)
        items.reverse()
        return items[:n]

    async def poll(self) -> list[Message]:
        """Read and delete all pending inbound messages from SQS.

        Returns an empty list if the bridge is disabled or no queue is
        configured.
        """
        self._ensure_initialised()
        if self._disabled or not self._sqs_queue_url:
            return []

        sqs = boto3.client("sqs", region_name=self._region_name)
        messages: list[Message] = []

        # Drain the queue in batches of 10 (SQS max per receive).
        while True:
            resp = sqs.receive_message(
                QueueUrl=self._sqs_queue_url,
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
                    msg = Message(
                        text=body.get("text", ""),
                        user_id=int(body.get("user_id", 0)),
                        timestamp=datetime.datetime.fromisoformat(
                            body.get("timestamp", datetime.datetime.now(datetime.UTC).isoformat())
                        ),
                    )
                    messages.append(msg)
                except Exception:
                    logger.warning(
                        "Failed to parse SQS message: %s", raw.get("Body"), exc_info=True
                    )
                entries_to_delete.append(
                    {"Id": raw["MessageId"], "ReceiptHandle": raw["ReceiptHandle"]}
                )

            if entries_to_delete:
                sqs.delete_message_batch(
                    QueueUrl=self._sqs_queue_url,
                    Entries=entries_to_delete,
                )

        return messages

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    # ── Internals ────────────────────────────────────────────────────────

    async def _send_to_all(self, text: str, *, parse_mode: str) -> None:
        """Send *text* to every configured chat ID."""
        http = await self._get_http()
        for chat_id in self._chat_ids:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            try:
                if self._debug:
                    logger.debug("Telegram request payload: %s", json.dumps(payload))

                resp = await http.post(
                    f"{_TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage",
                    json=payload,
                )
                resp.raise_for_status()

                resp_data = resp.json()
                if self._debug:
                    logger.debug("Telegram response: %s", json.dumps(resp_data))

                self._record_sent_message(chat_id, text, parse_mode, resp_data)
            except Exception:
                logger.warning(
                    "Failed to send Telegram message to chat %s",
                    chat_id,
                    exc_info=True,
                )

    def _record_sent_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str,
        resp_data: dict[str, Any],
    ) -> None:
        """Store a :class:`SentMessage` from a Telegram API response."""
        result = resp_data.get("result", {})
        sent = SentMessage(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            message_id=result.get("message_id"),
            rendered_text=result.get("text"),
            entities=result.get("entities", []),
            raw_response=resp_data,
        )
        self._sent_messages.append(sent)

    async def _poll_for_answer(
        self,
        message_id: int | None,
        options: list[str],
        timeout: float,
    ) -> str | None:
        """Poll SQS for a callback_query matching *message_id*."""
        if not self._sqs_queue_url:
            logger.warning("No SQS queue URL configured — cannot poll for answer.")
            return None

        sqs = boto3.client("sqs", region_name=self._region_name)
        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            remaining = deadline - asyncio.get_event_loop().time()
            wait_seconds = min(int(remaining), 20)  # SQS long-poll max is 20s
            if wait_seconds <= 0:
                break

            resp = await asyncio.to_thread(
                sqs.receive_message,
                QueueUrl=self._sqs_queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=wait_seconds,
            )
            batch = resp.get("Messages", [])
            for raw in batch:
                try:
                    body = json.loads(raw["Body"])
                    # Match callback_query by message_id.
                    if body.get("callback_query_message_id") == message_id:
                        data = body.get("callback_data", "")
                        if data in options:
                            # Delete the matched message.
                            await asyncio.to_thread(
                                sqs.delete_message,
                                QueueUrl=self._sqs_queue_url,
                                ReceiptHandle=raw["ReceiptHandle"],
                            )
                            return data
                except Exception:
                    logger.warning("Failed to parse SQS callback message", exc_info=True)

        return None
