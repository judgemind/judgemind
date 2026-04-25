-- Up Migration
--
-- Dispatcher v2 — update execution_mode column comment to reflect
-- Stage 4 (#3093) flipping the default to 'ecs'.
--
-- Context
-- -------
-- Migration 41 added the execution_mode column and seeded its comment
-- when subprocess was still the default. Stage 4 (#3093) flipped
-- DEFAULT_AGENT_EXECUTION_MODE from 'subprocess' to 'ecs'. The column
-- comment in schema.sql was updated in the #3093 PR to reflect the new
-- default; this migration keeps the live DB's system catalog in sync
-- with schema.sql so the schema-drift check stays green.

COMMENT ON COLUMN dispatcher.agents.execution_mode IS
    'One of ''subprocess'' (legacy fallback) | ''ecs'' (default since #3093) '
    '(per-agent Fargate task via ecs:RunTask). Set once at claim time from '
    'dispatcher.config.agent_execution_mode and held immutable across '
    'the agent''s lifetime. See #3091 / #3086 / #3078.';


-- Down Migration
--
-- Restore the original comment text from migration 41.
COMMENT ON COLUMN dispatcher.agents.execution_mode IS
    'One of ''subprocess'' (legacy, default) | ''ecs'' (per-agent '
    'Fargate task via ecs:RunTask). Set once at claim time from '
    'dispatcher.config.agent_execution_mode and held immutable across '
    'the agent''s lifetime. See #3091 / #3086 / #3078.';
