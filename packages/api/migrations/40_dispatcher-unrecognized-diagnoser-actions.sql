-- Up Migration
--
-- Dispatcher v2 — adds the `dispatcher.unrecognized_diagnoser_actions`
-- table so the daemon can persist diagnoser recommendation strings that
-- fall outside the known action set. Every terminal agent failure now
-- routes through the Opus diagnoser (issue #3032), and the closed-enum
-- guardrail on the diagnoser skill has been removed. That opens the
-- door for the LLM to propose novel actions the daemon does not yet
-- recognize — this table is how we persist + review those proposals.
--
-- Issue #3032. Builds on #2795 (diagnoser introduction) and #3010
-- (AC-infeasibility routing).
--
-- Design notes
-- ------------
-- - Minimal schema: one row per unrecognized action emission. The
--   operator's "5+ instances of the same `action_name` is signal to
--   implement a handler" workflow is a simple `SELECT action_name,
--   count(*) FROM dispatcher.unrecognized_diagnoser_actions WHERE
--   observed_at > now() - interval '7 days' GROUP BY 1 ORDER BY 2
--   DESC` — no fancy aggregation, no automated alerting.
-- - FK to `dispatcher.diagnoses(diagnosis_id)` so the original LLM
--   context / recommendation is always retrievable via JOIN. ON DELETE
--   stays at the default (RESTRICT) — unrecognized-action rows should
--   outlive diagnosis cleanup since they are the audit trail we need
--   for the operator review cadence.
-- - `payload` JSONB stores the full recommendation dict verbatim so
--   future handler implementations can see the shape the LLM proposed
--   without re-running the diagnoser.
-- - Index on `(action_name, observed_at DESC)` because the primary
--   operator query groups by action name and wants newest-first.

CREATE TABLE dispatcher.unrecognized_diagnoser_actions (
    id             BIGSERIAL   PRIMARY KEY,
    diagnosis_id   BIGINT      NOT NULL REFERENCES dispatcher.diagnoses(diagnosis_id),
    action_name    TEXT        NOT NULL,
    payload        JSONB       NOT NULL DEFAULT '{}',
    observed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_dispatcher_unrec_actions_action
    ON dispatcher.unrecognized_diagnoser_actions (action_name, observed_at DESC);

COMMENT ON TABLE dispatcher.unrecognized_diagnoser_actions IS
    'Diagnoser actions the daemon did not recognize. Operator reviews '
    'periodically; 5+ instances of a new action is signal to implement '
    'a handler. See issue #3032.';
COMMENT ON COLUMN dispatcher.unrecognized_diagnoser_actions.action_name IS
    'The action string as emitted by the diagnoser (e.g. "split_task"). '
    'Not constrained — anything the LLM proposed that was not in the '
    'known set lands here.';
COMMENT ON COLUMN dispatcher.unrecognized_diagnoser_actions.payload IS
    'The full recommendation dict (action + reasoning + any payload '
    'fields the LLM proposed) serialized as JSONB. Lets a future '
    'handler implementer see the proposed shape without replaying '
    'the diagnosis.';


-- Down Migration
DROP INDEX IF EXISTS dispatcher.idx_dispatcher_unrec_actions_action;
DROP TABLE IF EXISTS dispatcher.unrecognized_diagnoser_actions;
