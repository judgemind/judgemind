-- Up Migration
--
-- Dispatcher v2 — document the ``'orphaned'`` status value on
-- ``dispatcher.diagnoses.status`` (issue #3383).
--
-- Background
-- ----------
-- ``dispatcher.diagnoses.status`` (added in migration 21) is a free-text
-- TEXT column with default ``'pending'`` and no CHECK / enum constraint.
-- The original comment listed three values: ``pending | completed |
-- failed``. Issue #3383 introduces a fourth: ``'orphaned'``.
--
-- Why a new status, not a new ``failed`` reason
-- ---------------------------------------------
-- After PR #3378 (#3376 async diagnoser) shipped, the daemon-boot
-- orphan reaper (:py:meth:`_reap_orphaned_diagnoses_on_boot`)
-- transitioned ``status='pending'`` rows abandoned by a previous
-- daemon to ``status='failed'`` with reason
-- ``diagnoser_orphaned_by_daemon_restart``. But the diagnoser circuit
-- breaker (:py:meth:`_check_diagnoser_circuit_breaker`) counts
-- ``status='failed'`` rows in the last 24h as evidence that "the
-- diagnoser is making bad decisions". Orphans are NOT diagnoser
-- decisions — they are infrastructure artifacts (rows that were
-- never executed by the diagnoser before the parent daemon crashed).
--
-- On 2026-04-25, a daemon-restart cascade left 7 pending rows
-- abandoned. The boot reaper transitioned all 7 to ``'failed'``,
-- which gave the breaker a 7/7=100% historical fallback rate. The
-- breaker tripped at the 0.30 threshold, structurally disabling the
-- diagnoser. When the next real failure (#3380) hit
-- ``conflict_unresolvable`` later that night, the diagnoser
-- subprocess never spawned — breaker-gated. The empowered diagnoser
-- was disabled until the orphans aged out of the 24h window OR an
-- operator manually re-statused them.
--
-- The structural fix (Option B from the issue): use a distinct
-- ``'orphaned'`` status. The boot reaper writes ``'orphaned'`` (via
-- :py:meth:`_mark_diagnosis_orphaned`); the breaker query excludes
-- ``status='orphaned'`` rows from BOTH the failed-count numerator
-- AND the total-count denominator.
--
-- Why this migration, not a code-only change
-- ------------------------------------------
-- The status column is plain TEXT (no CHECK / enum), so the new
-- value is technically accepted by the DB without any migration —
-- the daemon code change alone unblocks the breaker. This migration
-- is the schema-doc complement: update the column comment so the
-- canonical value vocabulary (``pending | completed | failed |
-- orphaned``) is documented at the schema level. Future readers
-- (audits, replicas, BI tools) see the status enumeration without
-- having to grep daemon.py.
--
-- This migration intentionally does NOT backfill or modify any
-- existing rows. Operator-intervened rows on dev (diagnoses
-- #22–#28, manually re-statused to ``'orphaned'`` at 23:25Z on
-- 2026-04-25) remain untouched.

COMMENT ON COLUMN dispatcher.diagnoses.status IS
    'Diagnosis lifecycle: pending | completed | failed | orphaned. '
    'pending = subprocess in-flight. completed = recommendation '
    'consumed. failed = real diagnoser-run failure (timeout, '
    'malformed JSON, subprocess crash, unknown action) — counts '
    'against the circuit breaker. orphaned (#3383) = pending row '
    'reaped on daemon boot because the previous daemon crashed/'
    'restarted before the run completed; never executed by the '
    'diagnoser, so excluded from breaker fallback-rate.';


-- Down Migration
--
-- Restore the original column comment from migration 21. The
-- ``'orphaned'`` status value remains accepted (TEXT column has no
-- constraint), but the documentation reverts.

COMMENT ON COLUMN dispatcher.diagnoses.status IS
    'Diagnosis lifecycle: pending | completed | failed.';
