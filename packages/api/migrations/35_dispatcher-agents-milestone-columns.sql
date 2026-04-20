-- Up Migration
--
-- Dispatcher v2 — split the single ``status='succeeded'`` terminal
-- write into four independently-written milestone columns on
-- ``dispatcher.agents`` so the admin cockpit can distinguish
-- "shipped" from "verified" from "retro completed" and show a single
-- glyph whose color encodes pipeline completeness (#2953).
--
-- Problem
-- -------
-- Before this migration, ``dispatcher.agents.status`` conflated "did
-- the PR ship?" with "did verify + retro finish?" — so a PR that
-- merged successfully but whose verify or retro phase was killed by
-- a mid-pipeline dispatcher restart got written as ``status='failed'``
-- with ``failure_summary='dispatcher restarted'``. That renders as a
-- red ✗ in the admin panel even though the code shipped. See
-- issues #2942/#2943/#2944/#2945/#2946 for four concrete examples of
-- the false-failure pattern.
--
-- Fix
-- ---
-- Four new nullable TIMESTAMPTZ / TEXT columns, each written at the
-- exact moment its milestone happens (no batch update at end-of-
-- pipeline):
--
--   merged_at          — PR squash-merge observed (shipped bar).
--   verified_at        — verify phase returned VERIFIED (or skipped
--                        rows intentionally leave this NULL and set
--                        verify_skip_reason instead).
--   verify_skip_reason — nullable; populated when verify is
--                        intentionally skipped. Today: 'self_deploy'
--                        for dispatcher-self-PRs. Future:
--                        'docs_only', 'ci_cd_only'.
--   retroed_at         — retro phase reached PHASE_RETRO_DONE.
--
-- With these columns, ``status`` becomes a one-way latch:
--   * ``succeeded`` is written the moment ``merged_at`` is — not
--     conditional on verify/retro.
--   * If the container dies between merge and retro, the row reads
--     ``succeeded`` with ``merged_at`` set and ``verified_at`` /
--     ``retroed_at`` NULL. That absence is the visible signal; the
--     admin cockpit renders an amber ✓ to highlight the incomplete
--     post-merge bookkeeping.
--
-- Design notes
-- ------------
-- - TIMESTAMPTZ with no DEFAULT — explicit writes at each milestone,
--   no drift from NOW() elsewhere.
-- - No CHECK constraint on ``verify_skip_reason`` — the set of valid
--   values is daemon-code owned (``_VERIFY_SKIP_REASONS``); adding a
--   CHECK here would couple schema to that enum. Unknown values flow
--   through the UI (which renders the raw string in the tooltip).
-- - No indexes — every read path queries by ``agent_id`` or
--   ``ended_at`` which are already covered; these columns are
--   render-only.
-- - ``ADD COLUMN IF NOT EXISTS`` so the migration is idempotent.
--
-- Backfill
-- --------
-- Two UPDATE statements, both idempotent (guarded on the new column
-- being NULL):
--
--   1. Historical "true succeeded" rows — ``pr_number IS NOT NULL
--      AND status='succeeded'`` — get ``merged_at = ended_at``. We
--      don't know exactly when the merge happened vs. the verify, but
--      ``ended_at`` is the upper bound and matches the observable
--      "shipped before or at ended_at" truth.
--
--   2. The false-failure pattern this issue targets — ``pr_number IS
--      NOT NULL AND status='failed' AND failure_summary='dispatcher
--      restarted'`` — gets both ``merged_at = ended_at`` AND ``status
--      = 'succeeded'``. These rows merged successfully before the
--      daemon restart; the stored ``failed`` was the bug. Flipping
--      to ``succeeded`` removes the false-red glyph from the admin
--      page while ``verified_at`` / ``retroed_at`` staying NULL
--      preserves the "post-merge bookkeeping interrupted" signal (the
--      row renders amber ✓ instead of green).
--
-- Historical ``verified_at`` and ``retroed_at`` stay NULL because we
-- have no durable signal of when those phases completed — the prior
-- code path only wrote the coarse ``status`` column. The UI handles
-- this gracefully (the absence just means "no milestone stamped").

ALTER TABLE dispatcher.agents
    ADD COLUMN IF NOT EXISTS merged_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS verified_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS verify_skip_reason TEXT,
    ADD COLUMN IF NOT EXISTS retroed_at         TIMESTAMPTZ;

COMMENT ON COLUMN dispatcher.agents.merged_at IS
    'Timestamp the PR squash-merge was observed by the daemon '
    '(``_merge_pr_and_advance``). One-way latch: written once, never '
    'cleared. Paired with the ``status=''succeeded''`` write at the '
    'same merge point. NULL for rows that never merged (push_and_pr '
    'failed, CI red after retries, etc.) and for pre-migration-35 '
    'rows without a recoverable merge signal. Issue #2953.';

COMMENT ON COLUMN dispatcher.agents.verified_at IS
    'Timestamp the verify phase completed with verdict=VERIFIED. '
    'NULL when verify was intentionally skipped (see '
    '``verify_skip_reason``), when verify crashed mid-phase, or for '
    'pre-migration-35 rows. A merged row with NULL ``verified_at`` '
    'AND NULL ``verify_skip_reason`` renders as amber ✓ in the admin '
    'cockpit — "shipped but post-merge bookkeeping incomplete". '
    'Issue #2953.';

COMMENT ON COLUMN dispatcher.agents.verify_skip_reason IS
    'Non-null when verify is intentionally skipped — the row is '
    '"shipped + verify-does-not-apply" rather than "shipped + verify '
    'failed to run". Today the only written value is ''self_deploy'' '
    '(dispatcher-self-PR touches ``scripts/dispatcher/`` and the new '
    'daemon deploy will land mid-verify, making verify against the '
    'soon-to-be-replaced process useless). Planned future values: '
    '''docs_only'' (``docs/`` / ``*.md`` only), ''ci_cd_only'' '
    '(``.github/workflows/`` only). No CHECK constraint — the set is '
    'daemon-code owned. Issue #2953.';

COMMENT ON COLUMN dispatcher.agents.retroed_at IS
    'Timestamp the retro phase reached PHASE_RETRO_DONE. NULL when '
    'retro crashed, when the worktree was already gone at retro time '
    '(fast-path to cleanup_done), or for pre-migration-35 rows. A '
    'merged row with ``verified_at`` set but ``retroed_at`` NULL '
    'renders as amber ✓ — "shipped + verified but retro bookkeeping '
    'did not complete". Issue #2953.';

-- Backfill 1 — rows that legitimately succeeded (status='succeeded'
-- with a merged PR) get ``merged_at = ended_at``. Guarded on the new
-- column being NULL so re-running the migration (or the upstream
-- daemon code writing a precise merged_at at the actual merge time)
-- does not clobber a more accurate value.
UPDATE dispatcher.agents
   SET merged_at = ended_at
 WHERE pr_number IS NOT NULL
   AND status = 'succeeded'
   AND ended_at IS NOT NULL
   AND merged_at IS NULL;

-- Backfill 2 — the false-failure pattern #2953 targets. Rows that
-- got terminalized as 'failed' with failure_summary='dispatcher
-- restarted' AND had a PR number actually did merge before the daemon
-- restart; the 'failed' is the bug this issue resolves. Flip them to
-- 'succeeded' and stamp merged_at so the admin panel stops showing
-- red ✗ for work that actually shipped. Other 'failed' rows (no PR,
-- or a different failure_summary) stay as-is — they are genuine
-- non-merge failures.
UPDATE dispatcher.agents
   SET merged_at = ended_at,
       status    = 'succeeded'
 WHERE pr_number IS NOT NULL
   AND status = 'failed'
   AND failure_summary = 'dispatcher restarted'
   AND ended_at IS NOT NULL
   AND merged_at IS NULL;


-- Down Migration
--
-- Drop the four columns. The admin cockpit's OutcomePill handles
-- nullable inputs gracefully — absent milestone columns collapse
-- rendering back to the legacy ``status``-only path. Backfill 2's
-- status flip is NOT reversed on down-migrate (we do not know which
-- 'succeeded' rows came from the flip vs. the normal success path
-- without reading ``failure_summary``, and the flip was always the
-- correct terminal value anyway).

ALTER TABLE dispatcher.agents
    DROP COLUMN IF EXISTS merged_at,
    DROP COLUMN IF EXISTS verified_at,
    DROP COLUMN IF EXISTS verify_skip_reason,
    DROP COLUMN IF EXISTS retroed_at;
