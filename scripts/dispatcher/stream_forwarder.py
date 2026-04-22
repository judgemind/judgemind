"""Line-by-line subprocess stdout/stderr forwarder for dispatcher phase spawns.

Single shared utility that wraps a :class:`subprocess.Popen` handle and
forwards every line of the child's ``stdout`` and ``stderr`` in real time
to two sinks:

1. **Structured logger.** Each line becomes a JSON log record tagged with
   ``agent_id``, ``issue_number``, ``phase``, ``stream`` (``"stdout"`` /
   ``"stderr"``), and the raw child output under the ``raw_message`` key.
   The daemon's JSON log formatter ships these to CloudWatch where Log
   Insights can filter/group by ``agent_id`` and ``phase`` during a live
   hang. The key is ``raw_message`` rather than ``message`` because
   ``message`` is a reserved :class:`logging.LogRecord` attribute —
   passing it via ``extra`` raises ``KeyError``. Log Insights queries
   therefore use ``filter raw_message like /.../``.
2. **Raw JSONL mirror file.** The same structured events are written to
   ``{worktree}/.dispatcher/{phase}-{agent_id}.jsonl`` for live forensics
   — operators can ``exec`` into the running task and ``tail -f`` the
   file to see activity pause in real time.

Two non-daemon reader threads (one per stream) drive the forwarding. We
deliberately avoid :meth:`subprocess.Popen.communicate`, which would
block until EOF and defeat the whole point of real-time visibility. The
threads also continue pumping output during a hang (e.g. a reviewer
subprocess that pauses for 10 minutes) so operators can see the last
line emitted before the stall.

Usage (inside the daemon's phase-spawn code path)::

    with open(stdout_capture_path, "w") as stdout_sink, \\
         open(stderr_capture_path, "w") as stderr_sink:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
            cwd=str(worktree),
        )
        stream_subprocess_output(
            proc,
            agent_id=agent_id,
            issue_number=issue_number,
            phase=phase,
            logger=self._log,
            jsonl_path=worktree / ".dispatcher" / f"{phase}-{agent_id}.jsonl",
            stdout_sink=stdout_sink,
            stderr_sink=stderr_sink,
        )
        returncode = proc.wait(timeout=timeout_seconds)

The ``stdout_sink`` / ``stderr_sink`` file objects are optional tee
targets — when provided, the forwarder also writes each line verbatim
(with its trailing newline) to the sink before structuring it. This
preserves the pre-existing ``claude-p-<phase>.stdout.json`` /
``.stderr.log`` capture layout the daemon's metering code relies on
(``_parse_phase_usage`` reads the full stdout.json envelope), while
adding the structured line-by-line forwarding on top. See issue #3017
for the full rationale.

Design notes
------------

- **Reader threads are not daemon threads.** They are explicit,
  joined-by-the-caller threads so that a caller-side ``wait(timeout)``
  fault (``subprocess.TimeoutExpired``) does not strand them. The
  caller is responsible for calling :func:`join_stream_threads` (or
  using the ``ForwarderThreads`` handle returned by
  :func:`stream_subprocess_output_async`) after terminating the child.
- **Line buffering is the child's responsibility.** The Popen call site
  must set ``bufsize=1`` and ``text=True`` — without those, the Python
  stdlib buffers stdout in 8KB chunks and the real-time guarantee is
  lost. The forwarder itself reads in ``for line in file`` mode, which
  returns complete lines as soon as the child flushes.
- **The JSONL mirror is append-only.** The file is opened in ``"a"``
  mode so multiple retries of the same phase in the same worktree (e.g.
  ralph re-invoked from Step 2.5 on pre-push failure) accumulate rather
  than overwriting prior context.
- **Non-JSON stdout is handled.** The ``claude -p --output-format json``
  payload is a single JSON envelope emitted at exit, but stderr and any
  interim stdout lines are plain text. The forwarder treats every line
  as a raw string; it does not attempt to parse JSON. Downstream
  consumers (Log Insights ``parse @message`` filters) do the parsing.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import subprocess


#: Maximum length of a single forwarded line. Anything longer is
#: truncated before structured-logging / JSONL emission. CloudWatch Logs
#: accepts up to 256KB per event; we cap far below that so a runaway
#: reviewer line doesn't blow the per-record budget. The JSONL mirror
#: file honours the same cap for consistency — the truncation marker
#: (``"...[truncated]"``) makes it visible to operators grepping the
#: file that the line was clipped.
MAX_LINE_CHARS = 16_000

#: Truncation marker appended to lines longer than :data:`MAX_LINE_CHARS`.
TRUNCATION_MARKER = "...[truncated]"


@dataclass
class ForwarderThreads:
    """Handle returned by :func:`stream_subprocess_output_async`.

    Caller must invoke :meth:`join` after the subprocess exits (or is
    killed on timeout) so reader threads terminate cleanly.
    """

    stdout_thread: threading.Thread
    stderr_thread: threading.Thread

    def join(self, timeout: float | None = None) -> None:
        """Join both reader threads.

        A caller-side timeout here is best-effort — the threads exit on
        stream EOF, which happens once the child closes its FDs on
        process exit. If the caller forcibly killed the child via
        :meth:`subprocess.Popen.kill`, EOF arrives immediately. For a
        clean ``wait()`` exit, EOF has already arrived by the time the
        caller joins, so the join is near-instant.
        """
        self.stdout_thread.join(timeout=timeout)
        self.stderr_thread.join(timeout=timeout)


def _truncate_line(line: str) -> str:
    """Clip a single forwarded line to :data:`MAX_LINE_CHARS`.

    Keeps the leading ``MAX_LINE_CHARS - len(TRUNCATION_MARKER)`` chars
    and appends the marker so downstream readers can distinguish an
    intentionally-long line from a clipped one.
    """
    if len(line) <= MAX_LINE_CHARS:
        return line
    head_budget = MAX_LINE_CHARS - len(TRUNCATION_MARKER)
    return line[:head_budget] + TRUNCATION_MARKER


def _emit_line(
    *,
    line: str,
    stream: str,
    agent_id: str,
    issue_number: int | None,
    phase: str,
    logger: logging.Logger,
    jsonl_handle: IO[str] | None,
) -> None:
    """Structured-log a single line and mirror it to the JSONL file.

    Pure sink — no I/O against the child. Caller-invoked per line by the
    reader threads. Exceptions from either the logger or the JSONL write
    are swallowed so a broken sink never kills the reader thread (and
    thus never stalls the child's pipe).
    """
    clipped = _truncate_line(line.rstrip("\n"))
    record: dict[str, Any] = {
        "event": "phase_subprocess_line",
        "agent_id": agent_id,
        "issue_number": issue_number,
        "phase": phase,
        "stream": stream,
        # ``raw_message`` rather than ``message``: the latter is a
        # reserved :class:`logging.LogRecord` attribute and passing it
        # via ``extra`` raises ``KeyError``. See issue #3017 test
        # ``test_none_issue_number_is_logged_as_null`` for the
        # regression.
        "raw_message": clipped,
    }
    try:
        logger.info("daemon.phase_subprocess_line", extra=record)
    except Exception:  # pragma: no cover — defensive
        pass
    if jsonl_handle is not None:
        try:
            jsonl_handle.write(json.dumps(record, default=str) + "\n")
            jsonl_handle.flush()
        except Exception:  # pragma: no cover — defensive
            pass


def _pump_stream(
    *,
    source: IO[str] | None,
    sink: IO[str] | None,
    stream: str,
    agent_id: str,
    issue_number: int | None,
    phase: str,
    logger: logging.Logger,
    jsonl_handle: IO[str] | None,
) -> None:
    """Read ``source`` line-by-line and forward each line to every sink.

    Runs on a dedicated reader thread — one per stream. Terminates on
    EOF (child closed its end of the pipe, usually at process exit).
    """
    if source is None:
        return
    try:
        for raw_line in source:
            # ``raw_line`` always ends with ``\n`` except possibly the
            # last partial line before EOF. We preserve the trailing
            # newline when writing to ``sink`` (operator-facing log
            # file) but strip it for structured-log emission so the
            # JSON envelope ``message`` field doesn't carry a literal
            # ``\n``.
            if sink is not None:
                try:
                    sink.write(raw_line)
                    sink.flush()
                except Exception:  # pragma: no cover — defensive
                    pass
            _emit_line(
                line=raw_line,
                stream=stream,
                agent_id=agent_id,
                issue_number=issue_number,
                phase=phase,
                logger=logger,
                jsonl_handle=jsonl_handle,
            )
    except Exception:  # pragma: no cover — defensive
        # A broken source pipe should not leak as an unhandled thread
        # exception; the main thread is authoritative for subprocess
        # lifecycle via ``proc.wait()``.
        return


def stream_subprocess_output_async(
    proc: subprocess.Popen[str],
    *,
    agent_id: str,
    issue_number: int | None,
    phase: str,
    logger: logging.Logger,
    jsonl_path: Path | None,
    stdout_sink: IO[str] | None = None,
    stderr_sink: IO[str] | None = None,
) -> ForwarderThreads:
    """Start reader threads for *proc*'s stdout and stderr.

    Returns a :class:`ForwarderThreads` handle the caller joins after
    the subprocess exits (or is killed on timeout). The returned
    threads are non-daemon — callers must join them, but they self-
    terminate on EOF, so a normal ``proc.wait()`` path is followed by
    an immediate ``threads.join()``.

    The JSONL mirror file is opened here (parent ``.dispatcher/``
    directory is created if missing) so the caller does not have to
    thread an open file handle through multiple layers. Ownership
    follows the thread lifetime — the handle is closed after both
    threads exit (see :meth:`_close_jsonl_on_both_threads_exit`).

    ``stdout_sink`` / ``stderr_sink`` are optional tee targets the
    forwarder writes each line to verbatim (with the trailing newline).
    The daemon's pre-existing capture layout
    (``claude-p-<phase>.stdout.json``, ``claude-p-<phase>.stderr.log``)
    is preserved by passing open file handles to those paths as the
    sinks. ``None`` means "do not tee to a file" — used by tests.
    """
    jsonl_handle: IO[str] | None = None
    if jsonl_path is not None:
        try:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            jsonl_handle = jsonl_path.open("a", encoding="utf-8")
        except Exception:  # pragma: no cover — defensive
            jsonl_handle = None

    stdout_thread = threading.Thread(
        target=_pump_stream,
        kwargs={
            "source": proc.stdout,
            "sink": stdout_sink,
            "stream": "stdout",
            "agent_id": agent_id,
            "issue_number": issue_number,
            "phase": phase,
            "logger": logger,
            "jsonl_handle": jsonl_handle,
        },
        name=f"stream-forwarder-stdout-{agent_id}-{phase}",
        daemon=False,
    )
    stderr_thread = threading.Thread(
        target=_pump_stream,
        kwargs={
            "source": proc.stderr,
            "sink": stderr_sink,
            "stream": "stderr",
            "agent_id": agent_id,
            "issue_number": issue_number,
            "phase": phase,
            "logger": logger,
            "jsonl_handle": jsonl_handle,
        },
        name=f"stream-forwarder-stderr-{agent_id}-{phase}",
        daemon=False,
    )

    # A third daemon thread waits for both reader threads to exit and
    # then closes the shared JSONL handle. This keeps the handle alive
    # for the full forwarding lifetime without requiring the caller to
    # manage it, and avoids the two reader threads racing on close().
    def _closer() -> None:
        stdout_thread.join()
        stderr_thread.join()
        if jsonl_handle is not None:
            try:
                jsonl_handle.close()
            except Exception:  # pragma: no cover — defensive
                pass

    closer_thread = threading.Thread(
        target=_closer,
        name=f"stream-forwarder-closer-{agent_id}-{phase}",
        daemon=True,
    )

    stdout_thread.start()
    stderr_thread.start()
    closer_thread.start()

    return ForwarderThreads(
        stdout_thread=stdout_thread,
        stderr_thread=stderr_thread,
    )
