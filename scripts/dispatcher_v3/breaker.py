"""dispatcher-v3 overnight-safety circuit breaker.

Mirrors v2's overnight-safety breaker (see
``scripts/dispatcher/daemon.py:_evaluate_circuit_breaker`` /
``_check_circuit_breaker_auto_close``, issues #2860 / #3779) but
**scoped strictly to v3 terminal outcomes** via the
``terminal_outcomes.parent_run_id`` column landed by A1 (#3893) +
migration 56.

Why a separate module rather than methods on ``Launcher``: the breaker
logic is ~200 lines of pure DB + alert plumbing, and the launcher
is already past the ~700 LOC v3 spec target. Keeping it in
``breaker.py`` (a) makes the v2 → v3 mapping legible (one v2 daemon
method per v3 module function), (b) reduces the launcher's surface
area for the next phase's edits, and (c) keeps the unit-test seam
narrow: ``BreakerEvaluator(conn=fake)`` is testable without spinning
up a full :class:`Launcher`.

Trigger pattern (matches v2 verbatim):

1. **After every terminal write**, the launcher calls
   :meth:`BreakerEvaluator.record_and_evaluate` which inserts a row
   into ``dispatcher.terminal_outcomes`` (with ``parent_run_id`` set
   to the agent's run lineage) and immediately scans the rolling
   v3-scoped window. If the bad-outcome count meets the threshold,
   ``concurrency_cap_v3`` is flipped to 0, ``cap_flipped_by_v3`` is
   stamped, and a Telegram alert fires.
2. **Each tick**, the launcher calls
   :meth:`BreakerEvaluator.maybe_auto_close` which restores the cap
   when (a) the operator manually re-raised it (operator-reflip
   path), or (b) the bad-outcome window has fully rolled and the
   current bad-count is below threshold (time-based path; #3779
   prevents the closed-feedback-loop deadlock where cap=0 means no
   new outcomes which means bad_count never refreshes).

Independence from v2's breaker:

- v3 reads/writes ``concurrency_cap_v3`` (not ``concurrency_cap``).
  Each daemon's cap is independent; a v3 trip cannot kill v2's claim
  loop and vice versa.
- v3's window scan filters by ``V3_SCOPED_PARENT_RUN_FILTER`` (the
  same shape v2 uses, swapping the version literal). v2's outcomes
  never count toward v3's threshold.
- Config knobs are shared (``circuit_breaker_window_minutes``,
  ``_window_size``, ``_bad_outcome_threshold``,
  ``circuit_breaker_enabled``) — operator policy is daemon-agnostic.
  The daemon-specific cap key is the only divergence.
- Cap-flipped-by trail uses a v3-specific key
  (``cap_flipped_by_v3``) so an admin cockpit can render distinct
  banners for v2 vs v3 trips.

Issue #3883.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Optional

from dispatcher_v3 import telegram

# ---------------------------------------------------------------------------
# Constants — mirror v2's overnight-safety knobs verbatim so a single
# operator-facing knob applies symmetrically.
# ---------------------------------------------------------------------------

#: Default rolling-window length (minutes) when ``dispatcher.config``
#: does not set ``circuit_breaker_window_minutes``. Matches v2's
#: ``DEFAULT_OVERNIGHT_CB_WINDOW_MINUTES`` and the migration-29 seed.
#:
#: Issue #3883's spec text says "1h" but the established v2 default
#: is 30 minutes (and the issue body says "2-of-3 in last 1h"). We
#: keep the v2 default of 30 here because (a) operator-facing knobs
#: are seeded by migration 29 and operators have already tuned them,
#: and (b) the breaker accepts any window length the operator
#: chooses. The "1h" in the issue body is the spec's example; the
#: implementation reads the live config.
DEFAULT_OVERNIGHT_CB_WINDOW_MINUTES = 30

#: Default rolling-window size (N in M-of-N). Mirrors v2.
DEFAULT_OVERNIGHT_CB_WINDOW_SIZE = 10

#: Default bad-outcome threshold (M in M-of-N). Mirrors v2.
DEFAULT_OVERNIGHT_CB_BAD_OUTCOME_THRESHOLD = 5

#: Issue #3883 example: "2-of-3 in 1h". An operator who wants the
#: spec-text default rather than v2's seeded value can flip
#: ``circuit_breaker_v3_window_size`` and
#: ``circuit_breaker_v3_bad_outcome_threshold`` independently. The
#: dedicated v3 keys are checked first; if absent, the breaker falls
#: back to the shared v2 keys (so a fresh DB without v3-specific
#: tuning still trips on the v2-seeded 5-of-10).
V3_CB_WINDOW_SIZE_KEY = "circuit_breaker_v3_window_size"
V3_CB_THRESHOLD_KEY = "circuit_breaker_v3_bad_outcome_threshold"
V3_CB_WINDOW_MINUTES_KEY = "circuit_breaker_v3_window_minutes"

#: Set of "good" terminal statuses. Anything not in this set counts
#: as bad — same tolerant-classifier contract as v2 (#2860). A future
#: correct-outcome terminal (e.g. ``ci_unrecoverable``) is
#: conservatively counted as bad until added here.
OVERNIGHT_CB_GOOD_OUTCOME_STATUSES: frozenset[str] = frozenset({"succeeded"})

#: Sentinel value for the v3-specific ``cap_flipped_by_v3`` config
#: row. The auto-close path looks for this exact value to decide
#: whether a cap flip back to ≥1 was operator-initiated (clear flag)
#: vs the breaker's own flip (no-op — already flipped).
CAP_FLIPPED_BY_V3_CIRCUIT_BREAKER = "circuit_breaker_v3"

#: Default v3 target cap when the breaker auto-closes (#3779 pattern).
#: Operators tune this via ``target_concurrency_cap_v3``; falls back
#: to 1 to match the legacy v2 ``start`` semantics.
DEFAULT_TARGET_CONCURRENCY_CAP_V3 = 1

#: V3 run-scope filter used in every breaker scan. Mirrors v2's
#: ``V2_SCOPED_PARENT_RUN_FILTER`` shape, swapping the literal so v2
#: terminal outcomes are excluded from v3's threshold count.
V3_SCOPED_PARENT_RUN_FILTER = (
    "parent_run_id IN ("
    "SELECT run_id FROM dispatcher.runs WHERE dispatcher_version = 'v3'"
    ")"
)

log = logging.getLogger("dispatcher_v3.breaker")


# ---------------------------------------------------------------------------
# Telegram-alert seam — patched by tests
# ---------------------------------------------------------------------------


def _default_telegram_alerter(message: str, *, trigger: str, run_id: str) -> None:
    """Default Telegram alert hook — wraps :func:`telegram.send_alert`.

    Tests inject a no-op or a recorder; production code uses this
    default so the breaker module remains decoupled from the wider
    process. The wrapper swallows the (returncode, event_name) tuple
    that ``send_alert`` returns — best-effort by design.
    """
    telegram.send_alert(message=message, trigger=trigger, run_id=run_id)


# ---------------------------------------------------------------------------
# BreakerEvaluator — the public surface used by the launcher.
# ---------------------------------------------------------------------------


class BreakerEvaluator:
    """v3 overnight-safety circuit breaker.

    Constructed once per launcher instance with the live psycopg
    connection + run_id. Each tick the launcher invokes:

    - :meth:`record_and_evaluate` after marking an agent terminal
      (success or failure). Writes the outcome row, then evaluates
      the rolling window and trips the breaker when the threshold
      is met.
    - :meth:`maybe_auto_close` once per tick (independent of any
      terminal transition). Restores ``concurrency_cap_v3`` when the
      eligibility conditions hold.
    """

    def __init__(
        self,
        *,
        conn: Any,
        run_id: str,
        telegram_alerter: Callable[..., None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._run_id = run_id
        self._telegram_alerter = telegram_alerter or _default_telegram_alerter
        # ``clock`` is injected by tests that want to control "now"
        # for the time-based auto-close path. Defaults to UTC-aware
        # ``datetime.now(UTC)``.
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- public surface ----------------------------------------------------

    def record_and_evaluate(self, *, agent_id: str, status: str) -> bool:
        """Append a terminal-outcome row and evaluate the breaker.

        Returns True iff the breaker opened (or re-asserted an
        already-open state) on this call. Idempotent: a second call
        for the same agent/status pair appends a second row (it is a
        log of transitions, not a uniqueness gate) and re-runs
        evaluation, but the cap flip is a no-op when the cap is
        already 0.

        The outcome row carries ``agent_id``, ``issue_number`` (looked
        up from ``dispatcher.agents``), ``status`` (free-form text),
        and ``parent_run_id`` (the v3 run lineage from the agent
        row). Synthetic test inputs without a matching agent row land
        with NULL ``issue_number`` and NULL ``parent_run_id`` — the
        breaker tolerates both today (v2 ignores the column entirely;
        v3's window scan excludes NULLs via the
        ``V3_SCOPED_PARENT_RUN_FILTER`` subquery).
        """
        if self._conn is None:
            return False
        self._write_terminal_outcome(agent_id=agent_id, status=status)
        return self._evaluate(agent_id=agent_id)

    def maybe_auto_close(self) -> bool:
        """Restore ``concurrency_cap_v3`` when the breaker should close.

        Two close paths converge here:

        1. **Operator-reflip.** When ``concurrency_cap_v3 >= 1`` AND
           ``cap_flipped_by_v3 == 'circuit_breaker_v3'``, the operator
           has explicitly raised the cap. Clear the flag (so a future
           operator-initiated flip is not mis-attributed to the
           breaker) and emit ``daemon.circuit_breaker_v3_closed``.
        2. **Time-based.** When ``concurrency_cap_v3 == 0`` AND
           ``cap_flipped_by_v3 == 'circuit_breaker_v3'`` AND the
           bad-outcome window has fully rolled AND the current
           bad-count is below threshold, restore cap to
           ``target_concurrency_cap_v3`` (default 1) and clear the
           flag. Mirrors v2's #3779 pattern.

        Returns True when the breaker auto-closed this call; False
        when the conditions weren't met. The hot path
        (``cap_flipped_by_v3`` null or not-the-breaker) is a single
        SELECT + early return, so this is cheap to call every tick.
        """
        if self._conn is None:
            return False
        flipped_by = self._read_cap_flipped_by_v3()
        if flipped_by != CAP_FLIPPED_BY_V3_CIRCUIT_BREAKER:
            return False
        current_cap = self._read_int_config("concurrency_cap_v3", -1)
        if current_cap >= 1:
            return self._auto_close_clear_flag(current_cap)
        # current_cap < 1 → time-based path.
        if not self._cb_enabled():
            return False
        cap_updated_at = self._read_cap_updated_at("concurrency_cap_v3")
        if cap_updated_at is None:
            return False
        window_minutes = self._read_window_minutes()
        if not self._window_elapsed(cap_updated_at, window_minutes):
            return False
        window_size = self._read_window_size()
        threshold = self._read_threshold()
        bad_count = self._current_bad_count(window_minutes, window_size)
        if bad_count is None:
            return False
        if bad_count >= threshold:
            return False
        # Eligible — restore cap and clear flag.
        target_cap = self._read_int_config(
            "target_concurrency_cap_v3", DEFAULT_TARGET_CONCURRENCY_CAP_V3
        )
        if target_cap < 1:
            target_cap = DEFAULT_TARGET_CONCURRENCY_CAP_V3
        if not self._set_cap(target_cap, "circuit_breaker_v3_auto_close"):
            return False
        if not self._clear_cap_flipped_by_v3("circuit_breaker_v3_auto_close"):
            # Cap restore is the safety action; flag clear is diagnostic.
            # Continue to log the auto-close.
            pass
        log.info(
            "daemon.circuit_breaker_v3_auto_closed",
            extra={
                "event": "circuit_breaker_v3_auto_closed",
                "run_id": self._run_id,
                "previous_cap": current_cap,
                "new_cap": target_cap,
                "bad_count": bad_count,
                "threshold": threshold,
                "window_minutes": window_minutes,
                "window_size": window_size,
            },
        )
        return True

    # -- internals: terminal_outcomes write -------------------------------

    def _write_terminal_outcome(self, *, agent_id: str, status: str) -> None:
        """Insert one row into ``dispatcher.terminal_outcomes``.

        Looks up ``issue_number`` and ``parent_run_id`` from the agent
        row so the outcome is self-contained (no JOIN at scan time).
        Mirrors v2's ``_write_terminal_outcome`` (#3872 / A1 #3893
        added the ``parent_run_id`` column; #3876 + this issue use
        it as the v3-vs-v2 scope discriminator).
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT issue_number, parent_run_id "
                    "FROM dispatcher.agents WHERE agent_id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
                if row is not None:
                    issue_number = int(row[0]) if row[0] is not None else None
                    parent_run_id = str(row[1]) if row[1] is not None else None
                else:
                    issue_number = None
                    parent_run_id = None
                cur.execute(
                    "INSERT INTO dispatcher.terminal_outcomes "
                    "    (agent_id, issue_number, status, parent_run_id, "
                    "     ended_at) "
                    "VALUES (%s, %s, %s, %s, now())",
                    (agent_id, issue_number, status, parent_run_id),
                )
            self._conn.commit()
        except Exception:
            log.exception(
                "daemon.terminal_outcome_write_failed_v3",
                extra={
                    "event": "terminal_outcome_write_failed_v3",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                    "status": status,
                },
            )
            self._safe_rollback()

    # -- internals: breaker evaluation ------------------------------------

    @staticmethod
    def is_bad_outcome(status: str) -> bool:
        """Tolerant classifier — anything not in the good-set is bad.

        Mirrors v2's ``_is_bad_outcome``. Erring on the side of
        opening the breaker on an unknown new status is the safer
        bias for overnight operation.
        """
        return status not in OVERNIGHT_CB_GOOD_OUTCOME_STATUSES

    def _evaluate(self, *, agent_id: str) -> bool:
        """Trip the v3 breaker if the streak is bad.

        Returns True when the breaker opened (or re-asserted an
        already-open state); False when the threshold was not met or
        the breaker is disabled via ``circuit_breaker_enabled``.
        """
        if not self._cb_enabled():
            return False
        window_minutes = self._read_window_minutes()
        window_size = self._read_window_size()
        threshold = self._read_threshold()
        if window_size <= 0 or threshold <= 0:
            return False
        statuses = self._scan_recent_statuses(window_minutes, window_size)
        if statuses is None:
            return False
        bad_count = sum(1 for s in statuses if self.is_bad_outcome(s))
        if bad_count < threshold:
            return False
        # Threshold met. Flip ``concurrency_cap_v3`` to 0 and stamp
        # ``cap_flipped_by_v3``. Two UPDATEs so a partial failure on
        # the flag write doesn't roll back the cap flip — the flip is
        # the safety action.
        current_cap = self._read_int_config("concurrency_cap_v3", -1)
        if not self._set_cap(0, CAP_FLIPPED_BY_V3_CIRCUIT_BREAKER):
            return False
        # The flag write is best-effort: log on failure but continue.
        if not self._set_cap_flipped_by_v3(
            CAP_FLIPPED_BY_V3_CIRCUIT_BREAKER, CAP_FLIPPED_BY_V3_CIRCUIT_BREAKER
        ):
            log.warning(
                "daemon.circuit_breaker_v3_flag_failed",
                extra={
                    "event": "circuit_breaker_v3_flag_failed",
                    "run_id": self._run_id,
                    "agent_id": agent_id,
                },
            )

        was_already_open = current_cap == 0
        log.warning(
            "daemon.circuit_breaker_v3_opened",
            extra={
                "event": "circuit_breaker_v3_opened",
                "run_id": self._run_id,
                "agent_id": agent_id,
                "bad_count": bad_count,
                "window_size": window_size,
                "threshold": threshold,
                "window_minutes": window_minutes,
                "previous_cap": current_cap,
                "was_already_open": was_already_open,
                "recent_statuses": statuses,
            },
        )
        if not was_already_open:
            try:
                self._telegram_alerter(
                    telegram.render_breaker_opened(
                        bad_count=bad_count,
                        window_size=window_size,
                        window_minutes=window_minutes,
                        statuses=statuses,
                    ),
                    trigger="breaker_opened",
                    run_id=self._run_id,
                )
            except Exception:
                log.exception(
                    "daemon.circuit_breaker_v3_telegram_failed",
                    extra={
                        "event": "circuit_breaker_v3_telegram_failed",
                        "run_id": self._run_id,
                        "agent_id": agent_id,
                    },
                )
        return True

    def _scan_recent_statuses(
        self, window_minutes: int, window_size: int
    ) -> Optional[list[str]]:
        """Return statuses of the most recent v3-scoped outcomes (newest first).

        ``None`` on DB error — caller treats as "no trip this call".
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT t.status "
                    "FROM dispatcher.terminal_outcomes t "
                    "WHERE t.ended_at > now() - make_interval(mins => %s) "
                    f"  AND t.{V3_SCOPED_PARENT_RUN_FILTER} "
                    "ORDER BY t.ended_at DESC "
                    "LIMIT %s",
                    (window_minutes, window_size),
                )
                rows = cur.fetchall()
            self._conn.commit()
        except Exception:
            log.exception(
                "daemon.circuit_breaker_v3_scan_failed",
                extra={
                    "event": "circuit_breaker_v3_scan_failed",
                    "run_id": self._run_id,
                },
            )
            self._safe_rollback()
            return None
        return [str(r[0]) for r in rows if r and r[0] is not None]

    def _current_bad_count(
        self, window_minutes: int, window_size: int
    ) -> Optional[int]:
        """Count bad outcomes in the current rolling v3 window."""
        statuses = self._scan_recent_statuses(window_minutes, window_size)
        if statuses is None:
            return None
        return sum(1 for s in statuses if self.is_bad_outcome(s))

    # -- internals: config reads ------------------------------------------

    def _cb_enabled(self) -> bool:
        """Read ``dispatcher.config.circuit_breaker_enabled`` (default True)."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("circuit_breaker_enabled",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return True
        if row is None or row[0] is None:
            return True
        raw = row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return True
        if isinstance(raw, bool):
            return raw
        return True

    def _read_int_config(self, key: str, default: int) -> int:
        """Read an integer config row; default on missing/malformed."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    (key,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return default
        if row is None or row[0] is None:
            return default
        raw = row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return default
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return default
        return n

    def _read_window_minutes(self) -> int:
        """Read v3-specific key first, fall back to shared v2 key."""
        v3_value = self._read_int_config(V3_CB_WINDOW_MINUTES_KEY, -1)
        if v3_value > 0:
            return v3_value
        return self._read_int_config(
            "circuit_breaker_window_minutes", DEFAULT_OVERNIGHT_CB_WINDOW_MINUTES
        )

    def _read_window_size(self) -> int:
        v3_value = self._read_int_config(V3_CB_WINDOW_SIZE_KEY, -1)
        if v3_value > 0:
            return v3_value
        return self._read_int_config(
            "circuit_breaker_window_size", DEFAULT_OVERNIGHT_CB_WINDOW_SIZE
        )

    def _read_threshold(self) -> int:
        v3_value = self._read_int_config(V3_CB_THRESHOLD_KEY, -1)
        if v3_value > 0:
            return v3_value
        return self._read_int_config(
            "circuit_breaker_bad_outcome_threshold",
            DEFAULT_OVERNIGHT_CB_BAD_OUTCOME_THRESHOLD,
        )

    def _read_cap_flipped_by_v3(self) -> str | None:
        """Read ``dispatcher.config.cap_flipped_by_v3``.

        Mirrors v2's ``_read_cap_flipped_by`` JSONB-handling shape so
        a JSONB string ``"circuit_breaker_v3"`` is unwrapped to the
        Python string ``"circuit_breaker_v3"``.
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM dispatcher.config WHERE key = %s",
                    ("cap_flipped_by_v3",),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return None
        if row is None or row[0] is None:
            return None
        raw = row[0]
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            if isinstance(decoded, str):
                return decoded
            return None
        return None

    def _read_cap_updated_at(self, key: str) -> Optional[datetime]:
        """Read ``dispatcher.config.<key>.updated_at`` (UTC-aware)."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT updated_at FROM dispatcher.config WHERE key = %s",
                    (key,),
                )
                row = cur.fetchone()
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return None
        if row is None or row[0] is None:
            return None
        ts = row[0]
        # Test fixtures may stage ``(value, updated_at)`` tuples for
        # callers that read both columns at once. Detect that case
        # and pick off the timestamp from index 1.
        if (
            isinstance(row, tuple)
            and len(row) >= 2
            and not isinstance(row[0], datetime)
        ):
            ts = row[1]
        if ts is None or not isinstance(ts, datetime):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts

    def _window_elapsed(self, cap_updated_at: datetime, window_minutes: int) -> bool:
        """True iff ``window_minutes`` have elapsed since cap_updated_at."""
        if window_minutes <= 0:
            return False
        elapsed = self._clock() - cap_updated_at
        return elapsed >= timedelta(minutes=window_minutes)

    # -- internals: config writes -----------------------------------------

    def _set_cap(self, target: int, updated_by: str) -> bool:
        """UPDATE ``concurrency_cap_v3`` to *target*. Returns success."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.config (key, value, updated_at, "
                    "    updated_by) "
                    "VALUES (%s, %s, now(), %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                    "    updated_at = now(), updated_by = EXCLUDED.updated_by",
                    ("concurrency_cap_v3", str(target), updated_by),
                )
            self._conn.commit()
        except Exception:
            log.exception(
                "daemon.circuit_breaker_v3_flip_failed",
                extra={
                    "event": "circuit_breaker_v3_flip_failed",
                    "run_id": self._run_id,
                    "target": target,
                },
            )
            self._safe_rollback()
            return False
        return True

    def _set_cap_flipped_by_v3(self, value: str, updated_by: str) -> bool:
        """UPSERT ``cap_flipped_by_v3`` (JSONB-encoded string)."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.config (key, value, updated_at, "
                    "    updated_by) "
                    "VALUES (%s, %s, now(), %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                    "    updated_at = now(), updated_by = EXCLUDED.updated_by",
                    ("cap_flipped_by_v3", json.dumps(value), updated_by),
                )
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return False
        return True

    def _clear_cap_flipped_by_v3(self, updated_by: str) -> bool:
        """Set ``cap_flipped_by_v3`` to JSONB null."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO dispatcher.config (key, value, updated_at, "
                    "    updated_by) "
                    "VALUES (%s, 'null', now(), %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = 'null', "
                    "    updated_at = now(), updated_by = EXCLUDED.updated_by",
                    ("cap_flipped_by_v3", updated_by),
                )
            self._conn.commit()
        except Exception:
            self._safe_rollback()
            return False
        return True

    def _auto_close_clear_flag(self, current_cap: int) -> bool:
        """Operator-reflip path: clear ``cap_flipped_by_v3``, log, return True."""
        if not self._clear_cap_flipped_by_v3("circuit_breaker_v3_auto_close"):
            return False
        log.info(
            "daemon.circuit_breaker_v3_closed",
            extra={
                "event": "circuit_breaker_v3_closed",
                "run_id": self._run_id,
                "new_cap": current_cap,
            },
        )
        return True

    # -- shared helpers ----------------------------------------------------

    def _safe_rollback(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.rollback()
        except Exception:  # pragma: no cover — best-effort
            pass
