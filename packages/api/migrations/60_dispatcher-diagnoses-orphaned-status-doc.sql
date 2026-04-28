-- Up Migration
--
-- Dispatcher v2 — document all four lifecycle values of
-- ``dispatcher.diagnoses.status`` (issue #3392).
--
-- Context
-- -------
-- PR #3388 (issue #3383) introduced the ``orphaned`` status value so that
-- boot-reap of a previous daemon's pending rows does not trip the 24h
-- fallback-rate circuit breaker. Orphaned rows are excluded from the
-- breaker because the diagnoser never executed — they are infrastructure
-- artifacts, not diagnoser quality signals. The column remains a plain
-- TEXT with no enum / CHECK constraint (migration 21). This migration
-- adds the missing COMMENT ON COLUMN to document all four lifecycle
-- values in one canonical place.
--
-- Values
-- ------
--   pending   — row created, diagnoser not yet dispatched (initial DEFAULT).
--   completed — diagnoser ran to completion, recommendation/outcome written.
--   failed    — diagnoser ran but errored; counts toward the 24h fallback-rate
--               circuit breaker.
--   orphaned  — row abandoned by a previous daemon (boot-reap, watchdog kill,
--               restart cascade, issue #3383). Distinct from failed because
--               no diagnoser run completed; the circuit breaker excludes
--               these rows. See daemon.py::_mark_diagnosis_orphaned.

COMMENT ON COLUMN dispatcher.diagnoses.status IS
    '4-state lifecycle (issue #3392). '
    '"pending" — row created, awaiting dispatch (initial DEFAULT). '
    '"completed" — diagnoser ran to completion, recommendation/outcome written. '
    '"failed" — diagnoser ran but errored; counts toward 24h fallback-rate '
    'circuit breaker (issue #3383). '
    '"orphaned" — row abandoned by a previous daemon (boot-reap, watchdog kill, '
    'restart cascade); distinct from failed because no diagnoser run completed, '
    'so the breaker excludes these rows (issue #3383, '
    'daemon.py::_mark_diagnosis_orphaned).';


-- Down Migration
--
-- Restore prior state: no comment existed on this column before
-- this migration (verified: grep for COMMENT ON COLUMN dispatcher.diagnoses.status
-- returned zero hits before this PR).
COMMENT ON COLUMN dispatcher.diagnoses.status IS NULL;
