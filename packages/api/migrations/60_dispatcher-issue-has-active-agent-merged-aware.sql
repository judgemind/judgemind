-- Up Migration
--
-- Dispatcher — Issue #3738. Fix ``issue_has_active_agent`` so that a
-- ``succeeded`` row whose PR has *already merged* (``merged_at IS NOT NULL``)
-- no longer blocks the issue from being re-claimed.
--
-- Before this migration the function treated every succeeded row as blocking
-- regardless of whether the downstream PR had actually merged.  This caused
-- the ready queue to stall indefinitely when ``_write_merged_at`` wrote the
-- timestamp but the issue was never closed (e.g. missing ``Closes #N``
-- keyword).  Post-merge, the only state that still blocks re-claim is a
-- succeeded row whose merge has NOT yet been detected (``merged_at IS NULL``).
--
-- New gate:
--   TRUE  ↔  status IN ('running', 'retrying', 'needs_review')
--             OR (status = 'succeeded' AND merged_at IS NULL)
--   FALSE ↔  status = 'succeeded' AND merged_at IS NOT NULL
--             (PR merged — no longer an active agent; housekeeping will
--             auto-close the issue via _cleanup_stale_succeeded_rows)
--
-- Both branches use CREATE OR REPLACE so re-running the migration is safe.

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
    'Returns TRUE when dispatcher.agents has any row for this issue with '
    'status IN (''running'', ''retrying'', ''needs_review''), OR with '
    'status = ''succeeded'' AND merged_at IS NULL (PR not yet detected as merged). '
    'A succeeded row with merged_at IS NOT NULL no longer blocks re-claim — '
    'the housekeeping tick closes the stale issue via _cleanup_stale_succeeded_rows. '
    'Issue #3738 (prior definition: Issue #3001).';


-- Down Migration — restores the body from migration 37.

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
