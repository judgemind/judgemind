"""Orchestrator integration — lifecycle notifications and inbound command dispatch.

This module provides a high-level API that the Claude Code interactive session
(the orchestrator) uses to send notifications at task lifecycle events and to
poll for inbound commands from Telegram.

All functions are no-ops when Telegram is not configured.
"""

from __future__ import annotations

import asyncio
import datetime
import fcntl
import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .client import TelegramBridge
from .formatting import DEFAULT_GITHUB_REPO
from .interpreter import build_orchestrator_status
from .models import Message

logger = logging.getLogger(__name__)


# ── Command parsing ────────────────────────────────────────────────────────


class CommandKind(Enum):
    """Recognised inbound command types."""

    STATUS = "status"
    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    FREE_TEXT = "free_text"


@dataclass(frozen=True)
class Command:
    """A parsed inbound command from Telegram."""

    kind: CommandKind
    issue_number: int | None = None
    raw_text: str = ""


def parse_command(text: str) -> Command:
    """Parse a raw message string into a :class:`Command`.

    Recognised patterns (case-insensitive):
    - ``status`` → STATUS
    - ``start #N`` or ``start N`` → START with issue_number
    - ``stop #N`` or ``stop N`` → STOP with issue_number
    - ``pause`` → PAUSE
    - ``resume`` → RESUME
    - Anything else → FREE_TEXT
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


def _extract_issue_number(fragment: str) -> int | None:
    """Extract an issue number from ``#N`` or ``N``."""
    fragment = fragment.strip().lstrip("#")
    try:
        return int(fragment)
    except ValueError:
        return None


# ── Worker state (used for status replies) ─────────────────────────────────


@dataclass
class WorkerInfo:
    """Snapshot of a running worker for status reporting."""

    worker_number: int
    issue_number: int
    issue_title: str
    phase: str
    updated: str = ""


# ── Orchestrator class ────────────────────────────────────────────────────


@dataclass
class OrchestratorBridge:
    """High-level bridge between the orchestrator and Telegram.

    This wraps :class:`TelegramBridge` and adds:

    * **Lifecycle notifications** — ``session_started``, ``task_started``,
      ``task_completed``, ``task_failed``, ``session_ended``.
    * **Inbound command polling** — ``poll_commands`` reads the SQS queue,
      parses each message into a :class:`Command`, and returns them.
    * **Status reply** — ``reply_status`` sends a formatted summary of
      active workers back to Telegram.
    * **Pause/resume** — ``paused`` flag that the orchestrator can check
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
    _workers: dict[int, WorkerInfo] = field(default_factory=dict)
    _pending_commands: list[Command] = field(default_factory=list)
    _stopped_issues: set[int] = field(default_factory=set)
    _recently_completed: list[dict[str, Any]] = field(default_factory=list)
    _poll_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _poll_interval: float = field(default=30.0, repr=False)

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
            logger.warning("Failed to load state file %s — starting fresh.", self.state_file)

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
        """Write the orchestrator status JSON for the responder daemon.

        This writes ``orchestrator_status.json`` (at *status_file*) containing
        the full orchestrator state — active agents, open PRs, recently
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
                "issue_number": w.issue_number,
                "issue_title": w.issue_title,
                "phase": w.phase,
                "updated": w.updated,
            }
            for w in self._workers.values()
        ]

        status = build_orchestrator_status(
            active_agents=active_agents,
            open_prs=open_prs or [],
            recently_completed=self._recently_completed[-10:],
            queue=queue or [],
            paused=self.paused,
            stopped_issues=sorted(self._stopped_issues),
        )

        path = Path(self.status_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, indent=2, default=str))

    def refresh_state(self) -> None:
        """Re-read the state file to pick up external changes.

        The responder daemon writes ``paused`` and worker state directly
        to the state file.  The orchestrator should call this method
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
        except OSError:
            return []

        try:
            with os.fdopen(fd, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                content = f.read()
                if not content.strip():
                    return []

                try:
                    entries = json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Corrupt stop_requests file — clearing it.")
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
        """Notify that the orchestrator session has started."""
        await self.bridge.notify("Orchestrator session started. Send commands anytime.")

    async def session_ended(self) -> None:
        """Notify that the orchestrator session has ended."""
        self._clear_state_file()
        await self.bridge.notify("Orchestrator session ended.")

    async def task_started(self, *, issue_number: int, title: str, worker: int) -> None:
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
        await self.bridge.status_update(
            task=f"#{issue_number}",
            state="in_progress",
            details=f"Starting: {title} (worker-{worker})",
            repo=self.repo,
        )

    async def task_completed(self, *, issue_number: int, summary: str, worker: int) -> None:
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

    async def task_failed(self, *, issue_number: int, error: str, worker: int) -> None:
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

    def update_worker(self, worker: int, *, phase: str) -> None:
        """Update the tracked phase for *worker*."""
        if worker in self._workers:
            self._workers[worker].phase = phase
            self._workers[worker].updated = datetime.datetime.now(datetime.UTC).isoformat()
            self._save_state()

    def get_workers(self) -> list[WorkerInfo]:
        """Return a snapshot of all tracked workers."""
        return list(self._workers.values())

    # ── Reply helpers ───────────────────────────────────────────────────

    async def reply(self, text: str) -> None:
        """Send a free-form reply back to Telegram.

        Use this after processing a ``FREE_TEXT`` command to send the
        orchestrator's response back to the user via Telegram, making
        the channel truly bidirectional.

        No-op if the bridge is disabled.
        """
        await self.bridge.notify(text, repo=self.repo)

    async def notify(self, text: str, *, repo: str = DEFAULT_GITHUB_REPO) -> None:
        """Send a plain-text notification to all allowed users.

        Pass-through to :meth:`TelegramBridge.notify` so that callers can
        use ``OrchestratorBridge`` without needing to access the inner bridge
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
        can use ``OrchestratorBridge`` without needing to access the inner
        bridge directly.  No-op if the bridge is disabled.
        """
        await self.bridge.status_update(task=task, state=state, details=details, repo=repo)

    # ── Inbound command polling ─────────────────────────────────────────

    async def poll_commands(self) -> list[Command]:
        """Poll for inbound messages and parse them into commands.

        Returns an empty list if the bridge is disabled or the queue is empty.
        """
        messages: list[Message] = await self.bridge.poll()
        commands: list[Command] = []
        for msg in messages:
            cmd = parse_command(msg.text)
            commands.append(cmd)
        return commands

    # ── Built-in command handlers ───────────────────────────────────────

    async def reply_status(self) -> None:
        """Send a status summary of active workers to Telegram."""
        workers = self.get_workers()
        if not workers:
            status_text = "No active issues."
            if self.paused:
                status_text += " (paused — not spawning new work)"
            await self.bridge.notify(status_text, repo=self.repo)
            return

        lines = []
        for w in workers:
            lines.append(
                f"Worker-{w.worker_number}: #{w.issue_number} ({w.phase}) — {w.issue_title}"
            )
        summary = "\n".join(lines)
        if self.paused:
            summary += "\n\n(paused — not spawning new work)"
        await self.bridge.notify(summary, repo=self.repo)

    async def handle_command(self, cmd: Command) -> dict[str, Any]:
        """Handle a single command and return a result dict.

        The result dict always includes ``"handled": True/False`` and
        ``"reply"`` (the text sent back to the user, if any).

        For START and FREE_TEXT commands, the orchestrator must act on
        the returned result itself — this method only acknowledges receipt.
        """
        result: dict[str, Any] = {"handled": True, "command": cmd}

        if cmd.kind == CommandKind.STATUS:
            await self.reply_status()
            result["reply"] = "Status sent."

        elif cmd.kind == CommandKind.PAUSE:
            self.paused = True
            self._save_state()
            await self.bridge.notify("Paused. No new issues will be spawned.", repo=self.repo)
            result["reply"] = "Paused."

        elif cmd.kind == CommandKind.RESUME:
            self.paused = False
            self._save_state()
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

        elif cmd.kind == CommandKind.FREE_TEXT:
            await self.bridge.notify(f"Received: {cmd.raw_text}", repo=self.repo)
            result["reply"] = f"Forwarded to orchestrator: {cmd.raw_text}"
            result["action"] = "forward"
            result["text"] = cmd.raw_text
            result["needs_reply"] = True

        return result

    async def process_commands(self) -> list[dict[str, Any]]:
        """Poll for commands and handle each one. Returns list of result dicts.

        The orchestrator should inspect any results with ``action`` keys
        and act accordingly (e.g. spawn a task agent for ``start_task``).
        """
        commands = await self.poll_commands()
        results = []
        for cmd in commands:
            result = await self.handle_command(cmd)
            results.append(result)
        return results

    # ── Background polling ───────────────────────────────────────────

    async def start_polling(self, interval: float = 30.0) -> None:
        """Start a background task that polls for commands on *interval* seconds.

        Commands are accumulated in an internal buffer and can be retrieved
        with :meth:`drain_pending_commands`.  If polling is already active
        the existing task is cancelled and restarted with the new interval.

        No-op if the underlying bridge is disabled.
        """
        await self.stop_polling()
        self._poll_interval = interval
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop_polling(self) -> None:
        """Cancel the background polling task if one is running."""
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None

    @property
    def polling(self) -> bool:
        """Return ``True`` if background polling is active."""
        return self._poll_task is not None and not self._poll_task.done()

    def drain_pending_commands(self) -> list[Command]:
        """Return and clear all commands accumulated by background polling.

        This is the primary way for the orchestrator to consume commands
        when using :meth:`start_polling`.  The returned list may be empty
        if no commands have arrived since the last drain.
        """
        commands = list(self._pending_commands)
        self._pending_commands.clear()
        return commands

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
        except OSError:
            return []

        try:
            with os.fdopen(fd, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                content = f.read()
                if not content.strip():
                    return []

                try:
                    entries = json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("Corrupt inbox file — clearing it.")
                    entries = []

                # Truncate the file while we hold the lock.
                f.seek(0)
                f.truncate()

            for entry in entries:
                text = entry.get("text", "") if isinstance(entry, dict) else str(entry)
                cmd = parse_command(text)
                commands.append(cmd)

        except Exception:
            logger.warning("Failed to read inbox file", exc_info=True)

        return commands

    async def _poll_loop(self) -> None:
        """Internal loop that polls SQS on a fixed interval."""
        while True:
            try:
                commands = await self.poll_commands()
                self._pending_commands.extend(commands)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Background poll failed", exc_info=True)
            await asyncio.sleep(self._poll_interval)


def create_orchestrator_bridge(
    *,
    secret_id: str = "judgemind/telegram/bot",
    sqs_queue_url: str | None = None,
    region_name: str = "us-west-2",
    repo: str = DEFAULT_GITHUB_REPO,
    state_file: str | None = None,
    status_file: str | None = None,
    inbox_path: str | None = None,
    stop_requests_path: str | None = None,
) -> OrchestratorBridge:
    """Factory that creates an :class:`OrchestratorBridge` with a fresh client.

    If *state_file* is provided, worker state and the paused flag are persisted
    to that path so that short-lived invocations can share state across process
    boundaries.

    If *status_file* is provided, :meth:`OrchestratorBridge.write_status` will
    write the full orchestrator status (active agents, PRs, queue, etc.) to
    that path.  The responder daemon reads this file to provide context to
    the Claude interpreter.

    If *inbox_path* is provided, :meth:`OrchestratorBridge.read_inbox` will
    read commands from that file (written by ``scripts/tg-responder.py``)
    instead of polling SQS directly.

    If *stop_requests_path* is provided,
    :meth:`OrchestratorBridge.read_stop_requests` will read stop requests
    written by the responder daemon (``scripts/tg-responder.py``).

    The caller can also pass an existing :class:`TelegramBridge` directly
    to the dataclass constructor for testing.
    """
    bridge = TelegramBridge(
        secret_id=secret_id,
        sqs_queue_url=sqs_queue_url,
        region_name=region_name,
    )
    return OrchestratorBridge(
        bridge=bridge,
        repo=repo,
        state_file=state_file,
        status_file=status_file,
        inbox_path=inbox_path,
        stop_requests_path=stop_requests_path,
    )
