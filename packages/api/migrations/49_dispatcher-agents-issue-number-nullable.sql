-- Up Migration
--
-- Dispatcher v2 — drop NOT NULL on ``dispatcher.agents.issue_number``
-- (issue #3380, follow-up to #3374).
--
-- Why
-- ---
-- The generalized scheduled-skills mechanism (#3374) introduces
-- synthetic agents with ``kind='scheduled_skill'`` for periodic skills
-- like ``/audit`` and ``/spotcheck``. These agents legitimately have
-- no issue — the skill itself decides what to act on (e.g. ``/audit``
-- reads recent merged PRs from GitHub).
--
-- The original schema (migration 21) declared ``issue_number INT NOT
-- NULL`` because every pre-#3374 agent was issue-driven. The
-- ``_spawn_scheduled_skill_agent`` path INSERTs ``NULL`` for
-- ``issue_number`` and trips the constraint, so the daemon's spawn
-- pass fails with::
--
--     psycopg.errors.NotNullViolation: null value in column
--     "issue_number" of relation "agents" violates not-null constraint
--
-- Dropping the constraint is the minimal change. Existing call sites
-- that read ``issue_number`` already tolerate ``NULL`` (the daemon's
-- ``_agent_issue_number`` returns ``int | None``); the only new path
-- where ``NULL`` shows up is the synthetic scheduled-skill agents,
-- which never enter the issue-driven plan→ralph→PR pipeline.
--
-- Rollback
-- --------
-- The DOWN migration re-adds the constraint, but only after replacing
-- existing NULL values with 0 (the synthetic agents would otherwise
-- block the constraint add). The ``UPDATE ... WHERE issue_number IS
-- NULL`` is idempotent — re-running it is a no-op once all rows have
-- a non-NULL value.

ALTER TABLE dispatcher.agents
    ALTER COLUMN issue_number DROP NOT NULL;

COMMENT ON COLUMN dispatcher.agents.issue_number IS
    'GitHub issue number for issue-driven agents. NULL for synthetic '
    'agents (kind=''scheduled_skill'') that have no issue context — '
    'the skill itself decides what to act on. See #3374 / #3380.';


-- Down Migration
--
-- Replace any NULL values with 0 (sentinel) before re-adding the
-- constraint. Synthetic scheduled-skill agents inserted under #3374
-- would otherwise block the ALTER.
UPDATE dispatcher.agents SET issue_number = 0 WHERE issue_number IS NULL;

ALTER TABLE dispatcher.agents
    ALTER COLUMN issue_number SET NOT NULL;
