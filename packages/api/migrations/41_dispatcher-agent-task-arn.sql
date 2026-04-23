-- Up Migration
--
-- Dispatcher v2 — Stage 2 of the per-agent ECS migration (issue #3091,
-- part of the #3086 / #3078 Option A plan). Adds two columns to
-- ``dispatcher.agents`` so the daemon can track the lifecycle of a
-- one-shot agent-runner ECS task per agent.
--
-- Stage 2 context
-- ---------------
-- Stage 1a shipped the shared ``scripts/dispatcher/phase_transitions``
-- module (#3086). Stage 1b shipped the
-- ``judgemind-dispatcher-agent-runner-dev`` task definition + entrypoint
-- (#3090). Stage 2 wires the daemon to ``ecs:RunTask`` the new task def
-- per agent and to observe its lifecycle via ``ecs:DescribeTasks``. The
-- columns below are the persistence surface for that wiring:
--
--   * ``agent_task_arn`` — the ECS task ARN returned by ``ecs:RunTask``
--     for this agent's runner. NULL while the agent is running in the
--     legacy subprocess mode; populated the moment the daemon launches
--     the per-agent ECS task. Reads across a daemon restart: a fresh
--     daemon can ``ecs:DescribeTasks`` against every non-NULL value to
--     resume observation without the #3078 "every in-flight agent gets
--     killed on redeploy" bug.
--
--   * ``execution_mode`` — one of ``'subprocess'`` | ``'ecs'``. Set
--     once at claim time from ``dispatcher.config.agent_execution_mode``
--     and held immutable across the agent's lifetime (so a mid-flight
--     flip of the config flag does not produce a hybrid agent). The
--     default is ``'subprocess'`` — this PR is inert on deploy until
--     Stage 3 smoke (#3092) and Stage 4 flip-default (#3093).
--
-- Design notes
-- ------------
-- - ``execution_mode`` is ``TEXT NOT NULL DEFAULT 'subprocess'`` rather
--   than a CHECK-constrained enum. A TEXT column with a default lets
--   pre-#3091 rows (backfilled via the ALTER DEFAULT) take the
--   subprocess lane automatically, which is exactly the behaviour we
--   want during the migration window. A CHECK constraint can be added
--   in Stage 5 (#3094) once ``subprocess`` is removed and the value set
--   shrinks to a single option.
--
-- - ``agent_task_arn`` is NULLable because the legacy subprocess mode
--   does not populate it. Even in ``execution_mode='ecs'`` mode the
--   ARN is NULL between claim and a successful ``ecs:RunTask`` (a few
--   seconds at most); the reap path tolerates NULL.
--
-- - Partial index on ``(agent_task_arn) WHERE agent_task_arn IS NOT
--   NULL`` so the daemon's reap pass (one ``ecs:DescribeTasks`` call
--   per scheduler tick) can scan only the rows that have a live ECS
--   task ARN attached. Without the WHERE predicate the full-table
--   index would include every historical agent_task_arn=NULL row,
--   which is most of the table for the life of the subprocess path.
--
-- Rollback
-- --------
-- Dropping the columns is safe — nothing else in the schema references
-- them, and the daemon tolerates their absence at runtime (the Stage 2
-- daemon code checks for the columns via the same ``SELECT column_name
-- FROM information_schema.columns`` probe the test suite uses).

ALTER TABLE dispatcher.agents
    ADD COLUMN agent_task_arn TEXT NULL;

ALTER TABLE dispatcher.agents
    ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'subprocess';

CREATE INDEX dispatcher_agents_task_arn_idx
    ON dispatcher.agents (agent_task_arn)
    WHERE agent_task_arn IS NOT NULL;

COMMENT ON COLUMN dispatcher.agents.agent_task_arn IS
    'ECS task ARN of the per-agent agent-runner task (#3091). NULL for '
    'legacy subprocess-mode agents and between claim + ecs:RunTask in '
    'ecs-mode agents. Partial-indexed so the daemon reap pass scans '
    'only live ECS agents.';

COMMENT ON COLUMN dispatcher.agents.execution_mode IS
    'One of ''subprocess'' (legacy, default) | ''ecs'' (per-agent '
    'Fargate task via ecs:RunTask). Set once at claim time from '
    'dispatcher.config.agent_execution_mode and held immutable across '
    'the agent''s lifetime. See #3091 / #3086 / #3078.';


-- Down Migration
DROP INDEX IF EXISTS dispatcher.dispatcher_agents_task_arn_idx;

ALTER TABLE dispatcher.agents
    DROP COLUMN IF EXISTS execution_mode;

ALTER TABLE dispatcher.agents
    DROP COLUMN IF EXISTS agent_task_arn;
