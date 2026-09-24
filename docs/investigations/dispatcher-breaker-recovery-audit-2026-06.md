# Dispatcher config-flag circuit-breaker recovery + alert audit (2026-06)

**Issue:** #4593 (Parent: #4586)
**Scope:** `scripts/dispatcher/daemon.py`
**Question:** Does every config-flag circuit breaker in the daemon have BOTH (a) a
recovery path and (b) a loud alert (Telegram) on trip?

## Background

The diagnoser circuit breaker (#4586) shipped with a one-way flip of
`dispatcher.config.diagnoser_enabled → false` and no recovery path: once tripped,
no diagnoses ran, so the breaker could never re-measure the fallback rate and
re-enable itself. It stayed off until an operator manually flipped it, silently
stranding 213 `ralph_not_ship` failures. This is the same design smell the
overnight-safety breaker had before #3779 added time-based auto-close.

Two breakers, both eventually needing an auto-recovery path AND a loud alert.
This audit confirms the post-#4586 / post-#3779 state and verifies the pattern
is exhaustively covered.

## Method

1. Grepped `daemon.py` for every `UPDATE dispatcher.config SET value = ...`
   kill-switch flip.
2. Grepped for every autonomous `updated_by = '...'` attribution string
   (the canonical signal of a breaker / recovery write, distinct from operator
   command handlers attributed `updated_by = 'daemon'`).
3. For each breaker, read the trip method, verified a paired recovery method
   exists AND is wired into the supervisor / scheduler tick loop, and verified a
   Telegram alert (`scripts/notify-telegram.sh` via `_send_*_telegram_alert`) is
   fired on trip.

## Findings

### Inventory of config-flag flips

`updated_by` attribution sweep (`grep -nE "updated_by = '[^']*(breaker|safety|auto|guard|kill|trip)"`)
returns exactly six autonomous-write sites, all belonging to the two known
breakers and their recovery paths:

| Line | `updated_by` | Role |
|------|--------------|------|
| 20130 | `diagnoser_circuit_breaker` | diagnoser trip (flip `diagnoser_enabled → false`) |
| 20210 | `diagnoser_circuit_breaker` | diagnoser trip-instant record (`diagnoser_breaker_tripped_at`) |
| 20361, 20369 | `diagnoser_circuit_breaker_auto_recover` | diagnoser recovery (flip `diagnoser_enabled → true`, clear trip-at) |
| 21190, 21216, 21266 | `circuit_breaker_auto_close` | overnight recovery (restore `concurrency_cap`, clear `cap_flipped_by`) |

The overnight breaker's trip flip (`concurrency_cap → 0`) is attributed via the
constant `CAP_FLIPPED_BY_CIRCUIT_BREAKER = "circuit_breaker"` (daemon.py:2347),
written at daemon.py:20843 / 20875.

All other `UPDATE dispatcher.config` sites (daemon.py:3966, 3981, 4020, 4154) are
**operator command handlers** (`_handle_start`, `_handle_stop`,
`_handle_force_stop`) attributed `updated_by = 'daemon'`. These are
operator-driven cap flips triggered by an explicit dispatcher command — not
autonomous circuit breakers that auto-trip on a measured condition — and are out
of scope for this audit. The killswitch (`_pause_requested`, an in-memory
`threading.Event`) is likewise operator-driven, not a config-flag breaker.

### Breaker 1 — Overnight-safety circuit breaker

- **Trip method:** `_evaluate_circuit_breaker` (daemon.py:20688), called
  post-terminal-transition from `_mark_agent_terminal` (daemon.py:9161, 10176).
- **Flag flipped:** `concurrency_cap → 0`, stamped `cap_flipped_by = 'circuit_breaker'`.
- **Recovery path:** `_check_circuit_breaker_auto_close` (daemon.py:21070) —
  TWO converging paths:
  - **Operator-reflip** (#2860): operator raises cap ≥ 1, flag cleared.
  - **Time-based auto-close** (#3779): after the bad-outcome window has rolled
    AND the current bad_count is back under threshold, restore
    `concurrency_cap → target_concurrency_cap` and clear the flag.
  Wired into the scheduler tick at daemon.py:3384.
- **Loud alert:** `_send_circuit_breaker_telegram_alert` (daemon.py:20940),
  fired on fresh opens at daemon.py:20923 via `scripts/notify-telegram.sh`.
- **Verdict: COMPLIANT** — has both a recovery path and a Telegram alert.

### Breaker 2 — Diagnoser circuit breaker

- **Trip method:** `_check_diagnoser_circuit_breaker` (daemon.py:20053), called
  once per supervisor tick at daemon.py:25770.
- **Flag flipped:** `diagnoser_enabled → false`; trip instant persisted to
  `diagnoser_breaker_tripped_at` via `_record_diagnoser_breaker_tripped_at`
  (daemon.py:20192).
- **Recovery path:** `_check_diagnoser_breaker_auto_recover` (daemon.py:20311) —
  time-bounded re-enable (#4586): when `now() - tripped_at` exceeds the recovery
  window, flip `diagnoser_enabled → true` and clear the trip timestamp. Wired
  into the supervisor tick at daemon.py:25760 (BEFORE the trip check at 25770,
  so a recovered breaker can re-measure the same tick).
- **Loud alert:** `_send_diagnoser_breaker_telegram_alert` (daemon.py:20400),
  fired on trip at daemon.py:20176 via `scripts/notify-telegram.sh`.
- **Verdict: COMPLIANT** — has both a recovery path and a Telegram alert.

## Conclusion

**All config-flag circuit breakers in `daemon.py` are compliant.** Both breakers
have a recovery path AND a Telegram alert on trip. No follow-up issues are
required (the expected outcome after #3779 + #4586).

There are exactly two config-flag circuit breakers; the `updated_by` attribution
sweep is exhaustive, and no third breaker exists.

## Preventative measure shipped with this audit

To stop the design smell from recurring with a future breaker, this audit ships a
hygiene guard — `scripts/check-dispatcher-breaker-recovery-alert.py`
(`.sh` wrapper) — wired into CI. The guard maintains a declared registry of
config-flag breakers (each with its trip method, recovery method, and Telegram
alert method) and fails the build when:

1. A kill-switch config flip in `daemon.py` is attributed to an `updated_by` /
   `cap_flipped_by` breaker tag that is not in the registry (a new breaker added
   without registering its recovery + alert), OR
2. A registered breaker's named recovery method or Telegram alert method does not
   exist in `daemon.py` (a breaker whose recovery or alert was removed / renamed).

This encodes acceptance criteria (1) recovery path and (2) loud alert as a
machine-checkable invariant so the next breaker cannot ship without both.
