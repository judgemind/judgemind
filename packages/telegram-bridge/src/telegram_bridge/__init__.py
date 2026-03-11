"""Judgemind Telegram bridge — Python client for agent notifications and commands."""

from .client import TelegramBridge
from .formatting import escape_html, linkify_github_refs
from .interpreter import (
    InterpretedMessage,
    RateLimiter,
    RateLimitError,
    build_orchestrator_status,
    interpret_message,
)
from .models import Message, SentMessage
from .orchestrator import (
    Command,
    CommandKind,
    InstructionKind,
    OrchestratorBridge,
    OrchestratorInstruction,
    create_orchestrator_bridge,
)
from .validation import ValidationResult, validate_github_links, validate_telegram_payload

__all__ = [
    "Command",
    "CommandKind",
    "InstructionKind",
    "InterpretedMessage",
    "Message",
    "OrchestratorBridge",
    "OrchestratorInstruction",
    "RateLimitError",
    "RateLimiter",
    "SentMessage",
    "TelegramBridge",
    "ValidationResult",
    "build_orchestrator_status",
    "create_orchestrator_bridge",
    "escape_html",
    "interpret_message",
    "linkify_github_refs",
    "validate_github_links",
    "validate_telegram_payload",
]
