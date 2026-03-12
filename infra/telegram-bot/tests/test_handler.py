"""Tests for the Telegram webhook handler Lambda."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# Must be set before importing handler (which reads env vars at module level for boto3).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

ALLOWED_USER_ID = 123456
UNAUTHORIZED_USER_ID = 999999
CHAT_ID = 654321
SECRET_ID = "judgemind/telegram/bot"
QUEUE_NAME = "judgemind-telegram-inbound-dev"


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Clear the module-level secret cache between tests."""
    import handler

    handler._cached_secret = None


@pytest.fixture()
def aws_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Set up mocked AWS resources (Secrets Manager + SQS) and env vars."""
    with mock_aws():
        # Create the secret.
        sm = boto3.client("secretsmanager", region_name="us-west-2")
        sm.create_secret(
            Name=SECRET_ID,
            SecretString=json.dumps(
                {
                    "bot_token": "fake-bot-token",
                    "allowed_user_ids": [ALLOWED_USER_ID],
                }
            ),
        )

        # Create the SQS queue.
        sqs = boto3.client("sqs", region_name="us-west-2")
        queue = sqs.create_queue(QueueName=QUEUE_NAME)
        queue_url = queue["QueueUrl"]

        monkeypatch.setenv("TELEGRAM_SECRET_ID", SECRET_ID)
        monkeypatch.setenv("SQS_QUEUE_URL", queue_url)

        yield {"queue_url": queue_url}


def _make_event(body: dict[str, Any] | str) -> dict[str, Any]:
    """Build an API Gateway v2 event from a Telegram update body."""
    return {
        "body": json.dumps(body) if isinstance(body, dict) else body,
        "isBase64Encoded": False,
    }


def _get_sqs_messages(queue_url: str) -> list[dict[str, Any]]:
    """Receive all messages currently on the SQS queue."""
    sqs = boto3.client("sqs", region_name="us-west-2")
    resp = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=10,
        MessageAttributeNames=["All"],
    )
    return resp.get("Messages", [])


# ─── Valid message ───────────────────────────────────────────────────────────


class TestValidMessage:
    """A valid text message from an allowed user should be enqueued."""

    def test_enqueues_text_message(self, aws_env: dict[str, str]) -> None:
        import handler

        update = {
            "update_id": 1,
            "message": {
                "message_id": 42,
                "from": {"id": ALLOWED_USER_ID, "first_name": "Test"},
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "/status",
            },
        }
        result = handler.handler(_make_event(update), None)

        assert result["statusCode"] == 200

        messages = _get_sqs_messages(aws_env["queue_url"])
        assert len(messages) == 1

        body = json.loads(messages[0]["Body"])
        assert body["text"] == "/status"
        assert body["chat_id"] == CHAT_ID
        assert body["user_id"] == ALLOWED_USER_ID

        attrs = messages[0]["MessageAttributes"]
        assert attrs["chat_id"]["StringValue"] == str(CHAT_ID)
        assert attrs["user_id"]["StringValue"] == str(ALLOWED_USER_ID)
        assert attrs["message_type"]["StringValue"] == "text"

    def test_photo_with_caption(self, aws_env: dict[str, str]) -> None:
        """A photo message should enqueue photo metadata and caption."""
        import handler

        update = {
            "update_id": 2,
            "message": {
                "message_id": 43,
                "from": {"id": ALLOWED_USER_ID, "first_name": "Test"},
                "chat": {"id": CHAT_ID, "type": "private"},
                "photo": [{"file_id": "abc", "width": 100, "height": 100}],
                "caption": "start #99",
            },
        }
        result = handler.handler(_make_event(update), None)
        assert result["statusCode"] == 200

        messages = _get_sqs_messages(aws_env["queue_url"])
        assert len(messages) == 1
        body = json.loads(messages[0]["Body"])
        assert body["text"] == "start #99"
        assert body["chat_id"] == CHAT_ID
        assert body["user_id"] == ALLOWED_USER_ID
        # Photo metadata should be included.
        assert body["photo"] == [{"file_id": "abc", "width": 100, "height": 100}]
        assert body["message_id"] == 43
        # Message type attribute should be "photo".
        attrs = messages[0]["MessageAttributes"]
        assert attrs["message_type"]["StringValue"] == "photo"

    def test_text_takes_precedence_over_caption(self, aws_env: dict[str, str]) -> None:
        """When both text and caption exist, text should be used."""
        import handler

        update = {
            "update_id": 2,
            "message": {
                "message_id": 43,
                "from": {"id": ALLOWED_USER_ID, "first_name": "Test"},
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "/status",
                "caption": "this should be ignored",
            },
        }
        result = handler.handler(_make_event(update), None)
        assert result["statusCode"] == 200

        messages = _get_sqs_messages(aws_env["queue_url"])
        assert len(messages) == 1
        body = json.loads(messages[0]["Body"])
        assert body["text"] == "/status"

    def test_message_without_text_or_caption(self, aws_env: dict[str, str]) -> None:
        """A message with neither text nor caption should enqueue with empty text."""
        import handler

        update = {
            "update_id": 2,
            "message": {
                "message_id": 43,
                "from": {"id": ALLOWED_USER_ID, "first_name": "Test"},
                "chat": {"id": CHAT_ID, "type": "private"},
            },
        }
        result = handler.handler(_make_event(update), None)
        assert result["statusCode"] == 200

        messages = _get_sqs_messages(aws_env["queue_url"])
        assert len(messages) == 1
        body = json.loads(messages[0]["Body"])
        assert body["text"] == ""


# ─── Valid callback query ────────────────────────────────────────────────────


class TestValidCallback:
    """A callback query from an allowed user should be enqueued."""

    @patch("handler._answer_callback_query")
    def test_enqueues_callback(self, mock_answer: MagicMock, aws_env: dict[str, str]) -> None:
        import handler

        update = {
            "update_id": 3,
            "callback_query": {
                "id": "cb-123",
                "from": {"id": ALLOWED_USER_ID, "first_name": "Test"},
                "message": {
                    "message_id": 44,
                    "chat": {"id": CHAT_ID, "type": "private"},
                },
                "data": "approve_42",
            },
        }
        result = handler.handler(_make_event(update), None)

        assert result["statusCode"] == 200

        # The callback query was answered.
        mock_answer.assert_called_once_with("cb-123")

        messages = _get_sqs_messages(aws_env["queue_url"])
        assert len(messages) == 1

        body = json.loads(messages[0]["Body"])
        assert body["data"] == "approve_42"
        assert body["chat_id"] == CHAT_ID

        attrs = messages[0]["MessageAttributes"]
        assert attrs["message_type"]["StringValue"] == "callback"


# ─── Unauthorized user ───────────────────────────────────────────────────────


class TestUnauthorizedUser:
    """Messages from unauthorized users should be silently rejected."""

    def test_message_not_enqueued(self, aws_env: dict[str, str]) -> None:
        import handler

        update = {
            "update_id": 4,
            "message": {
                "message_id": 50,
                "from": {"id": UNAUTHORIZED_USER_ID, "first_name": "Hacker"},
                "chat": {"id": 111, "type": "private"},
                "text": "hello",
            },
        }
        result = handler.handler(_make_event(update), None)

        assert result["statusCode"] == 200
        messages = _get_sqs_messages(aws_env["queue_url"])
        assert len(messages) == 0

    @patch("handler._answer_callback_query")
    def test_callback_not_enqueued(self, mock_answer: MagicMock, aws_env: dict[str, str]) -> None:
        import handler

        update = {
            "update_id": 5,
            "callback_query": {
                "id": "cb-999",
                "from": {"id": UNAUTHORIZED_USER_ID, "first_name": "Hacker"},
                "message": {
                    "message_id": 51,
                    "chat": {"id": 111, "type": "private"},
                },
                "data": "sneaky",
            },
        }
        result = handler.handler(_make_event(update), None)

        assert result["statusCode"] == 200
        mock_answer.assert_not_called()
        messages = _get_sqs_messages(aws_env["queue_url"])
        assert len(messages) == 0


# ─── Malformed payloads ──────────────────────────────────────────────────────


class TestMalformedPayloads:
    """Malformed payloads must return 200 without crashing."""

    def test_empty_body(self, aws_env: dict[str, str]) -> None:
        import handler

        result = handler.handler({"body": "", "isBase64Encoded": False}, None)
        assert result["statusCode"] == 200

    def test_no_body_key(self, aws_env: dict[str, str]) -> None:
        import handler

        result = handler.handler({}, None)
        assert result["statusCode"] == 200

    def test_invalid_json(self, aws_env: dict[str, str]) -> None:
        import handler

        result = handler.handler({"body": "not json{{{", "isBase64Encoded": False}, None)
        assert result["statusCode"] == 200

    def test_unsupported_update_type(self, aws_env: dict[str, str]) -> None:
        import handler

        update = {"update_id": 99, "edited_message": {"text": "edited"}}
        result = handler.handler(_make_event(update), None)
        assert result["statusCode"] == 200

        messages = _get_sqs_messages(aws_env["queue_url"])
        assert len(messages) == 0

    def test_base64_encoded_body(self, aws_env: dict[str, str]) -> None:
        """API Gateway may send base64-encoded bodies."""
        import base64

        import handler

        update = {
            "update_id": 10,
            "message": {
                "message_id": 60,
                "from": {"id": ALLOWED_USER_ID, "first_name": "Test"},
                "chat": {"id": CHAT_ID, "type": "private"},
                "text": "/help",
            },
        }
        encoded = base64.b64encode(json.dumps(update).encode()).decode()
        result = handler.handler({"body": encoded, "isBase64Encoded": True}, None)

        assert result["statusCode"] == 200
        messages = _get_sqs_messages(aws_env["queue_url"])
        assert len(messages) == 1
        body = json.loads(messages[0]["Body"])
        assert body["text"] == "/help"
