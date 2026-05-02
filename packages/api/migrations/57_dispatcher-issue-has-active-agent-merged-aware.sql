-- Up Migration
--
-- Dispatcher merged-aware active-agent gate — Issue #3738 + #3001.
-- Fix ``dispatcher.issue_has_active_agent`` to ignore ``succeeded``
-- rows whose PR has already merged.
--
-- Problem
-- -------
-- Prior to this migration the function matched every ``status IN
-- ('running', 'retrying', 'succeeded', 'needs_review')`` row. A
-- ``succeeded`` row whose PR merged stays in the table indefinitely
-- (GC is retention-window-based, not event-based). Any new claim for
-- the same issue hits this row, ``issue_has_active_agent`` returns
-- TRUE, and the issue is permanently blocked from re-claim — even
-- though the prior work shipped successfully and the issue should be
-- re-claimable.
--
-- Fix
-- ---
-- Narrow the ``succeeded`` arm to only rows whose ``merged_at IS NULL``
-- (i.e. the PR has not yet merged). Once ``merged_at`` is stamped the
-- row no longer counts as an active blocker, so a new claim proceeds
-- immediately.  The daemon's ``_cleanup_stale_succeeded_rows``
-- housekeeping method closes the originating issue for the
-- already-merged rows; the SQL gate change alone is sufficient for
-- unblocking the re-claim.
--
-- The new gate:
--   status IN ('running', 'retrying', 'needs_review')
--   OR (status = 'succeeded' AND merged_at IS NULL)
--
-- ``ACTIVE_AGENT_STATUSES`` in ``scripts/dispatcher/daemon.py`` is
-- intentionally NOT changed — that tuple governs the Python-side
-- status membership check only; the SQL function is the single source
-- of truth for queue filtering.  The comment above
-- ``ACTIVE_AGENT_STATUSES`` is updated to note the divergence.
--
-- Issue #3738 + #3001.

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
           AND (
               status IN ('running', 'retrying', 'needs_review')
               OR (status = 'succeeded' AND merged_at IS NULL)
           )
    );
$$;

COMMENT ON FUNCTION dispatcher.issue_has_active_agent(integer) IS
    'Returns TRUE when dispatcher.agents has any row for this issue '
    'that is actively blocking re-claim: status IN (''running'', '
    '''retrying'', ''needs_review''), OR status=''succeeded'' with '
    'merged_at IS NULL (PR not yet merged). A succeeded row whose PR '
    'has merged (merged_at IS NOT NULL) is NOT counted — it no longer '
    'blocks re-claim. Diverges from ACTIVE_AGENT_STATUSES in '
    'scripts/dispatcher/daemon.py intentionally; see #3738. '
    'Issue #3738 + #3001.';


-- Down Migration
--
-- Restore the migration-37 body verbatim.  Down re-creates the
-- function with the original ``status IN (...)`` gate (no merged_at
-- awareness), exactly as written in
-- ``packages/api/migrations/37_dispatcher-queue-predicate-functions.sql``.

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
