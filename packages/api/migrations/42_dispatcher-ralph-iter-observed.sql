-- Up Migration
--
-- Dispatcher v2 — ECS agent-runner HEAD-watcher observability (#3144).
--
-- Motivation
-- ----------
-- The ECS per-agent runner (``scripts/dispatcher/agent-runner-entrypoint.sh``)
-- was shipping long ralph phases (60-120 min) to production with zero
-- iteration-level observability: between ``claude_phase_begin`` and
-- ``claude_phase_done`` CloudWatch showed nothing, and
-- ``dispatcher.ralph_patches`` had no rows (the daemon's
-- ``_start_ralph_head_watcher`` thread only runs in subprocess mode, not
-- in ECS mode). On 2026-04-23 agent ``6e79fb52`` on #2671 spent 85+
-- minutes in ralph with no visibility whatsoever. See #3144 for the
-- incident narrative.
--
-- The ECS-side fix (#3144) adds a bash subshell to the entrypoint that
-- mirrors the daemon's HEAD-watcher semantics: poll ``git log
-- origin/main..HEAD`` every 30s, and for each new commit emit a log
-- event + INSERT a ``dispatcher.ralph_patches`` row + UPDATE a new
-- counter column on ``dispatcher.agents``.
--
-- Schema change
-- -------------
-- One additive column:
--
-- * ``dispatcher.agents.ralph_iterations_observed INT NOT NULL
--   DEFAULT 0`` — atomically bumped by the ECS HEAD-watcher when it
--   persists a new ralph_patches row. Admin / fleet-status queries can
--   read this column directly without joining ``ralph_patches``. The
--   daemon's subprocess-mode watcher (#3042) does not update this
--   column today — its ralph_patches rows are the authoritative count
--   for that path — so a subprocess agent keeps ``ralph_iterations_
--   observed = 0``. Future unification would either back-fill on
--   daemon-side persist or drop the column once ECS mode is the only
--   runtime.
--
-- Non-change: we considered adding a partial unique index on
-- ``dispatcher.ralph_patches (agent_id, iteration_n)`` so the ECS
-- watcher could use ``ON CONFLICT (agent_id, iteration_n) DO NOTHING``.
-- We opted NOT to, because the daemon's existing
-- ``_persist_ralph_iteration_patch`` (in subprocess mode) inserts
-- without ON CONFLICT handling — a new uniqueness constraint would
-- retroactively turn a rare double-emit race into a hard failure on
-- the daemon side. #3144's constraint "no daemon.py changes" locks us
-- to the safer ECS-side alternative: a SELECT-then-INSERT guard in the
-- HEAD-watcher subshell. A future ticket can unify the two paths once
-- the daemon can also tolerate the constraint.

ALTER TABLE dispatcher.agents
    ADD COLUMN ralph_iterations_observed INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN dispatcher.agents.ralph_iterations_observed IS
    'Count of ralph iterations observed by the ECS agent-runner HEAD-watcher (#3144). Atomically UPDATEd per iteration when the watcher persists a new dispatcher.ralph_patches row. Subprocess-mode agents keep this at 0 — the daemon-side watcher (#3042) persists ralph_patches but does not update this column. Issue #3144.';


-- Down Migration
ALTER TABLE dispatcher.agents DROP COLUMN IF EXISTS ralph_iterations_observed;
