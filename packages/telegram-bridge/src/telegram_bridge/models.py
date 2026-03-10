"""Data models for the Telegram bridge."""

from __future__ import annotations

import datetime
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    """An inbound message received from Telegram via SQS."""

    text: str
    user_id: int
    timestamp: datetime.datetime
