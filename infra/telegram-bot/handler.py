"""Telegram webhook handler Lambda.

Receives POST requests from Telegram via API Gateway, validates the sender
against an allowlist in Secrets Manager, and enqueues valid messages to SQS
for async processing by the command handler.

Environment variables:
    SQS_QUEUE_URL: URL of the inbound SQS queue.
    TELEGRAM_SECRET_ID: ARN of the Secrets Manager secret containing the bot
        token and allowed user IDs.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Module-level cache — survives across Lambda invocations in the same container.
_cached_secret: dict[str, Any] | None = None


def _get_secret() -> dict[str, Any]:
    """Fetch and cache the Telegram bot secret from Secrets Manager.

    Returns a dict with keys ``bot_token`` (str) and ``allowed_user_ids``
    (list[int]).  The secret is fetched once per Lambda cold start and cached
    for the lifetime of the container.
    """
    global _cached_secret  # noqa: PLW0603
    if _cached_secret is not None:
        return _cached_secret

    secret_id = os.environ["TELEGRAM_SECRET_ID"]
    client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=secret_id)
    _cached_secret = json.loads(resp["SecretString"])
    return _cached_secret


def _is_user_allowed(user_id: int) -> bool:
    """Return True if *user_id* is in the configured allowlist."""
    secret = _get_secret()
    allowed = secret.get("allowed_user_ids", [])
    return user_id in allowed


def _enqueue_message(
    *,
    chat_id: int,
    user_id: int,
    message_type: str,
    payload: str,
) -> None:
    """Send a message to the inbound SQS queue."""
    sqs = boto3.client("sqs")
    queue_url = os.environ["SQS_QUEUE_URL"]
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=payload,
        MessageAttributes={
            "chat_id": {"DataType": "Number", "StringValue": str(chat_id)},
            "user_id": {"DataType": "Number", "StringValue": str(user_id)},
            "message_type": {"DataType": "String", "StringValue": message_type},
        },
    )


def _answer_callback_query(callback_query_id: str) -> None:
    """Answer a Telegram callback query to dismiss the loading indicator.

    Uses the Telegram Bot API directly via ``urllib`` (no extra deps needed).
    """
    import urllib.request

    secret = _get_secret()
    bot_token = secret["bot_token"]
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    data = json.dumps({"callback_query_id": callback_query_id}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310
    except Exception:
        # Best-effort — don't fail the webhook if this call fails.
        logger.exception("Failed to answer callback query %s", callback_query_id)


def _handle_message(update: dict[str, Any]) -> None:
    """Process a standard text message update."""
    message = update["message"]
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]

    if not _is_user_allowed(user_id):
        logger.info("Rejected message from unauthorized user %d", user_id)
        return

    text = message.get("text", "")
    _enqueue_message(
        chat_id=chat_id,
        user_id=user_id,
        message_type="text",
        payload=json.dumps({"text": text, "chat_id": chat_id, "user_id": user_id}),
    )
    logger.info("Enqueued text message from user %d in chat %d", user_id, chat_id)


def _handle_callback_query(update: dict[str, Any]) -> None:
    """Process an inline keyboard callback query update."""
    callback = update["callback_query"]
    user_id = callback["from"]["id"]
    chat_id = callback["message"]["chat"]["id"]

    if not _is_user_allowed(user_id):
        logger.info("Rejected callback from unauthorized user %d", user_id)
        return

    # Answer the callback to dismiss the Telegram loading indicator.
    _answer_callback_query(callback["id"])

    data = callback.get("data", "")
    _enqueue_message(
        chat_id=chat_id,
        user_id=user_id,
        message_type="callback",
        payload=json.dumps({"data": data, "chat_id": chat_id, "user_id": user_id}),
    )
    logger.info("Enqueued callback from user %d in chat %d", user_id, chat_id)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point.

    API Gateway v2 passes the raw Telegram JSON in ``event["body"]``.
    Always returns 200 to Telegram — retries on non-200 are undesirable.
    """
    ok_response: dict[str, Any] = {"statusCode": 200, "body": "ok"}

    try:
        body = event.get("body", "")
        if not body:
            logger.warning("Empty body received")
            return ok_response

        # API Gateway may base64-encode the body.
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode()

        update: dict[str, Any] = json.loads(body)

        if "callback_query" in update:
            _handle_callback_query(update)
        elif "message" in update:
            _handle_message(update)
        else:
            logger.info("Ignoring unsupported update type: %s", list(update.keys()))

    except Exception:
        # Catch everything — never return non-200 to Telegram.
        logger.exception("Error processing Telegram update")

    return ok_response
