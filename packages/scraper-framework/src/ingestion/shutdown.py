"""Graceful SIGTERM shutdown for the ingestion worker (#5069).

ECS stops a task by sending SIGTERM, waiting the container's
``stopTimeout``, then sending SIGKILL.  The worker runs as PID 1
(``ENTRYPOINT ["python", "-m"]``), and the kernel drops signals PID 1 has no
handler for, so without a handler SIGTERM did nothing and every deploy ended
in SIGKILL (exit 137).  A SIGKILL runs no Python, so it also left the #5055
crash charge on the message in flight: three deploys during one long message
dead-lettered it as a worker killer.

``ShutdownCoordinator`` turns SIGTERM into a clean stop:

* **Idle** (blocked in ``XREADGROUP``): the handler raises
  ``WorkerShutdown`` at once, and ``run()`` returns.
* **Busy**: the handler only records the request.  The worker claims no new
  message, finishes the one in flight, acks it, and ``run()`` returns.
* **Busy past the grace period**: a watchdog thread releases the message in
  flight (takes back its crash charge, never acks it) and ends the process
  with exit code 0.  The message stays pending, so another consumer reclaims
  it with ``XAUTOCLAIM`` and no blame.

The watchdog, not a second signal, enforces the bound: an exception raised
into the main thread would first wait for any ``ThreadPoolExecutor`` it
unwinds through (the OCR page pool), which can outlast ``stopTimeout``.
``os._exit`` does not wait.

The grace period must stay below the task definition's ``stopTimeout``
(``infra/terraform/modules/compute/main.tf``, 120 s, the Fargate maximum) so
the release runs before ECS sends SIGKILL.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType

logger = logging.getLogger(__name__)

#: Seconds an in-flight message may keep running after SIGTERM before the
#: worker releases it and exits.  15 s under the 120 s ``stopTimeout`` leaves
#: room for the release (one Redis DECR) and the log flush.  On dev,
#: 2026-11-01..15, LLM-bearing messages (``reingest_doc_timing`` total_ms >
#: 1 s, n=5866) ran p95 52 s, p99 86 s; 8 (0.14%) ran past 110 s.
DEFAULT_SHUTDOWN_GRACE_S = 105.0

#: Environment variable that overrides ``DEFAULT_SHUTDOWN_GRACE_S``.
SHUTDOWN_GRACE_ENV = "SHUTDOWN_GRACE_S"

#: Exit code of a shutdown that released its in-flight message.  0: the stop
#: was asked for and the message was handed back cleanly, so ECS history
#: shows a clean exit, not a crash.
RELEASED_EXIT_CODE = 0


class WorkerShutdown(BaseException):
    """Raised into the main thread by SIGTERM while the worker is idle.

    A ``BaseException`` so the consumer loop's ``except Exception`` does not
    swallow it."""


def shutdown_grace_from_env(default: float = DEFAULT_SHUTDOWN_GRACE_S) -> float:
    """``SHUTDOWN_GRACE_S`` as seconds, or *default* when unset or invalid."""
    raw = os.environ.get(SHUTDOWN_GRACE_ENV, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", SHUTDOWN_GRACE_ENV, raw)
        return default
    if value < 0:
        logger.warning("Ignoring negative %s=%r", SHUTDOWN_GRACE_ENV, raw)
        return default
    return value


class ShutdownCoordinator:
    """Shutdown state shared by the worker's main thread, its SIGTERM
    handler, and the grace-period watchdog thread.

    The worker sets ``idle`` around its blocking read, and sets / clears
    ``in_flight`` (the message it is processing) under ``lock``.
    ``release`` is called by the watchdog, under ``lock``, with the message
    still in flight when the grace period ends."""

    def __init__(
        self,
        release: Callable[[bytes], None],
        *,
        grace_s: float = DEFAULT_SHUTDOWN_GRACE_S,
        exit_fn: Callable[[int], object] = os._exit,
    ) -> None:
        self._release = release
        self.grace_s = grace_s
        self._exit_fn = exit_fn
        #: Guards ``in_flight`` / ``released`` and every Redis write that
        #: settles a message (charge, ack, take-back), so the watchdog's
        #: release cannot interleave with them.
        self.lock = threading.Lock()
        self.requested = False
        self.idle = False
        self.in_flight: bytes | None = None
        self.released: bytes | None = None
        self.requested_at: float | None = None
        self._requested_event = threading.Event()
        self._stopped_event = threading.Event()
        self._watchdog: threading.Thread | None = None

    # -- setup ---------------------------------------------------------------

    def install(self) -> None:
        """Install the SIGTERM handler and start the watchdog thread.

        Call from the main thread (``signal.signal`` requires it)."""
        signal.signal(signal.SIGTERM, self._on_sigterm)
        self._watchdog = threading.Thread(target=self._watch, name="shutdown-watchdog", daemon=True)
        self._watchdog.start()

    # -- signal handler (main thread) ----------------------------------------

    def _on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        # Signal handlers interrupt arbitrary main-thread code, so this one
        # takes no lock and does no I/O (logging included): it records the
        # request and wakes the watchdog, which logs.
        if self.requested:
            return
        self.request()
        if self.idle:
            raise WorkerShutdown("SIGTERM while idle")

    def request(self) -> None:
        """Request a shutdown without a signal: safe from any thread.

        Unlike SIGTERM it does not interrupt an idle read; the worker stops
        when that read returns (at most its block interval)."""
        if self.requested:
            return
        self.requested = True
        self.requested_at = time.monotonic()
        self._requested_event.set()

    # -- main-thread hooks ---------------------------------------------------

    def stopped(self) -> None:
        """Tell the watchdog the main loop has exited cleanly."""
        self._stopped_event.set()

    def seconds_since_request(self) -> float | None:
        if self.requested_at is None:
            return None
        return round(time.monotonic() - self.requested_at, 3)

    # -- watchdog thread -----------------------------------------------------

    def _watch(self) -> None:
        self._requested_event.wait()
        if self._stopped_event.is_set():
            return
        logger.info(
            "SIGTERM received: claiming no new messages, finishing message %s within %.0fs",
            self._decode(self.in_flight),
            self.grace_s,
            extra={
                "telemetry_event": "worker_shutdown_requested",
                "in_flight": self._decode(self.in_flight),
                "grace_s": self.grace_s,
            },
        )
        if self._stopped_event.wait(self.grace_s):
            return
        self._expire()

    def _expire(self) -> None:
        """End the process after the grace period, releasing the message in
        flight: its crash charge is taken back and it is not acked."""
        with self.lock:
            msg_id = self.in_flight
            if msg_id is not None:
                try:
                    self._release(msg_id)
                except Exception as exc:  # pragma: no cover - release logs its own
                    logger.warning("Could not release message %s: %s", msg_id, exc)
                self.released = msg_id
                self.in_flight = None
            logger.warning(
                "Shutdown grace period (%.0fs) expired: released message %s "
                "uncharged and unacked; exiting",
                self.grace_s,
                self._decode(msg_id),
                extra={
                    "telemetry_event": "worker_shutdown_released_message",
                    "released": self._decode(msg_id),
                    "grace_s": self.grace_s,
                },
            )
            _flush_logs()
            self._exit_fn(RELEASED_EXIT_CODE)

    @staticmethod
    def _decode(msg_id: bytes | None) -> str | None:
        if msg_id is None:
            return None
        return msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)


def _flush_logs() -> None:
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # pragma: no cover - best effort before os._exit
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # pragma: no cover - best effort before os._exit
            pass
