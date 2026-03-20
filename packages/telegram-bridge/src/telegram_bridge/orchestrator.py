"""Backward-compatibility shim -- re-exports from dispatcher.py.

All classes and functions have been renamed and moved to
``telegram_bridge.dispatcher``.  This module re-exports the old names
so that existing callers continue to work.

.. deprecated::
    Import from ``telegram_bridge.dispatcher`` instead.
"""

from .dispatcher import (
    ALLOWED_INSTRUCTION_ACTIONS,
    DEFAULT_REPLY_TIMEOUT_SECONDS,
    Command,
    CommandKind,
    InstructionKind,
    PendingReply,
    WorkerInfo,
    parse_command,
)
from .dispatcher import (
    DispatcherBridge as OrchestratorBridge,
)
from .dispatcher import (
    DispatcherInstruction as OrchestratorInstruction,
)
from .dispatcher import (
    create_dispatcher_bridge as create_orchestrator_bridge,
)

__all__ = [
    "ALLOWED_INSTRUCTION_ACTIONS",
    "Command",
    "CommandKind",
    "DEFAULT_REPLY_TIMEOUT_SECONDS",
    "InstructionKind",
    "OrchestratorBridge",
    "OrchestratorInstruction",
    "PendingReply",
    "WorkerInfo",
    "create_orchestrator_bridge",
    "parse_command",
]
