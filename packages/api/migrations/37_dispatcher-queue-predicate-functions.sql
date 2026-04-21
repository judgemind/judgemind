-- Up Migration
--
-- Dispatcher v2 — Phase 3001. SQL predicate functions for queue filtering.
--
-- Two STABLE helper functions in the ``dispatcher`` schema used by both
-- the Python daemon (``scripts/dispatcher/daemon.py``) and the TypeScript
-- resolver (``packages/api/src/graphql/dispatcher/resolvers.ts``).
-- Centralising the predicates in SQL eliminates the inline
-- ``status = ANY(...)`` and ``started_at > now() - make_interval(secs => …)``
-- query fragments from the daemon, and enables the TS resolver to batch-query
-- them efficiently via an ``unnest($2::int[])`` lateral so there is only one
-- DB round-trip for the entire queue-snapshot list.
--
-- Functions
-- ---------
-- 1. dispatcher.issue_has_active_agent(p_issue_number integer) RETURNS boolean
--    Returns TRUE when ``dispatcher.agents`` has any row for this issue
--    with status IN ('running', 'retrying', 'succeeded', 'needs_review').
--    This mirrors ``ACTIVE_AGENT_STATUSES`` in daemon.py (line 354), which
--    must stay in sync.  If the SQL definition drifts, the contract test in
--    ``packages/api/tests/dispatcher.integration.test.ts`` will fail.
--
-- 2. dispatcher.issue_cooldown_remaining_seconds(
--        p_issue_number integer,
--        p_cooldown_seconds integer
--    ) RETURNS integer
--    Returns the number of seconds remaining in the cooldown window keyed on
--    MAX(started_at) for any prior agent row for this issue.  Returns NULL
--    when there is no prior row (never attempted).  Returns 0 when the
--    cooldown has fully elapsed (GREATEST keeps it non-negative).
--
-- Both functions are marked STABLE (no writes; repeated calls with the same
-- args inside a transaction return the same result) so the planner can
-- optimise batch invocations.

CREATE OR REPLACE FUNCTION dispatcher.issue_has_active_agent(
    p_issue_number integer
) RETURNS boolean
    LANGUAGE sql
    STABLE
AS $$
    SELECT EXISTS (
        SELECT 1
          FROM dispatcher.agents
         WHERE issue_number = p_issue_number
           AND status IN ('running', 'retrying', 'succeeded', 'needs_review')
    );
$$;

COMMENT ON FUNCTION dispatcher.issue_has_active_agent(integer) IS
    'Returns TRUE when dispatcher.agents has any row for this issue with status '
    'IN (''running'', ''retrying'', ''succeeded'', ''needs_review''). '
    'Mirrors ACTIVE_AGENT_STATUSES in scripts/dispatcher/daemon.py:354. '
    'Issue #3001.';

CREATE OR REPLACE FUNCTION dispatcher.issue_cooldown_remaining_seconds(
    p_issue_number integer,
    p_cooldown_seconds integer
) RETURNS integer
    LANGUAGE sql
    STABLE
AS $$
    SELECT GREATEST(0,
        p_cooldown_seconds - EXTRACT(EPOCH FROM (now() - MAX(started_at)))::integer
    )
      FROM dispatcher.agents
     WHERE issue_number = p_issue_number
    HAVING MAX(started_at) IS NOT NULL;
$$;

COMMENT ON FUNCTION dispatcher.issue_cooldown_remaining_seconds(integer, integer) IS
    'Returns the seconds remaining in the cooldown window keyed on '
    'MAX(started_at) for any prior agent row for this issue. '
    'NULL when there is no prior row (never attempted). '
    '0 when cooldown has elapsed (GREATEST keeps it non-negative). '
    'Issue #3001.';


-- Down Migration
DROP FUNCTION IF EXISTS dispatcher.issue_cooldown_remaining_seconds(integer, integer);
DROP FUNCTION IF EXISTS dispatcher.issue_has_active_agent(integer);
