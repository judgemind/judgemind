"""Shared fake :class:`subprocess.Popen` for phase-spawn tests.

Issue #3017 migrated ``_spawn_phase_subprocess`` and
``_spawn_diagnoser_subprocess`` from :func:`subprocess.run` to
:class:`subprocess.Popen` + a stream-forwarder utility so child output
streams to CloudWatch in real time. The old tests that patched
``subprocess.run`` with a ``fake_run`` closure needed equivalent
``Popen`` fakery.

This module provides :func:`make_popen_factory` — a helper that builds a
drop-in ``Popen`` replacement for :func:`monkeypatch.setattr(subprocess, "Popen", ...)`.
Callers supply:

- ``stdout_chunks`` / ``stderr_chunks``: iterables of strings the fake
  child will emit through its stdout/stderr pipes.
- ``returncode``: exit code :meth:`wait` returns.
- ``timeout``: if truthy, :meth:`wait` raises
  :class:`subprocess.TimeoutExpired` on first call.
- ``on_start``: optional callback invoked with the captured cmd + kwargs
  — replaces the ``captured['cmd'] = cmd`` pattern of the old tests.

The fake satisfies everything the stream-forwarder and daemon need:

1. ``stdout`` / ``stderr`` are :class:`io.StringIO` handles the forwarder
   reads line-by-line; the fake pre-populates them with the requested
   chunks so ``for line in source`` sees the content immediately.
2. ``wait(timeout=...)`` returns the desired ``returncode`` or raises
   ``TimeoutExpired``.
3. ``kill()`` and ``poll()`` are no-ops that return sensibly.

This keeps the tests focused on the daemon's behaviour (command
construction, file writes, log events) without re-running the forwarder's
own unit tests for every spawn-test.
"""

from __future__ import annotations

import io
import subprocess
from typing import Any, Callable, Iterable


class _FakePopen:
    """Minimal Popen substitute for :mod:`subprocess` tests.

    Instances are not usually constructed directly — use the factory
    returned by :func:`make_popen_factory` and install it via
    :meth:`pytest.MonkeyPatch.setattr(subprocess, 'Popen', factory)`.
    """

    def __init__(
        self,
        *,
        stdout_chunks: Iterable[str],
        stderr_chunks: Iterable[str],
        returncode: int,
        timeout: bool,
        pid: int = 4242,
    ) -> None:
        self.stdout = io.StringIO("".join(stdout_chunks))
        self.stderr = io.StringIO("".join(stderr_chunks))
        self._returncode = returncode
        self._raise_timeout = timeout
        self._finished = False
        # #3376 — async fire-and-forget spawn writes the PID to
        # ``dispatcher.diagnoses.subprocess_pid``; the new tests assert
        # the value gets persisted, so the fake must expose a stable
        # ``pid`` attribute. Default 4242 for tests that don't override.
        self.pid = pid

    def wait(self, timeout: float | None = None) -> int:
        if self._raise_timeout and not self._finished:
            # Raise once, then pretend the kill()+wait path finished.
            self._raise_timeout = False
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=timeout or 0)
        self._finished = True
        return self._returncode

    def kill(self) -> None:
        self._finished = True

    def poll(self) -> int | None:
        return self._returncode if self._finished else None


def make_popen_factory(
    *,
    stdout_chunks: Iterable[str] = (),
    stderr_chunks: Iterable[str] = (),
    returncode: int = 0,
    timeout: bool = False,
    pid: int = 4242,
    on_start: Callable[[list[str], dict[str, Any]], None] | None = None,
) -> Callable[..., _FakePopen]:
    """Return a ``Popen`` factory with the requested behaviour baked in.

    The factory signature matches :class:`subprocess.Popen` enough for
    monkeypatching: it accepts the command list as its first positional
    argument and ignores ``stdout=``, ``stderr=``, ``bufsize=``,
    ``text=``, ``cwd=`` kwargs. Callers who care about those kwargs pass
    an ``on_start`` callback to capture them.

    ``pid`` controls the value the fake exposes via ``proc.pid`` —
    relied on by the async-spawn tests added for issue #3376.
    """
    chunks_out = list(stdout_chunks)
    chunks_err = list(stderr_chunks)

    def _factory(cmd: list[str], **kwargs: Any) -> _FakePopen:
        if on_start is not None:
            on_start(cmd, kwargs)
        return _FakePopen(
            stdout_chunks=chunks_out,
            stderr_chunks=chunks_err,
            returncode=returncode,
            timeout=timeout,
            pid=pid,
        )

    return _factory
