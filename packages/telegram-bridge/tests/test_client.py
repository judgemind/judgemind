"""Tests for the TelegramBridge client."""

from __future__ import annotations

import datetime
import json
import logging
import logging.handlers

import boto3
import httpx
import respx
from moto import mock_aws

from telegram_bridge import Message, SentMessage, TelegramBridge

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

    @respx.mock
    async def test_notify_uses_markdownv2_parse_mode(self) -> None:
        with mock_aws():
            _setup_secret()

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("Hello world.")
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert body["parse_mode"] == "MarkdownV2"

    @respx.mock
    async def test_notify_linkifies_issue_references(self) -> None:
        with mock_aws():
            _setup_secret()

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("Fixed #42 today.")
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            # Should contain a clickable link for #42
            assert "[\\#42](https://github.com/judgemind/judgemind/issues/42)" in body["text"]
            assert body["parse_mode"] == "MarkdownV2"

    @respx.mock
    async def test_notify_linkifies_pr_references(self) -> None:
        with mock_aws():
            _setup_secret()

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("PR #549 merged successfully.")
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            # Should contain a clickable link for PR #549
            assert "[PR \\#549](https://github.com/judgemind/judgemind/pull/549)" in body["text"]

    @respx.mock
    async def test_notify_escapes_special_chars_outside_links(self) -> None:
        with mock_aws():
            _setup_secret()

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("Issue #10 (done).")
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            # Parentheses around "done" should be escaped for MarkdownV2
            assert "\\(done\\)" in body["text"]
            # But the link URL parentheses should NOT be escaped
            assert "(https://github.com/" in body["text"]

    @respx.mock
    async def test_notify_disables_link_preview(self) -> None:
        with mock_aws():
            _setup_secret()

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("Check PR #42 for details.")
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert body["disable_web_page_preview"] is True

    @respx.mock
    async def test_notify_does_not_escape_exclamation_marks(self) -> None:
        with mock_aws():
            _setup_secret()

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("Links working!")
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            # The exclamation mark should appear unescaped in the sent text
            assert "\\!" not in body["text"]
            assert "working!" in body["text"]

    @respx.mock
    async def test_notify_respects_custom_repo(self) -> None:
        with mock_aws():
            _setup_secret()

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("#99 fixed.", repo="owner/other-repo")
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert "https://github.com/owner/other-repo/issues/99" in body["text"]


# ── status_update() ─────────────────────────────────────────────────────


class TestStatusUpdate:
    @respx.mock
    async def test_status_update_disables_link_preview(self) -> None:
        with mock_aws():
            _setup_secret()

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.status_update(task="#476", state="complete", details="PR #482 merged.")
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert body["disable_web_page_preview"] is True

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
    async def test_ask_disables_link_preview(self) -> None:
        with mock_aws():
            _setup_secret()
            queue_url = _setup_sqs()

            route = respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            )

            sqs = boto3.client("sqs", region_name="us-west-2")
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(
                    {
                        "callback_query_message_id": 1,
                        "callback_data": "Yes",
                        "user_id": 12345,
                    }
                ),
            )

            bridge = TelegramBridge(region_name="us-west-2", sqs_queue_url=queue_url)
            await bridge.ask("Continue?", options=["Yes", "No"], timeout=5.0)
            await bridge.close()

            body = json.loads(route.calls[0].request.content)
            assert body["disable_web_page_preview"] is True

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


# ── last_sent_messages() / debug inspection ─────────────────────────────


class TestDebugInspection:
    """Tests for the sent message debug/inspection feature."""

    @respx.mock
    async def test_sent_messages_stored_after_notify(self) -> None:
        with mock_aws():
            _setup_secret(user_ids=[111])

            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "message_id": 42,
                            "text": "Hello world.",
                            "entities": [{"type": "bold", "offset": 0, "length": 5}],
                        },
                    },
                )
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("Hello world.")
            await bridge.close()

            msgs = bridge.last_sent_messages()
            assert len(msgs) == 1
            assert isinstance(msgs[0], SentMessage)
            assert msgs[0].chat_id == 111
            assert msgs[0].message_id == 42
            assert msgs[0].rendered_text == "Hello world."
            assert msgs[0].entities == [{"type": "bold", "offset": 0, "length": 5}]
            assert msgs[0].parse_mode == "MarkdownV2"

    @respx.mock
    async def test_sent_messages_stored_for_multiple_chat_ids(self) -> None:
        with mock_aws():
            _setup_secret(user_ids=[111, 222])

            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {"message_id": 99, "text": "hi"},
                    },
                )
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("hi")
            await bridge.close()

            msgs = bridge.last_sent_messages(n=10)
            assert len(msgs) == 2
            chat_ids = {m.chat_id for m in msgs}
            assert chat_ids == {111, 222}

    @respx.mock
    async def test_last_sent_messages_returns_most_recent_first(self) -> None:
        with mock_aws():
            _setup_secret(user_ids=[111])

            call_count = 0

            def _response(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {"message_id": call_count, "text": f"msg-{call_count}"},
                    },
                )

            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                side_effect=_response
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("first")
            await bridge.notify("second")
            await bridge.notify("third")
            await bridge.close()

            msgs = bridge.last_sent_messages(n=2)
            assert len(msgs) == 2
            # Most recent first
            assert msgs[0].message_id == 3
            assert msgs[1].message_id == 2

    @respx.mock
    async def test_last_sent_messages_default_is_five(self) -> None:
        with mock_aws():
            _setup_secret(user_ids=[111])

            call_count = 0

            def _response(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {"message_id": call_count, "text": f"msg-{call_count}"},
                    },
                )

            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                side_effect=_response
            )

            bridge = TelegramBridge(region_name="us-west-2")
            for _ in range(8):
                await bridge.notify("msg")
            await bridge.close()

            msgs = bridge.last_sent_messages()
            assert len(msgs) == 5

    @respx.mock
    async def test_buffer_respects_max_size(self) -> None:
        with mock_aws():
            _setup_secret(user_ids=[111])

            call_count = 0

            def _response(request: httpx.Request) -> httpx.Response:
                nonlocal call_count
                call_count += 1
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {"message_id": call_count, "text": f"msg-{call_count}"},
                    },
                )

            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                side_effect=_response
            )

            bridge = TelegramBridge(region_name="us-west-2", debug_buffer_size=3)
            for _ in range(5):
                await bridge.notify("msg")
            await bridge.close()

            # Buffer size is 3, so only 3 messages retained
            msgs = bridge.last_sent_messages(n=10)
            assert len(msgs) == 3
            # Most recent should have highest message_id
            assert msgs[0].message_id == 5

    @respx.mock
    async def test_ask_stores_sent_message(self) -> None:
        with mock_aws():
            _setup_secret(user_ids=[111])
            queue_url = _setup_sqs()

            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {"message_id": 55, "text": "Rebase?"},
                    },
                )
            )

            sqs = boto3.client("sqs", region_name="us-west-2")
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(
                    {
                        "callback_query_message_id": 55,
                        "callback_data": "Yes",
                        "user_id": 111,
                    }
                ),
            )

            bridge = TelegramBridge(region_name="us-west-2", sqs_queue_url=queue_url)
            await bridge.ask("Rebase?", options=["Yes", "No"], timeout=5.0)
            await bridge.close()

            msgs = bridge.last_sent_messages()
            assert len(msgs) == 1
            assert msgs[0].message_id == 55
            assert msgs[0].chat_id == 111

    def test_last_sent_messages_empty_when_no_sends(self) -> None:
        bridge = TelegramBridge(region_name="us-west-2")
        assert bridge.last_sent_messages() == []

    @respx.mock
    async def test_raw_response_stored(self) -> None:
        with mock_aws():
            _setup_secret(user_ids=[111])

            full_response = {
                "ok": True,
                "result": {
                    "message_id": 10,
                    "text": "test",
                    "entities": [],
                    "date": 1234567890,
                },
            }

            respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                return_value=httpx.Response(200, json=full_response)
            )

            bridge = TelegramBridge(region_name="us-west-2")
            await bridge.notify("test")
            await bridge.close()

            msgs = bridge.last_sent_messages()
            assert msgs[0].raw_response == full_response

    @respx.mock
    async def test_debug_flag_set_from_env(self) -> None:
        """When DEBUG_TELEGRAM=1, the bridge sets _debug=True."""
        import os

        old = os.environ.get("DEBUG_TELEGRAM")
        os.environ["DEBUG_TELEGRAM"] = "1"
        try:
            with mock_aws():
                _setup_secret(user_ids=[111])

                respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                    return_value=httpx.Response(
                        200,
                        json={"ok": True, "result": {"message_id": 1, "text": "hi"}},
                    )
                )

                bridge = TelegramBridge(region_name="us-west-2")
                assert bridge._debug is True

                await bridge.notify("hi")
                await bridge.close()

                assert len(bridge.last_sent_messages()) == 1
        finally:
            if old is None:
                os.environ.pop("DEBUG_TELEGRAM", None)
            else:
                os.environ["DEBUG_TELEGRAM"] = old

    @respx.mock
    async def test_debug_logging_emits_log_records(self) -> None:
        """Verify that debug log records are actually emitted when DEBUG_TELEGRAM=1."""
        import os

        old = os.environ.get("DEBUG_TELEGRAM")
        os.environ["DEBUG_TELEGRAM"] = "1"
        try:
            with mock_aws():
                _setup_secret(user_ids=[111])

                respx.post("https://api.telegram.org/botfake-bot-token/sendMessage").mock(
                    return_value=httpx.Response(
                        200,
                        json={"ok": True, "result": {"message_id": 1, "text": "hi"}},
                    )
                )

                bridge = TelegramBridge(region_name="us-west-2")

                tg_logger = logging.getLogger("telegram_bridge.client")
                tg_logger.setLevel(logging.DEBUG)

                handler = logging.handlers.MemoryHandler(capacity=100)
                tg_logger.addHandler(handler)
                try:
                    await bridge.notify("hi")
                    handler.flush()
                    debug_records = [r for r in handler.buffer if "Telegram" in r.getMessage()]
                    assert len(debug_records) >= 2  # request + response
                finally:
                    tg_logger.removeHandler(handler)
                    await bridge.close()
        finally:
            if old is None:
                os.environ.pop("DEBUG_TELEGRAM", None)
            else:
                os.environ["DEBUG_TELEGRAM"] = old
