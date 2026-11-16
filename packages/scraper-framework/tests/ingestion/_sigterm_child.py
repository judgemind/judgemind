"""Child process for ``test_sigterm_shutdown_5069``: runs a real
``IngestionWorker`` with SIGTERM handling against a fake Redis whose state
is written to a JSON file after every change, prints ``READY`` once the
scenario is set up, and waits for the parent's SIGTERM.

Usage: python _sigterm_child.py {idle|busy|slow} STATE_PATH
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any
from unittest.mock import MagicMock

from ingestion.worker import IngestionWorker


class FileRedis:
    def __init__(self, path: str, n: int) -> None:
        self.path = path
        self.entries = {
            f"{i}-0".encode(): {b"data": json.dumps({"document_id": f"doc-{i}"}).encode()}
            for i in range(1, n + 1)
        }
        self.unread = list(self.entries)
        self.pending: list[bytes] = []
        self.kv: dict[str, int] = {}
        self.on_idle: Any = None
        self._save()

    def _save(self) -> None:
        with open(self.path, "w") as fh:
            json.dump({"kv": self.kv, "pending": [p.decode() for p in self.pending]}, fh)

    def xreadgroup(self, group: str, consumer: str, streams: Any, **kw: Any) -> Any:
        if self.unread:
            ids, self.unread = self.unread, []
            self.pending.extend(ids)
            self._save()
            return [(b"document.captured", [(i, self.entries[i]) for i in ids])]
        if self.on_idle is not None:
            self.on_idle()
        time.sleep(kw.get("block", 100) / 1000)
        return []

    def xautoclaim(self, *args: Any, **kw: Any) -> Any:
        return (b"0-0", [], [])

    def xack(self, stream: str, group: str, msg_id: bytes) -> int:
        self.pending.remove(msg_id)
        self._save()
        return 1

    def hincrby(self, name: str, field: str, amount: int = 1) -> int:
        self.kv[field] = self.kv.get(field, 0) + amount
        self._save()
        return self.kv[field]

    def hdel(self, name: str, *fields: str) -> int:
        n = sum(1 for f in fields if self.kv.pop(f, None) is not None)
        self._save()
        return n

    def eval(self, script: str, numkeys: int, name: str, field: str) -> int:
        """Emulates ``_REFUND_CHARGE_LUA``: never below 0."""
        value = self.kv.get(field, 0)
        if value <= 1:
            self.kv.pop(field, None)
        else:
            self.kv[field] = value - 1
        self._save()
        return max(value - 1, 0)


def ready() -> None:
    print("READY", flush=True)


def main() -> None:
    mode, path = sys.argv[1], sys.argv[2]
    redis = FileRedis(path, 0 if mode == "idle" else 1)
    worker = IngestionWorker(
        redis_client=redis,
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    for name in (
        "health_check",
        "_ensure_consumer_group",
        "_cleanup_stale_consumers",
        "_maybe_trim_consumed_entries",
        "close",
    ):
        setattr(worker, name, MagicMock())

    def busy(event: dict[str, Any]) -> None:
        ready()
        while not worker._shutdown.requested:
            time.sleep(0.01)
        time.sleep(0.1)  # finishes well inside the grace period

    def slow(event: dict[str, Any]) -> None:
        ready()
        time.sleep(60)  # outlasts the grace period

    if mode == "idle":
        redis.on_idle = ready
    worker.process_event = {"busy": busy, "slow": slow}.get(mode, MagicMock())  # type: ignore[method-assign]
    worker.install_shutdown_handler()
    worker.run(block_ms=100)


if __name__ == "__main__":
    main()
