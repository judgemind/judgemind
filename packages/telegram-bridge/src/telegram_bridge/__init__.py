"""Judgemind Telegram bridge — Python client for agent notifications and commands."""

from .client import TelegramBridge
from .formatting import escape_html, linkify_github_refs, split_message
from .interpreter import (
    InterpretedMessage,
    RateLimiter,
    RateLimitError,
    build_orchestrator_status,
    clear_client_cache,
    get_client,
    interpret_message,
)
from .models import Message, SentMessage
from .orchestrator import (
    Command,
    CommandKind,
    InstructionKind,
    OrchestratorBridge,
    OrchestratorInstruction,
    PendingReply,
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
    "PendingReply",
    "RateLimitError",
    "RateLimiter",
    "SentMessage",
    "TelegramBridge",
    "ValidationResult",
    "build_orchestrator_status",
    "clear_client_cache",
    "create_orchestrator_bridge",
    "get_client",
    "escape_html",
    "interpret_message",
    "linkify_github_refs",
    "split_message",
    "validate_github_links",
    "validate_telegram_payload",
]
