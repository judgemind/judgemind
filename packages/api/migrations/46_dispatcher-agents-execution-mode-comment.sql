-- Up Migration
--
-- Update the ``dispatcher.agents.execution_mode`` column comment to
-- reflect that ``'ecs'`` is now the default since #3093 (Stage 4 of the
-- #3086 per-agent-ECS migration) and that ``'subprocess'`` is a legacy
-- fallback rather than the default.

COMMENT ON COLUMN dispatcher.agents.execution_mode IS
    'One of ''subprocess'' (legacy fallback) | ''ecs'' (default since #3093) '
    '(per-agent Fargate task via ecs:RunTask). Set once at claim time from '
    'dispatcher.config.agent_execution_mode and held immutable across '
    'the agent''s lifetime. See #3091 / #3086 / #3078.';


-- Down Migration
COMMENT ON COLUMN dispatcher.agents.execution_mode IS
    'One of ''subprocess'' (legacy, default) | ''ecs'' (per-agent '
    'Fargate task via ecs:RunTask). Set once at claim time from '
    'dispatcher.config.agent_execution_mode and held immutable across '
    'the agent''s lifetime. See #3091 / #3086 / #3078.';
