-- Up Migration
--
-- Dispatcher v2 — auto-unstick bookkeeping for the ``merge`` phase
-- (issue #2641). Adds one column to ``dispatcher.agents`` so the
-- daemon can bound the "stale-rollup unstick" recovery loop at
-- one attempt per agent lifetime.
--
-- Why a dedicated column (not ``retries_used`` or ``output_json``)
-- ---------------------------------------------------------------
-- * ``retries_used`` already counts fix-CI mechanical retries
--   (``FIX_CI_MAX_RETRIES``). Overloading it with merge-unstick
--   counts would conflate two independent budgets and break the
--   ``ci_red_after_retries`` tier-3 gate.
-- * ``output_json`` is a per-phase-output JSONB on a different
--   table (``dispatcher.phase_outputs``). Storing agent-lifetime
--   bookkeeping there would split a single counter across many
--   rows and complicate the "at most one unstick per agent"
--   predicate. The ``dispatcher.agents`` row is the natural home
--   for an agent-lifetime integer.
--
-- Behaviour
-- ---------
-- * DEFAULT 0 — every existing row (including in-flight agents mid-
--   deploy) is backfilled to 0, i.e. "no unstick attempted yet".
--   Matches the issue's "at most one attempt per agent lifetime"
--   retry budget: first stale-rollup rejection bumps to 1 and
--   succeeds (or routes via merge_unstick_exhausted if the push
--   itself failed); a second rejection trips the exhausted path.
-- * NOT NULL — the daemon always reads an integer, never a NULL.
-- * No index — this column is only read/written during the
--   merge-phase handler (one agent at a time, already filtered
--   by agent_id). Adding an index would be pure overhead.
--
-- Rollback
-- --------
-- Dropping the column is safe. The daemon tolerates its absence at
-- runtime: the merge-unstick code path degrades to "no retry
-- budget tracked", and on the rare stale-rollup occurrence the
-- auto-unstick simply re-attempts on every tick until GitHub
-- accepts the merge or an operator intervenes. This is the pre-
-- #2641 behaviour, unchanged for any pre-migration deploy.

ALTER TABLE dispatcher.agents
    ADD COLUMN merge_unstick_attempts INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN dispatcher.agents.merge_unstick_attempts IS
    'Count of stale-rollup merge-phase auto-unstick attempts for this '
    'agent (#2641). Bounded at 1 per agent lifetime: the first '
    'stale-rollup rejection (GitHub stderr "base branch policy '
    'prohibits the merge" while ci-passed on HEAD is SUCCESS) bumps '
    'this to 1 and the daemon pushes an empty commit to force a '
    'fresh rollup evaluation; a second rejection in the same agent''s '
    'lifetime routes through _handle_agent_failure with category '
    'merge_unstick_exhausted.';


-- Down Migration
ALTER TABLE dispatcher.agents
    DROP COLUMN IF EXISTS merge_unstick_attempts;
