"""Data models for the Telegram bridge."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Message:
    """An inbound message received from Telegram via SQS."""

    text: str
    user_id: int
    timestamp: datetime.datetime


@dataclass(frozen=True)
class SentMessage:
    """Record of an outgoing message sent via the Telegram Bot API.

    Stores both the request payload and the API response so that the
    orchestrator can inspect what Telegram actually rendered (including
    parsed entities).
    """

    chat_id: int
    text: str
    parse_mode: str
    message_id: int | None = None
    rendered_text: str | None = None
    entities: list[dict[str, Any]] = field(default_factory=list)
    timestamp: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    raw_response: dict[str, Any] = field(default_factory=dict)
