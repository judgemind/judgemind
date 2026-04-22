-- Up Migration
--
-- Dispatcher v2 — persist ralph SHIP'd patches in postgres to survive
-- daemon restart during the push-and-pr window (#3012).
--
-- Motivation
-- ----------
-- Today, ralph's SHIP'd diff lives only in the worktree until the
-- daemon's ``_push_and_open_pr`` successfully runs ``gh pr create``.
-- Between ralph SHIP exit and a successful ``gh pr create`` the diff is
-- ephemeral:
--
--   1. The branch is on ralph's worktree but NOT on origin yet.
--   2. A daemon restart (deploy, crash, operator kill) triggers a
--      worktree sweep (``_create_worktree`` re-uses a clean base for
--      the next attempt), which wipes the diff.
--   3. The next claim starts fresh and re-runs ralph from scratch,
--      burning the prior hour-plus of work.
--
-- Concrete cost observed 2026-04-21: #2564 agent c3a69458 shipped after
-- 1h 48m of ralph, hit ``git_push_timeout`` at 15:17, and the deploy of
-- PR #2999 killed the daemon minutes later. Four subsequent attempts
-- re-ralph'd the same task, eating hours of cycle time. See #3012 for
-- the incident narrative and fix design.
--
-- The fix
-- -------
-- Persist ralph's SHIP'd diff in postgres for the narrow window between
-- ralph SHIP exit and successful ``gh pr create``. Once the PR exists,
-- the branch is durably on origin and the postgres copy is redundant —
-- delete it. Patch lifetime is typically 5-15 min in the happy path;
-- failures extend up to the stuck-timeout budget. 7-day TTL catches
-- pathological cases (daemon crash between SHIP and PR create, operator
-- force-stop).
--
-- Design notes
-- ------------
-- - Dedicated table (not a new column on ``phase_outputs.log_text``).
--   ``phase_outputs.log_text`` holds the phase narrative; mixing raw
--   patches into it blurs two separate concerns (observability narrative
--   vs. recoverable artifact) and complicates the ``DELETE on PR create``
--   semantics. A separate table lets the delete target one row by
--   ``agent_id`` without risking the phase log rows the admin page reads.
-- - ``patch_content TEXT`` — git format-patch output is ASCII plus the
--   file diff bytes. PostgreSQL's TOAST + pglz compression applies
--   automatically for rows over ~2kB. No explicit compression setting
--   needed.
-- - ``commit_sha`` is nullable — the daemon writes it when available from
--   ``git rev-parse HEAD`` but a patch apply failure during the read path
--   doesn't need the SHA to know the patch is gone.
-- - Two indexes:
--     * ``issue_number`` — the read path queries "does a prior SHIP'd
--       patch exist for this issue?" on every ``_create_worktree``.
--     * ``created_at`` — the housekeeping sweep prunes rows older than
--       7 days. A btree on ``created_at`` supports the range scan.
-- - Nullable ``patch_id`` FK on ``phase_outputs`` — the ralph SHIP row is
--   the only row that ever gets populated; every other phase row stays
--   NULL. ``ON DELETE SET NULL`` so the ralph phase_outputs row survives
--   the patch cleanup (the admin log view and metering aggregates still
--   work). Rows for non-SHIP ralph exits (AC_INFEASIBLE, max-iterations,
--   crashes) never get a patch_id.
-- - No dedicated retention config key — 7 days is a safety-net window,
--   not an operator-tunable observability knob. The existing retention
--   config keys (``phase_output_retention_days``, etc.) all describe
--   telemetry retention; patches are ephemeral recovery state. If
--   operators ever need to tune this, a ``ralph_patch_retention_days``
--   config key can be added later without a migration.

CREATE TABLE dispatcher.ralph_patches (
    patch_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id       UUID        NOT NULL REFERENCES dispatcher.agents(agent_id),
    issue_number   INT         NOT NULL,
    patch_content  TEXT        NOT NULL,
    commit_sha     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ralph_patches_issue_idx
    ON dispatcher.ralph_patches (issue_number);

CREATE INDEX ralph_patches_created_at_idx
    ON dispatcher.ralph_patches (created_at);

COMMENT ON TABLE dispatcher.ralph_patches IS
    'Patches from ralph SHIP exits, persisted for the narrow window between SHIP and a successful gh pr create. Issue #3012.';

COMMENT ON COLUMN dispatcher.ralph_patches.patch_content IS
    'git format-patch -1 HEAD --stdout output. Applied with git am when a retry inherits the prior SHIP.';

COMMENT ON COLUMN dispatcher.ralph_patches.commit_sha IS
    'ralph''s HEAD SHA at SHIP time. Nullable — written when git rev-parse succeeds; informational only (the patch is the authoritative artifact).';

ALTER TABLE dispatcher.phase_outputs
    ADD COLUMN patch_id UUID REFERENCES dispatcher.ralph_patches(patch_id) ON DELETE SET NULL;

COMMENT ON COLUMN dispatcher.phase_outputs.patch_id IS
    'FK to dispatcher.ralph_patches when this is the ralph SHIP row; NULL for every other phase and for ralph non-SHIP exits. ON DELETE SET NULL so phase_outputs survives patch cleanup. Issue #3012.';


-- Down Migration
ALTER TABLE dispatcher.phase_outputs DROP COLUMN IF EXISTS patch_id;
DROP TABLE IF EXISTS dispatcher.ralph_patches;
