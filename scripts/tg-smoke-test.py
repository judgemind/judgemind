#!/usr/bin/env python3
"""Tier 2: Full end-to-end Telegram pipeline smoke test.

Sends a synthetic Telegram webhook payload to the Lambda endpoint, then polls
the Telegram Bot API for the bot's reply to verify delivery and formatting.

Exit codes:
    0 — All checks passed.
    1 — One or more checks failed.
    2 — Configuration error (missing env vars, secrets, etc.).

Environment variables:
    WEBHOOK_URL         — API Gateway endpoint for the Lambda webhook.
    TG_TEST_BOT_TOKEN   — Telegram bot token for the *test* bot (optional; falls
                          back to ``judgemind/telegram/test-bot`` in Secrets Manager).
    TG_TEST_USER_ID     — Telegram user ID of the test bot (numeric).
    TG_TEST_CHAT_ID     — Chat ID to send test messages to (usually same as user ID
                          for private chats).

Usage:
    scripts/tg-smoke-test.py                          # Uses Secrets Manager
    scripts/tg-smoke-test.py --webhook-url <URL>      # Override webhook URL
    scripts/tg-smoke-test.py --dry-run                # Validate config only
"""

from __future__ import annotations

from _venv_helper import ensure_venv

ensure_venv("telegram-bridge")

import argparse  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# How long to wait for the bot to reply (seconds).
_REPLY_TIMEOUT = 30
_POLL_INTERVAL = 2


def _get_config(args: argparse.Namespace) -> dict[str, str]:
    """Resolve configuration from args, env vars, and Secrets Manager."""
    config: dict[str, str] = {}

    # Webhook URL.
    config["webhook_url"] = args.webhook_url or os.environ.get("WEBHOOK_URL", "")
    if not config["webhook_url"]:
        logger.error("WEBHOOK_URL not set and --webhook-url not provided.")
        sys.exit(2)

    # Test bot token.
    token = os.environ.get("TG_TEST_BOT_TOKEN", "")
    if not token:
        try:
            import boto3

            sm = boto3.client("secretsmanager", region_name="us-west-2")
            resp = sm.get_secret_value(SecretId="judgemind/telegram/test-bot")
            secret = json.loads(resp["SecretString"])
            token = secret.get("bot_token", "")
        except Exception:
            logger.warning(
                "Could not fetch test bot token from Secrets Manager. "
                "Set TG_TEST_BOT_TOKEN env var instead."
            )
    if not token:
        logger.error(
            "Test bot token not available. Set TG_TEST_BOT_TOKEN or store in "
            "judgemind/telegram/test-bot secret."
        )
        sys.exit(2)
    config["bot_token"] = token

    # Test user/chat IDs.
    config["user_id"] = os.environ.get("TG_TEST_USER_ID", "")
    config["chat_id"] = os.environ.get("TG_TEST_CHAT_ID", config["user_id"])
    if not config["user_id"]:
        logger.error("TG_TEST_USER_ID not set.")
        sys.exit(2)

    return config


def _build_webhook_payload(
    *,
    user_id: int,
    chat_id: int,
    text: str,
) -> dict[str, Any]:
    """Build a synthetic Telegram webhook update payload."""
    return {
        "update_id": int(time.time()),
        "message": {
            "message_id": int(time.time()),
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "SmokeTest",
            },
            "chat": {
                "id": chat_id,
                "type": "private",
            },
            "date": int(time.time()),
            "text": text,
        },
    }


def _send_webhook(
    webhook_url: str,
    payload: dict[str, Any],
) -> bool:
    """POST the payload to the Lambda webhook endpoint.

    Returns True if the webhook accepted it (HTTP 200).
    """
    logger.info("Sending webhook payload to %s", webhook_url)
    try:
        resp = httpx.post(
            webhook_url,
            json=payload,
            timeout=10.0,
        )
        logger.info("Webhook response: %d %s", resp.status_code, resp.text[:200])
        return resp.status_code == 200
    except httpx.HTTPError as exc:
        logger.error("Webhook request failed: %s", exc)
        return False


def _poll_for_reply(
    bot_token: str,
    *,
    after_date: int,
    timeout: int = _REPLY_TIMEOUT,
) -> dict[str, Any] | None:
    """Poll the Telegram getUpdates API for a reply from the bot.

    Looks for messages sent after *after_date* (Unix timestamp).  Returns the
    first matching message dict, or None on timeout.
    """
    deadline = time.time() + timeout
    offset = 0

    while time.time() < deadline:
        try:
            resp = httpx.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": min(int(deadline - time.time()), 5),
                },
                timeout=10.0,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.warning("getUpdates failed: %s", data)
                time.sleep(_POLL_INTERVAL)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                # Look for a reply from the bot (not from the test user).
                if (
                    msg.get("from", {}).get("is_bot")
                    and msg.get("date", 0) >= after_date
                ):
                    return msg

        except httpx.HTTPError as exc:
            logger.warning("getUpdates request failed: %s", exc)

        time.sleep(_POLL_INTERVAL)

    return None


def _validate_reply(reply: dict[str, Any]) -> list[str]:
    """Validate the bot's reply message.

    Returns a list of error strings (empty = all checks passed).
    """
    errors: list[str] = []

    text = reply.get("text", "")
    if not text:
        errors.append("Reply has no text content.")
        return errors

    # Check that the reply is non-trivial.
    if len(text) < 5:
        errors.append(f"Reply is suspiciously short: {text!r}")

    # Check for Telegram entities (links, formatting).
    entities = reply.get("entities", [])

    logger.info("Reply text: %s", text[:300])
    logger.info("Entities: %s", entities)

    # If the reply mentions issue numbers, check they are linked.
    issue_refs = re.findall(r"#(\d+)", text)
    if issue_refs:
        text_links = [e for e in entities if e["type"] == "text_link"]
        if not text_links:
            errors.append(
                f"Reply mentions issues ({issue_refs}) but has no text_link entities. "
                "GitHub references should be clickable links."
            )

    return errors


def main() -> int:
    """Run the smoke test. Returns 0 on success, 1 on failure."""
    parser = argparse.ArgumentParser(
        description="Telegram pipeline end-to-end smoke test"
    )
    parser.add_argument(
        "--webhook-url",
        help="API Gateway webhook URL (overrides WEBHOOK_URL env var)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration only, do not send messages",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_REPLY_TIMEOUT,
        help=f"Seconds to wait for bot reply (default: {_REPLY_TIMEOUT})",
    )
    args = parser.parse_args()

    config = _get_config(args)
    logger.info("Configuration resolved successfully.")
    logger.info("  Webhook URL: %s", config["webhook_url"])
    logger.info("  User ID: %s", config["user_id"])
    logger.info("  Chat ID: %s", config["chat_id"])

    if args.dry_run:
        logger.info("Dry run — skipping message send.")
        return 0

    user_id = int(config["user_id"])
    chat_id = int(config["chat_id"])
    test_text = "status"

    # Build and send the webhook payload.
    payload = _build_webhook_payload(
        user_id=user_id,
        chat_id=chat_id,
        text=test_text,
    )
    before_send = int(time.time())

    if not _send_webhook(config["webhook_url"], payload):
        logger.error("FAIL: Webhook did not accept the payload.")
        return 1

    logger.info("Webhook accepted. Polling for bot reply...")

    # Poll for the reply.
    reply = _poll_for_reply(
        config["bot_token"],
        after_date=before_send,
        timeout=args.timeout,
    )

    if reply is None:
        logger.error("FAIL: No reply received within %d seconds.", args.timeout)
        return 1

    logger.info("Reply received from bot.")

    # Validate the reply.
    errors = _validate_reply(reply)
    if errors:
        for err in errors:
            logger.error("FAIL: %s", err)
        return 1

    logger.info("PASS: All smoke test checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
