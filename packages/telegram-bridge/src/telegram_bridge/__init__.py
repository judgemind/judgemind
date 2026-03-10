"""Judgemind Telegram bridge — Python client for agent notifications and commands."""

from .client import TelegramBridge
from .formatting import linkify_github_refs
from .models import Message
from .orchestrator import Command, CommandKind, OrchestratorBridge, create_orchestrator_bridge

__all__ = [
    "Command",
    "CommandKind",
    "Message",
    "OrchestratorBridge",
    "TelegramBridge",
    "create_orchestrator_bridge",
    "linkify_github_refs",
]
