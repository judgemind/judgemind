"""Dispatcher integration — lifecycle notifications and inbound command dispatch.

This module provides a high-level API that the Claude Code interactive session
(the dispatcher) uses to send notifications at task lifecycle events and to
poll for inbound commands from Telegram.

All functions are no-ops when Telegram is not configured.
"""

from __future__ import annotations

import datetime
import fcntl
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .client import TelegramBridge
from .formatting import DEFAULT_GITHUB_REPO
from .interpreter import ALLOWED_PRIORITIES, build_dispatcher_status

logger = logging.getLogger(__name__)


# ── Command parsing ────────────────────────────────────────────────────────


class CommandKind(Enum):
    """Recognised inbound command types."""

    STATUS = "status"
    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    FILE_ISSUE = "file_issue"
    DISCUSS = "discuss"
    DO = "do"
    FREE_TEXT = "free_text"


@dataclass(frozen=True)
class Command:
    """A parsed inbound command from Telegram."""

    kind: CommandKind
    issue_number: int | None = None
    raw_text: str = ""
    # Extended metadata for complex command types.
    description: str = ""
    priority: str = ""
    labels: tuple[str, ...] = ()
    message: str = ""
    instruction: str = ""
    reply_to: int | None = None


def parse_command(text: str) -> Command:
    """Parse a raw message string into a :class:`Command`.

    Recognised patterns (case-insensitive):
    - ``status`` -> STATUS
    - ``start #N`` or ``start N`` -> START with issue_number
    - ``stop #N`` or ``stop N`` -> STOP with issue_number
    - ``pause`` -> PAUSE
    - ``resume`` -> RESUME
    - Anything else -> FREE_TEXT
    """
    stripped = text.strip()
    lower = stripped.lower()

    if lower == "status":
        return Command(kind=CommandKind.STATUS, raw_text=stripped)

    if lower == "pause":
        return Command(kind=CommandKind.PAUSE, raw_text=stripped)

    if lower == "resume":
        return Command(kind=CommandKind.RESUME, raw_text=stripped)

    # start #N / start N
    if lower.startswith("start "):
        issue_num = _extract_issue_number(stripped[6:])
        if issue_num is not None:
            return Command(kind=CommandKind.START, issue_number=issue_num, raw_text=stripped)

    # stop #N / stop N
    if lower.startswith("stop "):
        issue_num = _extract_issue_number(stripped[5:])
        if issue_num is not None:
            return Command(kind=CommandKind.STOP, issue_number=issue_num, raw_text=stripped)

    return Command(kind=CommandKind.FREE_TEXT, raw_text=stripped)


# ── Dispatcher instruction types ────────────────────────────────────────


class InstructionKind(Enum):
    """Recognised subagent -> dispatcher instruction types."""

    RESTART_RESPONDER = "restart_responder"
    TERRAFORM_APPLY = "terraform_apply"
    NOTIFY = "notify"
    RUN_SCRIPT = "run_script"
    FILE_ISSUE = "file_issue"


#: The set of allowed action strings for validation.
ALLOWED_INSTRUCTION_ACTIONS: frozenset[str] = frozenset(kind.value for kind in InstructionKind)


@dataclass(frozen=True)
class DispatcherInstruction:
    """A parsed instruction from a subagent to the dispatcher.

    Subagents write these to ``tmp/dispatcher_inbox.json`` via
    ``scripts/dispatcher-request.py``.  The dispatcher reads them
    with :meth:`DispatcherBridge.read_dispatcher_inbox`.
    """

    kind: InstructionKind
    from_issue: int | None = None
    reason: str = ""
    message: str = ""
    module: str = ""
    script: str = ""
    args: tuple[str, ...] = ()
    description: str = ""
    priority: str = ""
    labels: tuple[str, ...] = ()
    title: str = ""
    timestamp: str = ""


def _parse_instruction(entry: dict[str, Any]) -> DispatcherInstruction | None:
    """Parse a raw JSON dict into a :class:`DispatcherInstruction`.

    Returns ``None`` if the entry has an unrecognised or missing action.
    """
    action = entry.get("action", "")
    if action not in ALLOWED_INSTRUCTION_ACTIONS:
        logger.warning("Unknown dispatcher instruction action: %s -- skipping.", action)
        return None

    kind = InstructionKind(action)
    from_issue = entry.get("from_issue")
    if not isinstance(from_issue, int):
        from_issue = None

    return DispatcherInstruction(
        kind=kind,
        from_issue=from_issue,
        reason=str(entry.get("reason", "")),
        message=str(entry.get("message", "")),
        module=str(entry.get("module", "")),
        script=str(entry.get("script", "")),
        args=tuple(str(a) for a in entry.get("args", [])),
        description=str(entry.get("description", "")),
        priority=str(entry.get("priority", "")),
        labels=tuple(str(lbl) for lbl in entry.get("labels", [])),
        title=str(entry.get("title", "")),
        timestamp=str(entry.get("timestamp", "")),
    )


def _extract_issue_number(fragment: str) -> int | None:
    """Extract an issue number from ``#N`` or ``N``."""
    fragment = fragment.strip().lstrip("#")
    try:
        return int(fragment)
    except ValueError:
        return None


def _worker_label(worker: int | str) -> str:
    """Return a human-readable label for a worker identifier.

    Integer worker numbers produce ``Worker-3``.
    String agent IDs like ``agent-ab4722a2`` produce ``Agent-ab47``
    (truncated to the first 4 hex chars for readability).
    """
    if isinstance(worker, int):
        return f"Worker-{worker}"
    s = str(worker)
    if s.startswith("agent-") and len(s) > 10:
        return f"Agent-{s[6:10]}"
    return s


def _parse_inbox_entry(entry: dict[str, Any]) -> Command:
    """Parse a structured inbox entry written by the responder daemon.

    The responder writes entries with an ``action`` key that maps to one of
    the extended command types (``file_issue``, ``discuss``, ``do``, ``start``).
    """
    action = entry.get("action", "")
    reply_to = entry.get("reply_to")
    if isinstance(reply_to, int):
        pass
    else:
        reply_to = None

    if action == "file_issue":
        priority = str(entry.get("priority", "p2"))
        if priority not in ALLOWED_PRIORITIES:
            logger.warning(
                "Normalizing invalid priority %r to 'p2' in inbox file_issue entry",
                priority,
            )
            priority = "p2"
        return Command(
            kind=CommandKind.FILE_ISSUE,
            description=str(entry.get("description", "")),
            priority=priority,
            labels=tuple(entry.get("labels", ())),
            reply_to=reply_to,
            raw_text=str(entry.get("description", "")),
        )
    if action == "discuss":
        return Command(
            kind=CommandKind.DISCUSS,
            message=str(entry.get("message", "")),
            reply_to=reply_to,
            raw_text=str(entry.get("message", "")),
        )
    if action == "do":
        return Command(
            kind=CommandKind.DO,
            instruction=str(entry.get("instruction", "")),
            reply_to=reply_to,
            raw_text=str(entry.get("instruction", "")),
        )
    if action == "start":
        issue_num = entry.get("issue")
        if isinstance(issue_num, int):
            return Command(
                kind=CommandKind.START,
                issue_number=issue_num,
                reply_to=reply_to,
                raw_text=f"start #{issue_num}",
            )

    # Fall back to text-based parsing for unrecognised actions.
    text = str(entry.get("text", entry.get("instruction", "")))
    return parse_command(text)


# ── Pending reply tracking ──────────────────────────────────────────────────

#: Default timeout in seconds for pending replies before a fallback is sent.
DEFAULT_REPLY_TIMEOUT_SECONDS: float = 60.0


@dataclass
class PendingReply:
    """Tracks an outbound reply that the dispatcher owes the user.

    When the dispatcher picks up a ``discuss``, ``do``, or ``free_text``
    command, a :class:`PendingReply` is created.  The dispatcher is expected
    to call :meth:`DispatcherBridge.resolve_pending_reply` (or use the
    ``command_id`` parameter on :meth:`DispatcherBridge.reply`) when it has
    finished processing.  If no reply arrives within *timeout_seconds*, the
    bridge sends a fallback message.
    """

    command_id: str
    kind: str
    raw_text: str
    reply_to: int | None = None
    created_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    fallback_sent: bool = False


# ── Worker state (used for status replies) ─────────────────────────────────


@dataclass
class WorkerInfo:
    """Snapshot of a running worker for status reporting."""

    worker_number: int | str
    issue_number: int
    issue_title: str
    phase: str
    updated: str = ""


# ── Dispatcher class ────────────────────────────────────────────────────


@dataclass
class DispatcherBridge:
    """High-level bridge between the dispatcher and Telegram.

    This wraps :class:`TelegramBridge` and adds:

    * **Lifecycle notifications** -- ``session_started``, ``task_started``,
      ``task_completed``, ``task_failed``, ``session_ended``.
    * **File-based inbox** -- ``read_inbox`` reads commands from the file-based
      inbox written by the responder daemon (``scripts/tg-responder.py``).
    * **Status reply** -- ``reply_status`` sends a formatted summary of
      active workers back to Telegram.
    * **Pause/resume** -- ``paused`` flag that the dispatcher can check
      before spawning new work.

    All methods are no-ops if the underlying bridge is disabled.
    """

    bridge: TelegramBridge
    repo: str = DEFAULT_GITHUB_REPO
    paused: bool = False
    state_file: str | None = None
    status_file: str | None = None
    inbox_path: str | None = None
    stop_requests_path: str | None = None
    dispatcher_inbox_path: str | None = None
    prs_since_last_audit: int = 0
    session_number: int = 0
    _workers: dict[int | str, WorkerInfo] = field(default_factory=dict)
    _stopped_issues: set[int] = field(default_factory=set)
    _recently_completed: list[dict[str, Any]] = field(default_factory=list)
    _pending_replies: dict[str, PendingReply] = field(default_factory=dict)
    reply_timeout_seconds: float = DEFAULT_REPLY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Load persisted state from *state_file* if it exists."""
        if self.state_file:
            self._load_state()

    # ── State persistence ────────────────────────────────────────────────

    def _save_state(self) -> None:
        """Persist worker state and paused flag to *state_file*."""
        if not self.state_file:
            return
        data = {
            "paused": self.paused,
            "prs_since_last_audit": self.prs_since_last_audit,
            "session_number": self.session_number,
            "workers": {
                str(k): {
                    "worker_number": v.worker_number,
                    "issue_number": v.issue_number,
                    "issue_title": v.issue_title,
                    "phase": v.phase,
                    "updated": v.updated,
                }
                for k, v in self._workers.items()
            },
        }
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    def _load_state(self) -> None:
        """Load worker state and paused flag from *state_file*."""
        if not self.state_file:
            return
        path = Path(self.state_file)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            self.paused = data.get("paused", False)
            self.prs_since_last_audit = data.get("prs_since_last_audit", 0)
            self.session_number = data.get("session_number", 0)
            self._workers = {}
            for _key, w in data.get("workers", {}).items():
                info = WorkerInfo(
                    worker_number=w["worker_number"],
                    issue_number=w["issue_number"],
                    issue_title=w["issue_title"],
                    phase=w["phase"],
                    updated=w.get("updated", ""),
                )
                self._workers[info.worker_number] = info
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Failed to load state file %s -- starting fresh.", self.state_file)

    def _clear_state_file(self) -> None:
        """Remove the *state_file* on session end."""
        if not self.state_file:
            return
        path = Path(self.state_file)
        if path.exists():
            path.unlink()

    def write_status(
        self,
        *,
        open_prs: list[dict[str, Any]] | None = None,
        queue: list[dict[str, Any]] | None = None,
    ) -> None:
        """Write the dispatcher status JSON for the responder daemon.

        This writes ``dispatcher_status.json`` (at *status_file*) containing
        the full dispatcher state -- active agents, open PRs, recently
        completed tasks, queue, and paused/stopped state.  The responder
        daemon reads this file to provide context to the Claude interpreter.

        Call this after every state change (task start/complete/fail, pause,
        resume, etc.).

        Args:
            open_prs: List of open PR dicts (number, CI status, mergeable).
            queue: Next issues by priority (list of dicts with number, title).
        """
        if not self.status_file:
            return

        active_agents = [
            {
                "worker_number": w.worker_number,
                "agent_label": _worker_label(w.worker_number),
                "issue_number": w.issue_number,
                "issue_title": w.issue_title,
                "phase": w.phase,
                "updated": w.updated,
            }
            for w in self._workers.values()
        ]

        status = build_dispatcher_status(
            active_agents=active_agents,
            open_prs=open_prs or [],
            recently_completed=self._recently_completed[-10:],
            queue=queue or [],
            paused=self.paused,
            stopped_issues=sorted(self._stopped_issues),
            prs_since_last_audit=self.prs_since_last_audit,
            session_number=self.session_number,
        )

        path = Path(self.status_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, indent=2, default=str))

    def refresh_state(self) -> None:
        """Re-read the state file to pick up external changes.

        The responder daemon writes ``paused`` and worker state directly
        to the state file.  The dispatcher should call this method
        before each spawn decision to pick up those changes.
        """
        self._load_state()

    def read_stop_requests(self) -> list[int]:
        """Read and clear stop requests written by the responder daemon.

        The responder writes stop requests to *stop_requests_path* as a
        JSON array of objects with an ``issue_number`` field.  This method
        reads the file atomically (with ``fcntl.flock``), extracts the
        issue numbers, truncates the file, and merges the numbers into
        the internal ``_stopped_issues`` set.

        Returns the list of *newly read* issue numbers from this call.
        Previously read stop requests remain in ``_stopped_issues`` until
        cleared via :meth:`clear_stopped_issues`.
        """
        if not self.stop_requests_path:
            return []

        path = Path(self.stop_requests_path)
        if not path.exists():
            return []

        new_issues: list[int] = []

        try:
            fd = os.open(str(path), os.O_RDWR)
            with os.fdopen(fd, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                content = f.read()
                if not content.strip():
                    return []

                try:
                    entries = json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Corrupt stop_requests file -- clearing it.")
                    entries = []

                # Truncate the file while we hold the lock.
                f.seek(0)
                f.truncate()

            for entry in entries:
                if isinstance(entry, dict):
                    issue_num = entry.get("issue_number")
                    if isinstance(issue_num, int):
                        new_issues.append(issue_num)
                        self._stopped_issues.add(issue_num)

        except Exception:
            logger.warning("Failed to read stop_requests file", exc_info=True)

        return new_issues

    def is_issue_stopped(self, issue_number: int) -> bool:
        """Return ``True`` if *issue_number* has been stopped via a stop request."""
        return issue_number in self._stopped_issues

    def clear_stopped_issues(self) -> None:
        """Clear all accumulated stop requests."""
        self._stopped_issues.clear()

    @property
    def stopped_issues(self) -> frozenset[int]:
        """Return a snapshot of all stopped issue numbers."""
        return frozenset(self._stopped_issues)

    # ── Lifecycle notifications ─────────────────────────────────────────

    async def session_started(self) -> None:
        """Notify that the dispatcher session has started."""
        await self.bridge.notify("Orchestrator session started. Send commands anytime.")

    async def session_ended(self) -> None:
        """Notify that the dispatcher session has ended."""
        self._clear_state_file()
        await self.bridge.notify("Orchestrator session ended.")

    async def task_started(self, *, issue_number: int, title: str, worker: int | str) -> None:
        """Notify that a task agent has been spawned."""
        self._workers[worker] = WorkerInfo(
            worker_number=worker,
            issue_number=issue_number,
            issue_title=title,
            phase="starting",
            updated=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        self._save_state()
        self.write_status()
        label = _worker_label(worker)
        await self.bridge.status_update(
            task=f"#{issue_number}",
            state="in_progress",
            details=f"Starting: {title} ({label})",
            repo=self.repo,
        )

    async def task_completed(self, *, issue_number: int, summary: str, worker: int | str) -> None:
        """Notify that a task agent has completed successfully."""
        self._workers.pop(worker, None)
        self._recently_completed.append(
            {
                "issue_number": issue_number,
                "outcome": "completed",
                "summary": summary,
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        )
        self._save_state()
        self.write_status()
        await self.bridge.status_update(
            task=f"#{issue_number}",
            state="complete",
            details=summary,
            repo=self.repo,
        )

    async def task_failed(self, *, issue_number: int, error: str, worker: int | str) -> None:
        """Notify that a task agent has failed."""
        self._workers.pop(worker, None)
        self._recently_completed.append(
            {
                "issue_number": issue_number,
                "outcome": "failed",
                "error": error,
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            }
        )
        self._save_state()
        self.write_status()
        await self.bridge.status_update(
            task=f"#{issue_number}",
            state="failed",
            details=error,
            repo=self.repo,
        )

    # ── Worker tracking ─────────────────────────────────────────────────

    def update_worker(self, worker: int | str, *, phase: str) -> None:
        """Update the tracked phase for *worker*."""
        if worker in self._workers:
            self._workers[worker].phase = phase
            self._workers[worker].updated = datetime.datetime.now(datetime.UTC).isoformat()
            self._save_state()

    def get_workers(self) -> list[WorkerInfo]:
        """Return a snapshot of all tracked workers."""
        return list(self._workers.values())

    # ── Reply helpers ───────────────────────────────────────────────────

    async def reply(
        self,
        text: str,
        *,
        command_id: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Send a free-form reply back to Telegram.

        Use this after processing a ``discuss``, ``do``, or ``free_text``
        command to send the dispatcher's response back to the user via
        Telegram, making the channel truly bidirectional.

        Args:
            text: The reply text to send.
            command_id: If provided, resolves the corresponding pending
                reply so that no timeout fallback is sent.
            reply_to_message_id: If provided, the Telegram ``message_id``
                to thread this reply to (sets ``reply_to_message_id`` in
                the Telegram API payload).

        No-op if the bridge is disabled.
        """
        if command_id is not None:
            self.resolve_pending_reply(command_id)
        await self.bridge.notify(text, repo=self.repo, reply_to_message_id=reply_to_message_id)

    # ── Pending reply tracking ─────────────────────────────────────────

    def _track_pending_reply(
        self,
        *,
        kind: str,
        raw_text: str,
        reply_to: int | None = None,
    ) -> str:
        """Create a pending reply record and return its unique command ID.

        Called internally by :meth:`handle_command` for ``discuss``, ``do``,
        and ``free_text`` commands.
        """
        command_id = uuid.uuid4().hex
        self._pending_replies[command_id] = PendingReply(
            command_id=command_id,
            kind=kind,
            raw_text=raw_text,
            reply_to=reply_to,
        )
        return command_id

    def resolve_pending_reply(self, command_id: str) -> PendingReply | None:
        """Remove a pending reply record, indicating it has been answered.

        Returns the removed :class:`PendingReply`, or ``None`` if the
        *command_id* was not found (already resolved or never tracked).
        """
        return self._pending_replies.pop(command_id, None)

    async def check_reply_timeouts(
        self,
        timeout_seconds: float | None = None,
    ) -> list[PendingReply]:
        """Check all pending replies and send a fallback for any that have timed out.

        Args:
            timeout_seconds: Override the default timeout. If ``None``, uses
                :attr:`reply_timeout_seconds`.

        Returns:
            A list of :class:`PendingReply` entries that timed out and
            received a fallback message.
        """
        if timeout_seconds is None:
            timeout_seconds = self.reply_timeout_seconds

        now = datetime.datetime.now(datetime.UTC)
        timed_out: list[PendingReply] = []

        for pending in list(self._pending_replies.values()):
            if pending.fallback_sent:
                continue
            elapsed = (now - pending.created_at).total_seconds()
            if elapsed >= timeout_seconds:
                pending.fallback_sent = True
                timed_out.append(pending)
                await self.bridge.notify(
                    "Still working on your request \u2014 will reply when ready.",
                    repo=self.repo,
                    reply_to_message_id=pending.reply_to,
                )

        return timed_out

    def get_pending_replies(self) -> list[PendingReply]:
        """Return a snapshot of all pending reply records."""
        return list(self._pending_replies.values())

    async def notify(self, text: str, *, repo: str = DEFAULT_GITHUB_REPO) -> None:
        """Send a plain-text notification to all allowed users.

        Pass-through to :meth:`TelegramBridge.notify` so that callers can
        use ``DispatcherBridge`` without needing to access the inner bridge
        directly.  No-op if the bridge is disabled.
        """
        await self.bridge.notify(text, repo=repo)

    async def status_update(
        self,
        *,
        task: str,
        state: str,
        details: str,
        repo: str = DEFAULT_GITHUB_REPO,
    ) -> None:
        """Send a formatted status card to all allowed users.

        Pass-through to :meth:`TelegramBridge.status_update` so that callers
        can use ``DispatcherBridge`` without needing to access the inner
        bridge directly.  No-op if the bridge is disabled.
        """
        await self.bridge.status_update(task=task, state=state, details=details, repo=repo)

    # ── Built-in command handlers ───────────────────────────────────────

    async def reply_status(self) -> None:
        """Send a status summary of active workers to Telegram."""
        workers = self.get_workers()
        if not workers:
            status_text = "No active issues."
            if self.paused:
                status_text += " (paused \u2014 not spawning new work)"
            await self.bridge.notify(status_text, repo=self.repo)
            return

        lines = []
        for w in workers:
            label = _worker_label(w.worker_number)
            lines.append(f"{label}: #{w.issue_number} ({w.phase}) \u2014 {w.issue_title}")
        summary = "\n".join(lines)
        if self.paused:
            summary += "\n\n(paused \u2014 not spawning new work)"
        await self.bridge.notify(summary, repo=self.repo)

    async def handle_command(self, cmd: Command) -> dict[str, Any]:
        """Handle a single command and return a result dict.

        The result dict always includes ``"handled": True/False`` and
        ``"reply"`` (the text sent back to the user, if any).

        For START and FREE_TEXT commands, the dispatcher must act on
        the returned result itself -- this method only acknowledges receipt.
        """
        result: dict[str, Any] = {"handled": True, "command": cmd}

        if cmd.kind == CommandKind.STATUS:
            await self.reply_status()
            result["reply"] = "Status sent."

        elif cmd.kind == CommandKind.PAUSE:
            self.paused = True
            self._save_state()
            self.write_status()
            await self.bridge.notify("Paused. No new issues will be spawned.", repo=self.repo)
            result["reply"] = "Paused."

        elif cmd.kind == CommandKind.RESUME:
            self.paused = False
            self._save_state()
            self.write_status()
            await self.bridge.notify("Resumed. Will spawn issues as normal.", repo=self.repo)
            result["reply"] = "Resumed."

        elif cmd.kind == CommandKind.START:
            await self.bridge.notify(
                f"Acknowledged: will start issue #{cmd.issue_number}.", repo=self.repo
            )
            result["reply"] = f"Starting #{cmd.issue_number}."
            result["action"] = "start_task"
            result["issue_number"] = cmd.issue_number

        elif cmd.kind == CommandKind.STOP:
            await self.bridge.notify(
                f"Acknowledged: noted stop request for #{cmd.issue_number}. "
                "Will not spawn more work for this issue.",
                repo=self.repo,
            )
            result["reply"] = f"Stop noted for #{cmd.issue_number}."
            result["action"] = "stop_task"
            result["issue_number"] = cmd.issue_number

        elif cmd.kind == CommandKind.FILE_ISSUE:
            await self.bridge.notify(
                "Filing that issue \u2014 I'll send the link when it's created.",
                repo=self.repo,
            )
            result["reply"] = "Filing issue."
            result["action"] = "file_issue"
            result["description"] = cmd.description
            result["priority"] = cmd.priority
            result["labels"] = list(cmd.labels)
            result["reply_to"] = cmd.reply_to

        elif cmd.kind == CommandKind.DISCUSS:
            # No Telegram notification here -- the responder daemon already
            # replied to the user with a Claude-generated acknowledgment.
            command_id = self._track_pending_reply(
                kind="discuss", raw_text=cmd.message, reply_to=cmd.reply_to
            )
            result["reply"] = "Forwarded to dispatcher for discussion."
            result["action"] = "discuss"
            result["message"] = cmd.message
            result["reply_to"] = cmd.reply_to
            result["needs_reply"] = True
            result["command_id"] = command_id

        elif cmd.kind == CommandKind.DO:
            # No Telegram notification here -- the responder daemon already
            # replied to the user with a Claude-generated acknowledgment.
            command_id = self._track_pending_reply(
                kind="do", raw_text=cmd.instruction, reply_to=cmd.reply_to
            )
            result["reply"] = "Forwarded to dispatcher for execution."
            result["action"] = "do"
            result["instruction"] = cmd.instruction
            result["reply_to"] = cmd.reply_to
            result["needs_reply"] = True
            result["command_id"] = command_id

        elif cmd.kind == CommandKind.FREE_TEXT:
            # No Telegram notification here -- the responder daemon already
            # replied to the user with a Claude-generated acknowledgment.
            command_id = self._track_pending_reply(
                kind="free_text", raw_text=cmd.raw_text, reply_to=cmd.reply_to
            )
            result["reply"] = f"Forwarded to dispatcher: {cmd.raw_text}"
            result["action"] = "forward"
            result["text"] = cmd.raw_text
            result["needs_reply"] = True
            result["command_id"] = command_id

        return result

    # ── File-based inbox (for use with tg-responder.py) ────────────────

    def read_inbox(self) -> list[Command]:
        """Read and clear all commands from the file-based inbox.

        The background daemon (``scripts/tg-responder.py``) writes raw SQS
        messages to *inbox_path* as a JSON array.  This method reads that file,
        parses each entry into a :class:`Command`, atomically truncates the
        file, and returns the parsed commands.

        Returns an empty list if *inbox_path* is not set, the file does not
        exist, or the file is empty.

        File locking (``fcntl.flock``) ensures mutual exclusion with the
        daemon's append operation.
        """
        if not self.inbox_path:
            return []

        path = Path(self.inbox_path)
        if not path.exists():
            return []

        commands: list[Command] = []

        try:
            fd = os.open(str(path), os.O_RDWR)
            with os.fdopen(fd, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                content = f.read()
                if not content.strip():
                    return []

                try:
                    entries = json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Corrupt inbox file -- clearing it.")
                    entries = []

                # Truncate the file while we hold the lock.
                f.seek(0)
                f.truncate()

            for entry in entries:
                if isinstance(entry, dict) and "action" in entry:
                    cmd = _parse_inbox_entry(entry)
                else:
                    text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
                    cmd = parse_command(text)
                commands.append(cmd)

        except Exception:
            logger.warning("Failed to read inbox file", exc_info=True)

        return commands

    # ── Dispatcher inbox (subagent -> dispatcher) ─────────────────────

    def read_dispatcher_inbox(self) -> list[DispatcherInstruction]:
        """Read and clear all instructions from the dispatcher inbox file.

        Subagents write instructions to *dispatcher_inbox_path* (typically
        ``tmp/dispatcher_inbox.json``) using ``scripts/dispatcher-request.py``.
        This method reads that file, parses each entry into a
        :class:`DispatcherInstruction`, atomically truncates the file, and
        returns the parsed instructions.

        Returns an empty list if *dispatcher_inbox_path* is not set, the
        file does not exist, or the file is empty.  Invalid entries (unknown
        action types, corrupt JSON) are logged and skipped.

        File locking (``fcntl.flock``) ensures mutual exclusion with the
        subagent's append operation.
        """
        if not self.dispatcher_inbox_path:
            return []

        path = Path(self.dispatcher_inbox_path)
        if not path.exists():
            return []

        instructions: list[DispatcherInstruction] = []

        try:
            fd = os.open(str(path), os.O_RDWR)
            with os.fdopen(fd, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                content = f.read()
                if not content.strip():
                    return []

                try:
                    entries = json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Corrupt dispatcher inbox file -- clearing it.")
                    entries = []

                # Truncate the file while we hold the lock.
                f.seek(0)
                f.truncate()

            if not isinstance(entries, list):
                logger.warning("Dispatcher inbox is not a JSON array -- ignoring.")
                return []

            for entry in entries:
                if not isinstance(entry, dict):
                    logger.warning("Skipping non-dict entry in dispatcher inbox.")
                    continue
                instruction = _parse_instruction(entry)
                if instruction is not None:
                    instructions.append(instruction)

        except OSError:
            return []
        except Exception:
            logger.warning("Failed to read dispatcher inbox file", exc_info=True)

        return instructions

    # ── Backward compatibility ──────────────────────────────────────────

    @property
    def orchestrator_inbox_path(self) -> str | None:
        """Backward-compat alias for dispatcher_inbox_path."""
        return self.dispatcher_inbox_path

    def read_orchestrator_inbox(self) -> list[DispatcherInstruction]:
        """Backward-compat alias for read_dispatcher_inbox."""
        return self.read_dispatcher_inbox()


def create_dispatcher_bridge(
    *,
    secret_id: str = "judgemind/telegram/bot",
    sqs_queue_url: str | None = None,
    region_name: str = "us-west-2",
    repo: str = DEFAULT_GITHUB_REPO,
    state_file: str | None = None,
    status_file: str | None = None,
    inbox_path: str | None = None,
    stop_requests_path: str | None = None,
    dispatcher_inbox_path: str | None = None,
    orchestrator_inbox_path: str | None = None,
) -> DispatcherBridge:
    """Factory that creates a :class:`DispatcherBridge` with a fresh client.

    If *state_file* is provided, worker state and the paused flag are persisted
    to that path so that short-lived invocations can share state across process
    boundaries.

    If *status_file* is provided, :meth:`DispatcherBridge.write_status` will
    write the full dispatcher status (active agents, PRs, queue, etc.) to
    that path.  The responder daemon reads this file to provide context to
    the Claude interpreter.

    If *inbox_path* is provided, :meth:`DispatcherBridge.read_inbox` will
    read commands from that file (written by ``scripts/tg-responder.py``).

    If *stop_requests_path* is provided,
    :meth:`DispatcherBridge.read_stop_requests` will read stop requests
    written by the responder daemon (``scripts/tg-responder.py``).

    If *dispatcher_inbox_path* is provided,
    :meth:`DispatcherBridge.read_dispatcher_inbox` will read instructions
    written by subagents via ``scripts/dispatcher-request.py``.

    The caller can also pass an existing :class:`TelegramBridge` directly
    to the dataclass constructor for testing.
    """
    # Support the old kwarg name for backward compat.
    effective_inbox_path = dispatcher_inbox_path or orchestrator_inbox_path

    bridge = TelegramBridge(
        secret_id=secret_id,
        sqs_queue_url=sqs_queue_url,
        region_name=region_name,
    )
    return DispatcherBridge(
        bridge=bridge,
        repo=repo,
        state_file=state_file,
        status_file=status_file,
        inbox_path=inbox_path,
        stop_requests_path=stop_requests_path,
        dispatcher_inbox_path=effective_inbox_path,
    )


# Backward-compat aliases — will be removed in a future release.
OrchestratorBridge = DispatcherBridge
OrchestratorInstruction = DispatcherInstruction
create_orchestrator_bridge = create_dispatcher_bridge
