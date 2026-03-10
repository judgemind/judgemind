"""Tests for the TelegramBridge client."""

from __future__ import annotations

import datetime
import json

import boto3
import httpx
import respx
from moto import mock_aws

from telegram_bridge import Message, TelegramBridge

# ── Helpers ──────────────────────────────────────────────────────────────


def _setup_secret(
    *,
    token: str = "fake-bot-token",
    user_ids: list[int] | None = None,
) -> None:
    """Create the Secrets Manager secret used by TelegramBridge."""
    sm = boto3.client("secretsmanager", region_name="us-west-2")
    sm.create_secret(
        Name="judgemind/telegram/bot",
        SecretString=json.dumps(
            {
                "bot_token": token,
                "allowed_user_ids": [12345] if user_ids is None else user_ids,
            }
        ),
    )


def _setup_sqs(queue_name: str = "test-telegram-inbound") -> str:
    """Create an SQS queue and return its URL."""
    sqs = boto3.client("sqs", region_name="us-west-2")
    resp = sqs.create_queue(QueueName=queue_name)
    return resp["QueueUrl"]


# ── No-op mode ───────────────────────────────────────────────────────────


class TestNoOpMode:
    """When the secret is missing or empty, all methods should be silent no-ops."""

    async def test_missing_secret_disables_bridge(self) -> None:
        with mock_aws():
            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("hello")
            assert bridge._disabled is True

    async def test_empty_token_disables_bridge(self) -> None:
        with mock_aws():
            _setup_secret(token="")
            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("hello")
            assert bridge._disabled is True

    async def test_ask_returns_none_when_disabled(self) -> None:
        with mock_aws():
            bridge = TelegramBridge(region_name="us-west-2")
            result = await bridge.ask("question?", options=["Yes", "No"], timeout=0.1)
            assert result is None

    async def test_poll_returns_empty_when_disabled(self) -> None:
        with mock_aws():
            bridge = TelegramBridge(region_name="us-west-2")
            result = await bridge.poll()
            assert result == []


# ── notify() ─────────────────────────────────────────────────────────────


class TestNotify:
    @respx.mock
    async def test_notify_sends_message_to_all_users(self) -> None:
        with mock_aws():
            _setup_secret(user_ids=[111, 222])

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("Task done.")
            await bridge.close()

            assert route.call_count == 2
            bodies = [json.loads(c.request.content) for c in route.calls]
            sent_ids = {b["chat_id"] for b in bodies}
            assert sent_ids == {111, 222}


# ── status_update() ─────────────────────────────────────────────────────


class TestStatusUpdate:
    @respx.mock
    async def test_status_update_sends_formatted_card(self) -> None:
        with mock_aws():
            _setup_secret()

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.status_update(task="#476", state="complete", details="CI green.")
            await bridge.close()

            assert route.call_count == 1
            body = json.loads(route.calls[0].request.content)
            assert body["parse_mode"] == "MarkdownV2"
            assert "Issue" in body["text"]


# ── ask() ────────────────────────────────────────────────────────────────


class TestAsk:
    @respx.mock
    async def test_ask_sends_inline_keyboard(self) -> None:
        with mock_aws():
            _setup_secret()
            queue_url = _setup_sqs()

            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
            )

            sqs = boto3.client("sqs", region_name="us-west-2")
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(
                    {
                        "callback_query_message_id": 42,
                        "callback_data": "Yes, rebase",
                        "user_id": 12345,
                    }
                ),
            )

            bridge = TelegramBridge(region_name="us-west-2", sqs_queue_url=queue_url)
            result = await bridge.ask(
                "Rebase?",
                options=["Yes, rebase", "No"],
                timeout=5.0,
            )
            await bridge.close()

            assert result == "Yes, rebase"

    @respx.mock
    async def test_ask_returns_none_on_timeout(self) -> None:
        with mock_aws():
            _setup_secret()
            queue_url = _setup_sqs()

            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
            )

            bridge = TelegramBridge(region_name="us-west-2", sqs_queue_url=queue_url)
            result = await bridge.ask(
                "Question?",
                options=["A", "B"],
                timeout=0.1,
            )
            await bridge.close()

            assert result is None

    async def test_ask_returns_none_when_no_chat_ids(self) -> None:
        with mock_aws():
            _setup_secret(user_ids=[])

            bridge = TelegramBridge(region_name="us-west-2")
            result = await bridge.ask("Q?", options=["A"])
            assert result is None


# ── poll() ───────────────────────────────────────────────────────────────


class TestPoll:
    async def test_poll_reads_and_deletes_messages(self) -> None:
        with mock_aws():
            _setup_secret()
            queue_url = _setup_sqs()

            sqs = boto3.client("sqs", region_name="us-west-2")

            now = datetime.datetime.now(datetime.UTC)
            for i in range(3):
                sqs.send_message(
                    QueueUrl=queue_url,
                    MessageBody=json.dumps(
                        {
                            "text": f"msg-{i}",
                            "user_id": 12345,
                            "timestamp": now.isoformat(),
                        }
                    ),
                )

            bridge = TelegramBridge(region_name="us-west-2", sqs_queue_url=queue_url)
            messages = await bridge.poll()

            assert len(messages) == 3
            assert all(isinstance(m, Message) for m in messages)
            texts = {m.text for m in messages}
            assert texts == {"msg-0", "msg-1", "msg-2"}

            # Queue should now be empty.
            resp = sqs.receive_message(
                QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0
            )
            assert resp.get("Messages", []) == []

    async def test_poll_returns_empty_when_no_queue_url(self) -> None:
        with mock_aws():
            _setup_secret()
            bridge = TelegramBridge(region_name="us-west-2")
            result = await bridge.poll()
            assert result == []
