-- Up Migration
--
-- Dispatcher v2 — async diagnoser spawn (issue #3376). Adds one column
-- to ``dispatcher.diagnoses``:
--
--   * ``subprocess_pid`` INTEGER — OS PID of the ``claude -p
--     /diagnose-failure`` subprocess. Set when the supervisor tick
--     fires the spawn (fire-and-forget); read by the supervisor tick's
--     reap pass on subsequent ticks to determine whether the diagnoser
--     subprocess has exited (then read recommendation + consume
--     directive) OR has exceeded its 90-min wall-clock budget (then
--     SIGTERM/SIGKILL + mark failed). NULL means the spawn hasn't
--     happened yet OR the process has already been reaped (i.e. the
--     row is in a terminal status).
--
-- Why this column
-- ---------------
-- Issue #3368 lifted the diagnoser's wall-clock budget from 5 min to
-- 90 min so the empowered diagnoser can run sub-skills (/tdd,
-- /task-v2-fix-conflict). The pre-#3376 spawn path called
-- ``proc.wait()`` synchronously inside ``supervisor_tick`` — but the
-- daemon's watchdog (``EXIT_THRESHOLD=120s``) calls ``os._exit(137)``
-- on any tick that exceeds 120s. Net: every diagnoser run >120s killed
-- the daemon and orphaned the diagnosis row at ``status='pending'``
-- forever. Three observed kills 20:14–20:34Z 2026-04-25.
--
-- The async fix (this migration) turns spawn into fire-and-forget +
-- a separate reaper pass. The reaper needs the subprocess PID so it
-- can:
--
--   1. ``os.kill(pid, 0)`` to check liveness without sending a signal.
--   2. ``os.kill(pid, SIGTERM)`` (then SIGKILL after grace) when the
--      diagnoser blows its 90-min budget.
--
-- Why a column not a sidecar table
-- --------------------------------
-- One PID per diagnosis row, lifecycle is identical to the row's own
-- pending → terminal transition. A sidecar table would require a JOIN
-- on every reap pass for no gain. The column is NULL for older rows
-- (pre-#3376 sync spawns) and for rows that never spawned (DB error
-- between INSERT and Popen — falls through to mechanical escalation).
--
-- Why INTEGER not BIGINT
-- ----------------------
-- Linux PIDs are bounded by ``/proc/sys/kernel/pid_max`` which on
-- modern systems caps at 4194304 (2²²) — well within INTEGER range.
-- macOS caps lower. No realistic deployment exceeds 2³¹.

ALTER TABLE dispatcher.diagnoses
    ADD COLUMN subprocess_pid INTEGER;

COMMENT ON COLUMN dispatcher.diagnoses.subprocess_pid IS
    'OS PID of the diagnoser subprocess (issue #3376). Written by '
    'supervisor_tick when it fires the async spawn; read by the '
    'reap pass to check liveness via os.kill(pid, 0) and to enforce '
    'the 90-min wall-clock budget. NULL means the row has not been '
    'spawned yet (or has already been reaped to a terminal status).';

-- Partial index supports the reaper''s ``WHERE status = ''pending''
-- AND subprocess_pid IS NOT NULL`` scan.
CREATE INDEX idx_dispatcher_diagnoses_pending_pid
    ON dispatcher.diagnoses (subprocess_pid)
    WHERE status = 'pending' AND subprocess_pid IS NOT NULL;


-- Down Migration
--
-- Drop the index first, then the column. Reverting reinstates the
-- pre-#3376 schema. The diagnoser path falls back to the synchronous
-- spawn behavior — but the watchdog cascade also returns, so this
-- down-migration is for true emergencies only.
DROP INDEX IF EXISTS dispatcher.idx_dispatcher_diagnoses_pending_pid;
ALTER TABLE dispatcher.diagnoses
    DROP COLUMN IF EXISTS subprocess_pid;
