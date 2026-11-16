"""Fake Redis hash commands for the ingestion worker's per-message charges
(``worker.MESSAGE_CHARGES_KEY``, #5055 / #5092).

Mix ``ChargesFake`` into a fake Redis client.  ``kv`` holds the hash's
fields (``crashes:<id>`` / ``errors:<id>``) as ints.  A fake that sets
``alive = False`` (a SIGKILLed worker) makes every write a no-op returning
``None``.  ``on_change`` is called after every write, for fakes that
persist their state.  ``_REFUND_CHARGE_LUA`` is emulated here and run for
real in ``ingestion/test_message_lease_5091.py::TestRealRedis``.
"""

from __future__ import annotations

from typing import Any

from ingestion import worker as worker_mod


def crash_field(msg_id: str) -> str:
    return f"{worker_mod.CRASH_FIELD_PREFIX}{msg_id}"


def error_field(msg_id: str) -> str:
    return f"{worker_mod.ERROR_FIELD_PREFIX}{msg_id}"


class ChargesFake:
    kv: dict[str, int]
    alive: bool = True

    def on_change(self) -> None:
        """Hook for fakes that persist their state."""

    @staticmethod
    def _field(field: Any) -> str:
        return field.decode() if isinstance(field, bytes) else str(field)

    def hincrby(self, name: str, field: Any, amount: int = 1) -> int | None:
        assert name == worker_mod.MESSAGE_CHARGES_KEY
        if not self.alive:
            return None
        key = self._field(field)
        self.kv[key] = self.kv.get(key, 0) + amount
        self.on_change()
        return self.kv[key]

    def hdel(self, name: str, *fields: Any) -> int:
        assert name == worker_mod.MESSAGE_CHARGES_KEY
        if not self.alive:
            return 0
        n = sum(1 for f in fields if self.kv.pop(self._field(f), None) is not None)
        self.on_change()
        return n

    def hscan_iter(self, name: str, **_kw: Any) -> Any:
        assert name == worker_mod.MESSAGE_CHARGES_KEY
        return iter([(k.encode(), str(v).encode()) for k, v in list(self.kv.items())])

    def eval_charge(self, script: str, numkeys: int, *args: Any) -> int | None:
        """Emulates ``_REFUND_CHARGE_LUA``."""
        assert script is worker_mod._REFUND_CHARGE_LUA
        assert numkeys == 1
        name, field = args
        assert name == worker_mod.MESSAGE_CHARGES_KEY
        if not self.alive:
            return None
        key = self._field(field)
        value = self.kv.get(key)
        if value is None or value <= 1:
            self.kv.pop(key, None)
            self.on_change()
            return 0
        self.kv[key] = value - 1
        self.on_change()
        return self.kv[key]

    def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        return self.eval_charge(script, numkeys, *args)

    # -- volatile-lru eviction --------------------------------------------------

    def expire(self, name: str, seconds: int) -> bool:
        """Records a TTL: ``evict_volatile`` then drops the key."""
        self.__dict__.setdefault("volatile", set()).add(name)
        return True

    def evict_volatile(self) -> None:
        """What ElastiCache's ``volatile-lru`` does under memory pressure:
        evict every key that has a TTL (#5092)."""
        if worker_mod.MESSAGE_CHARGES_KEY in self.__dict__.get("volatile", set()):
            self.kv.clear()
