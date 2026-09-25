"""Regression tests for #4705 — idle XREADGROUP polls raising a socket timeout.

redis-py 8 changed ``DEFAULT_SOCKET_TIMEOUT`` from ``None`` (block forever) to
5 seconds.  The ingestion worker polls with ``XREADGROUP ... BLOCK 5000``, so
every idle poll raced a 5s client read timeout and surfaced as
``redis.exceptions.TimeoutError: Timeout reading from socket``, logged at ERROR
by the consumer loop roughly every 5 seconds (~23k events/day on dev).

The fix sizes the consumer client's ``socket_timeout`` from the block interval
plus a margin, so an idle poll returns an empty result instead of raising.  A
genuine hang (server silent well past the block interval) still raises and is
still logged at ERROR.

These tests run a tiny in-process fake Redis server over a real TCP socket so
the redis-py client's actual timeout handling is exercised, not a mock.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Iterator
from typing import BinaryIO
from unittest.mock import MagicMock, patch

import pytest
import redis

from ingestion.worker import (
    DEFAULT_BLOCK_MS,
    IngestionWorker,
    redis_socket_timeout_for_block,
)

# ---------------------------------------------------------------------------
# Fake Redis server
# ---------------------------------------------------------------------------


def _read_command(rfile: BinaryIO) -> list[bytes] | None:
    """Parse one RESP array-of-bulk-strings command; None on EOF."""
    header = rfile.readline()
    if not header:
        return None
    assert header.startswith(b"*"), header
    parts: list[bytes] = []
    for _ in range(int(header[1:].strip())):
        length_line = rfile.readline()
        length = int(length_line[1:].strip())
        parts.append(rfile.read(length + 2)[:-2])
    return parts


class _FakeRedisServer:
    """Speaks just enough RESP3 for the redis-py handshake plus XREADGROUP.

    ``XREADGROUP`` behaves like a real idle stream: the server holds the reply
    for the requested BLOCK interval (scaled by ``block_scale``), then returns
    a RESP3 null.  ``block_scale`` > 1 simulates a server that stays silent
    past the block interval (a genuine hang).
    """

    def __init__(self, block_scale: float = 1.0) -> None:
        self.block_scale = block_scale
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"

    def _serve(self) -> None:
        self._sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        rfile = conn.makefile("rb")
        try:
            while not self._stop.is_set():
                cmd = _read_command(rfile)
                if cmd is None:
                    return
                name = cmd[0].upper()
                if name == b"HELLO":
                    conn.sendall(b"%1\r\n$5\r\nproto\r\n:3\r\n")
                elif name == b"XREADGROUP":
                    block_ms = int(cmd[cmd.index(b"BLOCK") + 1])
                    # Not time.sleep: tests/conftest.py's autouse
                    # _fast_retry_sleeps patches the shared time module.
                    self._stop.wait(block_ms / 1000 * self.block_scale)
                    conn.sendall(b"_\r\n")
                else:
                    conn.sendall(b"+OK\r\n")
        except OSError:
            return
        finally:
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._sock.close()


@pytest.fixture
def fake_redis() -> Iterator[_FakeRedisServer]:
    server = _FakeRedisServer()
    yield server
    server.close()


@pytest.fixture
def hung_redis() -> Iterator[_FakeRedisServer]:
    # Silent for 100x the block interval: a genuine server/network hang.
    server = _FakeRedisServer(block_scale=100.0)
    yield server
    server.close()


def _worker(redis_client: object) -> IngestionWorker:
    return IngestionWorker(
        redis_client=redis_client,
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )


# A short block keeps the tests fast; the timeout sizing is proportional.
_TEST_BLOCK_MS = 300


# ---------------------------------------------------------------------------
# Idle poll path
# ---------------------------------------------------------------------------


def test_xreadgroup_timeout_socket_timeout_exceeds_block_interval() -> None:
    """The consumer client's read timeout must outlast the XREADGROUP block."""
    assert redis_socket_timeout_for_block(DEFAULT_BLOCK_MS) > DEFAULT_BLOCK_MS / 1000
    assert redis_socket_timeout_for_block(_TEST_BLOCK_MS) > _TEST_BLOCK_MS / 1000


def test_xreadgroup_timeout_idle_poll_is_empty_not_error(
    fake_redis: _FakeRedisServer,
) -> None:
    """An idle BLOCK poll returns empty and counts as an empty poll — no raise."""
    client = redis.Redis.from_url(
        fake_redis.url,
        decode_responses=False,
        socket_timeout=redis_socket_timeout_for_block(_TEST_BLOCK_MS),
    )
    worker = _worker(client)

    worker._process_batch(batch_size=10, block_ms=_TEST_BLOCK_MS)
    worker._process_batch(batch_size=10, block_ms=_TEST_BLOCK_MS)

    assert worker._empty_polls == 2


def test_xreadgroup_timeout_reproduces_with_socket_timeout_below_block(
    fake_redis: _FakeRedisServer,
) -> None:
    """Pins the #4705 mechanism: socket_timeout <= block makes idle polls raise.

    The dev configuration before the fix was the boundary case (redis-py 8
    default socket_timeout=5s vs BLOCK 5000); a timeout comfortably below the
    block keeps this test deterministic.
    """
    client = redis.Redis.from_url(
        fake_redis.url,
        decode_responses=False,
        socket_timeout=_TEST_BLOCK_MS / 1000 / 2,
    )
    worker = _worker(client)

    with pytest.raises(redis.exceptions.TimeoutError, match="Timeout reading from socket"):
        worker._process_batch(batch_size=10, block_ms=_TEST_BLOCK_MS)


def test_xreadgroup_timeout_genuine_hang_still_raises(hung_redis: _FakeRedisServer) -> None:
    """A server silent well past block + margin still raises — stays loud."""
    client = redis.Redis.from_url(
        hung_redis.url,
        decode_responses=False,
        # Shrink the margin so the test does not wait the production 10s.
        socket_timeout=_TEST_BLOCK_MS / 1000 + 0.2,
    )
    worker = _worker(client)

    with pytest.raises(redis.exceptions.TimeoutError):
        worker._process_batch(batch_size=10, block_ms=_TEST_BLOCK_MS)


# ---------------------------------------------------------------------------
# Entrypoint wiring
# ---------------------------------------------------------------------------


@patch("ingestion.__main__.IngestionWorker")
@patch("ingestion.__main__.make_s3_client")
@patch("ingestion.__main__.make_opensearch_client")
@patch.object(redis.Redis, "ping", return_value=True)
@patch.dict(
    "os.environ",
    {
        "DATABASE_URL": "postgresql://localhost/test",
        "REDIS_URL": "redis://127.0.0.1:6379",
        "OPENSEARCH_URL": "https://localhost:9200",
        "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
    },
    clear=False,
)
def test_xreadgroup_timeout_main_builds_client_with_block_aware_timeout(
    _mock_ping: MagicMock,
    _mock_os: MagicMock,
    _mock_s3: MagicMock,
    mock_worker_cls: MagicMock,
) -> None:
    """main() hands the worker a real client whose read timeout > BLOCK."""
    from ingestion.__main__ import main

    main()

    client = mock_worker_cls.call_args.kwargs["redis_client"]
    socket_timeout = client.connection_pool.connection_kwargs["socket_timeout"]
    assert socket_timeout is None or socket_timeout > DEFAULT_BLOCK_MS / 1000


# ---------------------------------------------------------------------------
# Startup guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("socket_timeout", "expect_warning"),
    [(5, True), (2.5, True), (15.0, False), (None, False)],
)
def test_xreadgroup_timeout_run_warns_when_socket_timeout_below_block(
    socket_timeout: float | None,
    expect_warning: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """run() warns once at startup if the client would time out idle polls."""
    client = redis.Redis(host="127.0.0.1", port=1, socket_timeout=socket_timeout)
    worker = _worker(client)
    worker.health_check = MagicMock()
    worker._ensure_consumer_group = MagicMock()
    worker._cleanup_stale_consumers = MagicMock()
    worker._reclaim_pending_messages = MagicMock(return_value=0)
    worker._process_batch = MagicMock(side_effect=KeyboardInterrupt)

    with caplog.at_level(logging.WARNING, logger="ingestion.worker"):
        worker.run(block_ms=DEFAULT_BLOCK_MS)

    warned = [r for r in caplog.records if "socket_timeout" in r.getMessage()]
    assert bool(warned) is expect_warning


def test_xreadgroup_timeout_run_guard_tolerates_mock_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guard must not crash on clients without real connection kwargs."""
    worker = _worker(MagicMock())
    worker.health_check = MagicMock()
    worker._ensure_consumer_group = MagicMock()
    worker._cleanup_stale_consumers = MagicMock()
    worker._reclaim_pending_messages = MagicMock(return_value=0)
    worker._process_batch = MagicMock(side_effect=KeyboardInterrupt)

    with caplog.at_level(logging.WARNING, logger="ingestion.worker"):
        worker.run()

    assert not [r for r in caplog.records if "socket_timeout" in r.getMessage()]
