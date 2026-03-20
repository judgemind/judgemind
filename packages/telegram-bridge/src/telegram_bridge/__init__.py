"""Judgemind Telegram bridge -- Python client for agent notifications and commands."""

from .client import TelegramBridge
from .dispatcher import (
    Command,
    CommandKind,
    DispatcherBridge,
    DispatcherInstruction,
    InstructionKind,
    OrchestratorBridge,
    OrchestratorInstruction,
    PendingReply,
    create_dispatcher_bridge,
    create_orchestrator_bridge,
)
from .formatting import escape_html, linkify_github_refs, split_message
from .interpreter import (
    InterpretedMessage,
    RateLimiter,
    RateLimitError,
    build_dispatcher_status,
    build_orchestrator_status,
    clear_client_cache,
    get_client,
    interpret_message,
)
from .models import Message, SentMessage
from .validation import ValidationResult, validate_github_links, validate_telegram_payload

__all__ = [
    "Command",
    "CommandKind",
    "DispatcherBridge",
    "DispatcherInstruction",
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
    "build_dispatcher_status",
    "build_orchestrator_status",
    "clear_client_cache",
    "create_dispatcher_bridge",
    "create_orchestrator_bridge",
    "get_client",
    "escape_html",
    "interpret_message",
    "linkify_github_refs",
    "split_message",
    "validate_github_links",
    "validate_telegram_payload",
]
