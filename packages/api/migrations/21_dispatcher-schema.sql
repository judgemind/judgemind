-- Up Migration
--
-- Dispatcher v2 — Phase 1 Sub-task A. Full `dispatcher.*` schema per
-- `docs/specs/dispatcher-v2-spec.md` §18 (DDL sketch).
--
-- Issue #2727 (Parent: #2725, Phase 1 epic).
--
-- This is the state foundation for dispatcher v2. Downstream in Phase 1:
--   - daemon.py skeleton (sub-task C) reads/writes these tables
--   - admin GraphQL queries (sub-task D) expose them to the admin page
--   - admin page (sub-task E) renders them
--
-- Design notes
-- ------------
-- - The schema is *not* added to `search_path`. Dispatcher-internal code
--   uses fully-qualified `dispatcher.*` names exclusively. This mirrors
--   the convention we adopted for `derived.*` / `telemetry.*` after the
--   schema reorganization (migration 15) and keeps the dispatcher state
--   clearly separated from application data.
-- - All FKs between dispatcher tables are intra-schema; no dispatcher
--   table references anything in `public`, `derived`, etc.
-- - `phase_transitions.phase` is intentionally free-form text (not an
--   enum) so adding a new phase name does not require a migration —
--   validation lives in the daemon (see §17 operational trap 7).
-- - `dispatcher_daemon` role + GRANTs are deferred to sub-task B
--   (#2725). Today, the API role (`judgemind`, i.e. the DB owner) has
--   implicit full access. Sub-task B will introduce a least-privilege
--   daemon role and narrow the API role's permissions. A TODO is left
--   at the bottom of this migration so the grant story stays visible.
--
-- Seed: `dispatcher.config` is populated with the seven live-editable
-- settings from §18 (concurrency_cap, subprocess_timeout_s,
-- backoff_seconds, idle_audit_every_n_prs, idle_spotcheck_cron,
-- runner_by_phase, model_by_phase, runner_shadow). `updated_by='init'`
-- marks these rows as migration-seeded — the admin page will set
-- `updated_by` to a GitHub login when operators edit them.

CREATE SCHEMA dispatcher;

-- --------------------------------------------------------------------
-- runs — one row per daemon boot; lease token for the double-daemon
-- race described in §17 operational trap 2.
-- --------------------------------------------------------------------
CREATE TABLE dispatcher.runs (
    run_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    stopped_at    TIMESTAMPTZ,
    heartbeat_ts  TIMESTAMPTZ NOT NULL DEFAULT now(),
    version_sha   TEXT        NOT NULL,
    host          TEXT        NOT NULL,
    pid           INT         NOT NULL
);

COMMENT ON TABLE  dispatcher.runs                 IS 'One row per daemon boot. Latest row is the active lease.';
COMMENT ON COLUMN dispatcher.runs.heartbeat_ts    IS 'Daemon updates every tick. CloudWatch alarm pages if stale > 5min.';
COMMENT ON COLUMN dispatcher.runs.version_sha     IS 'Git SHA of the daemon build; supports forensic questions after a crash.';

-- --------------------------------------------------------------------
-- agents — one row per /task (or audit/spotcheck/security-review)
-- invocation. Replaces the ad-hoc worktree/issue bookkeeping that
-- lives in `tmp/agent-status/*.txt` today.
-- --------------------------------------------------------------------
CREATE TABLE dispatcher.agents (
    agent_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_run_id    UUID        REFERENCES dispatcher.runs(run_id),
    kind             TEXT        NOT NULL DEFAULT 'task',      -- task | audit | spotcheck | security-review
    issue_number     INT         NOT NULL,
    worktree_path    TEXT        NOT NULL,
    phase            TEXT        NOT NULL DEFAULT 'claiming',
    status           TEXT        NOT NULL DEFAULT 'running',   -- running | succeeded | failed | retrying | crashed
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    exit_code        INT,
    pr_number        INT,
    retries_used     INT         NOT NULL DEFAULT 0,
    pid              INT,
    runner_override  JSONB,                                     -- per-agent override of runner_by_phase; NULL = use config default
    model_override   JSONB                                      -- per-agent override of model_by_phase; NULL = use config default
);

CREATE INDEX idx_dispatcher_agents_running
    ON dispatcher.agents (status)
    WHERE status = 'running';

CREATE INDEX idx_dispatcher_agents_issue_number
    ON dispatcher.agents (issue_number);

COMMENT ON TABLE  dispatcher.agents                  IS 'One row per /task (or audit/spotcheck/security-review) agent invocation.';
COMMENT ON COLUMN dispatcher.agents.runner_override  IS 'Per-agent override of dispatcher.config.runner_by_phase; NULL = use config default.';
COMMENT ON COLUMN dispatcher.agents.model_override   IS 'Per-agent override of dispatcher.config.model_by_phase; NULL = use config default.';

-- --------------------------------------------------------------------
-- phase_transitions — append-only log. Replaces tmp/agent-status/*.txt.
-- `phase` is intentionally free-form TEXT (not an enum).
-- --------------------------------------------------------------------
CREATE TABLE dispatcher.phase_transitions (
    transition_id      BIGSERIAL   PRIMARY KEY,
    agent_id           UUID        NOT NULL REFERENCES dispatcher.agents(agent_id),
    phase              TEXT        NOT NULL,
    ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    autocompact_count  INT         NOT NULL DEFAULT 0
);

CREATE INDEX idx_dispatcher_phase_transitions_agent_ts
    ON dispatcher.phase_transitions (agent_id, ts DESC);

COMMENT ON TABLE dispatcher.phase_transitions IS 'Append-only phase log; replaces tmp/agent-status/*.txt. Phase is free-form text; validation is daemon-side.';

-- --------------------------------------------------------------------
-- failures — direct-write from hooks + daemon-side detectors.
-- --------------------------------------------------------------------
CREATE TABLE dispatcher.failures (
    failure_id    BIGSERIAL   PRIMARY KEY,
    agent_id      UUID        REFERENCES dispatcher.agents(agent_id),
    category      TEXT        NOT NULL,
    detected_by   TEXT        NOT NULL,
    details       JSONB       NOT NULL DEFAULT '{}',
    ts            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_dispatcher_failures_ts
    ON dispatcher.failures (ts DESC);

CREATE INDEX idx_dispatcher_failures_category_ts
    ON dispatcher.failures (category, ts DESC);

COMMENT ON COLUMN dispatcher.failures.detected_by IS 'hook | scheduler | diagnoser — who wrote this row.';

-- --------------------------------------------------------------------
-- retry_markers — mechanical-failure retry queue with exponential
-- backoff (60 → 300 → 900s; attempt capped at 3 per §8).
-- --------------------------------------------------------------------
CREATE TABLE dispatcher.retry_markers (
    marker_id       BIGSERIAL   PRIMARY KEY,
    agent_id        UUID        NOT NULL REFERENCES dispatcher.agents(agent_id),
    reason          TEXT        NOT NULL,
    attempt         INT         NOT NULL CHECK (attempt BETWEEN 1 AND 3),
    retry_after_ts  TIMESTAMPTZ NOT NULL,
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX idx_dispatcher_retry_markers_pending
    ON dispatcher.retry_markers (retry_after_ts)
    WHERE resolved_at IS NULL;

-- --------------------------------------------------------------------
-- commands — operator-issued actions consumed by the daemon.
-- --------------------------------------------------------------------
CREATE TABLE dispatcher.commands (
    command_id    BIGSERIAL   PRIMARY KEY,
    command       TEXT        NOT NULL,                         -- start | stop | drain | pause | resume | retry | force_kill
    issued_by     TEXT        NOT NULL,
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at   TIMESTAMPTZ,
    payload       JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_dispatcher_commands_unconsumed
    ON dispatcher.commands (consumed_at)
    WHERE consumed_at IS NULL;

-- --------------------------------------------------------------------
-- config — live-editable key/value settings. Seeded below.
-- --------------------------------------------------------------------
CREATE TABLE dispatcher.config (
    key         TEXT        PRIMARY KEY,
    value       JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT        NOT NULL
);

-- --------------------------------------------------------------------
-- diagnoses — tiered diagnoser output (§8). One row per invocation.
-- --------------------------------------------------------------------
CREATE TABLE dispatcher.diagnoses (
    diagnosis_id    BIGSERIAL   PRIMARY KEY,
    failure_id      BIGINT      NOT NULL REFERENCES dispatcher.failures(failure_id),
    agent_id        UUID        NOT NULL REFERENCES dispatcher.agents(agent_id),
    status          TEXT        NOT NULL DEFAULT 'pending',     -- pending | completed | failed
    context         JSONB       NOT NULL,                       -- serialized bundle passed to the diagnoser
    recommendation  JSONB,                                      -- {action, reasoning, hint?, new_scope?}
    outcome         JSONB,                                      -- filled in after the retry resolves
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_dispatcher_diagnoses_pending
    ON dispatcher.diagnoses (status)
    WHERE status = 'pending';

CREATE INDEX idx_dispatcher_diagnoses_agent_id
    ON dispatcher.diagnoses (agent_id);

-- --------------------------------------------------------------------
-- phase_outputs — structured JSON output from each phase, read from
-- {worktree}/tmp/dispatcher-output/<phase>.json by the daemon.
-- UNIQUE (agent_id, phase) — one output per phase per agent.
-- --------------------------------------------------------------------
CREATE TABLE dispatcher.phase_outputs (
    output_id    BIGSERIAL   PRIMARY KEY,
    agent_id     UUID        NOT NULL REFERENCES dispatcher.agents(agent_id),
    phase        TEXT        NOT NULL,                          -- plan | ralph | summary | fix_ci | verify | retro
    output_json  JSONB       NOT NULL,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_dispatcher_phase_outputs_agent_phase
    ON dispatcher.phase_outputs (agent_id, phase);

-- --------------------------------------------------------------------
-- notifications — Telegram + admin-page notification history.
-- Preserves escalation history even when Telegram itself is down (§12).
-- --------------------------------------------------------------------
CREATE TABLE dispatcher.notifications (
    notification_id  BIGSERIAL   PRIMARY KEY,
    kind             TEXT        NOT NULL,                       -- stuck_timeout | deploy_failure | diagnoser_escalate | api_overload | claude_p_broken | boot | daily_summary
    severity         TEXT        NOT NULL,                       -- human_attention | status
    issue_number     INT,
    pr_number        INT,
    body             TEXT        NOT NULL,
    issue_url        TEXT,                                       -- denormalized for admin-page display; NULL on status-class messages
    sent_at          TIMESTAMPTZ,
    send_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_dispatcher_notifications_created_at
    ON dispatcher.notifications (created_at DESC);

CREATE INDEX idx_dispatcher_notifications_human_attention_unsent
    ON dispatcher.notifications (severity, sent_at)
    WHERE severity = 'human_attention';

-- --------------------------------------------------------------------
-- Seed config. The seven live-editable keys from §18. `updated_by`
-- distinguishes migration-seeded rows ('init') from operator edits.
-- --------------------------------------------------------------------
INSERT INTO dispatcher.config (key, value, updated_by) VALUES
    ('concurrency_cap',         '5',                                                                                                                                               'init'),
    ('subprocess_timeout_s',    '10800',                                                                                                                                           'init'),  -- 180 min ceiling; covers known outliers (#2513 107min, #2628 98min)
    ('backoff_seconds',         '[60,300,900]',                                                                                                                                    'init'),
    ('idle_audit_every_n_prs',  '20',                                                                                                                                              'init'),
    ('idle_spotcheck_cron',     '"0 14 * * *"',                                                                                                                                    'init'),
    ('runner_by_phase',         '{"plan":"claude","ralph":"claude","summary":"claude","fix_ci":"claude","verify":"claude","retro":"claude","diagnose":"claude"}',                  'init'),
    ('model_by_phase',          '{"plan":"opus","ralph":"sonnet","summary":"haiku","fix_ci":"sonnet","verify":"haiku","retro":"haiku","diagnose":"opus"}',                         'init'),
    ('runner_shadow',           '{}',                                                                                                                                              'init');

-- --------------------------------------------------------------------
-- GRANTs — deferred to sub-task B (#2725, Phase 1 sub-task B).
--
-- Today every service connects as the `judgemind` role, which is the DB
-- owner and therefore has implicit full access to dispatcher.*. Sub-task
-- B will:
--   1. Create a least-privilege `dispatcher_daemon` DB role.
--   2. Narrow the API role so only admin GraphQL queries can read
--      dispatcher.* (no writes from the API service).
--   3. Add the corresponding `GRANT USAGE ON SCHEMA dispatcher TO ...`
--      and `GRANT SELECT / INSERT / UPDATE / DELETE ON ... TO ...`.
--
-- Keeping the GRANT story in a single follow-up migration is cleaner
-- than threading partial grants through this foundation migration.
-- --------------------------------------------------------------------


-- Down Migration
-- Wipes all dispatcher.* state. `CASCADE` handles the bigserial
-- sequences, indexes, and inter-table FKs in one statement.
DROP SCHEMA IF EXISTS dispatcher CASCADE;
