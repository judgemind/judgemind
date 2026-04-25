-- Up Migration
--
-- Dispatcher v2 — extend `dispatcher.diagnoses` for the empowered
-- diagnoser (issue #3366). Adds two columns:
--
--   * `actions_taken` JSONB — structured action log. The empowered
--     diagnoser writes one entry per side-effect action (git commit,
--     git push, gh issue create/edit/comment, skill invoke, non-trivial
--     bash run). Operator audit trail; the daemon never reads this
--     column for decisions, only post-hoc human review.
--
--   * `next_directive` TEXT — 3-state daemon-consumed directive:
--       - 'respawn_at=<phase>' — diagnoser advanced the work; daemon
--         spawns a new agent-runner ECS task on the same agent_id
--         with START_PHASE=<phase> env so the agent resumes mid-pipeline.
--       - 'terminal' — diagnoser explicitly says no further action.
--         Daemon frees the slot and logs `diagnoser_completed_terminal`.
--       - NULL (the absence) — diagnoser didn't run, crashed, or
--         didn't finish. Distinguishable from 'terminal' so the daemon
--         can log `diagnoser_did_not_complete` and fall back to
--         escalate. This is the key change from "exits cleanly =
--         done" — without an explicit value the daemon cannot tell
--         "diagnoser ran and decided no more action" from "diagnoser
--         crashed before completing".
--
-- Why two new columns vs. extending the JSONB recommendation
-- ----------------------------------------------------------
-- `recommendation` retains its 8-action vocabulary as supplementary
-- context (operator audit, weekly report). The directive is the daemon
-- consumer's input — it deserves a flat, type-checkable column so the
-- consumer SQL stays trivial and the value is queryable from
-- CloudWatch / dev-db-query without JSONB unpacking.
--
-- `actions_taken` is JSONB because the shape evolves: today the schema
-- is `[{ts, type, ...payload}]` with `type` in
-- {git_commit, git_push, gh_issue_create, gh_issue_edit,
--  gh_issue_comment, skill_invoke, bash_run} (see
-- `.claude/skills/diagnose-failure/SKILL.md` §Audit trail). New
-- action types arrive over time; a column-per-type would require a
-- migration per addition.

ALTER TABLE dispatcher.diagnoses
    ADD COLUMN actions_taken JSONB,
    ADD COLUMN next_directive TEXT;

COMMENT ON COLUMN dispatcher.diagnoses.actions_taken IS
    'Structured action log written by the empowered diagnoser '
    '(issue #3366). One entry per side-effect: git_commit, git_push, '
    'gh_issue_create, gh_issue_edit, gh_issue_comment, skill_invoke, '
    'bash_run (non-trivial). Operator audit trail; daemon never reads.';

COMMENT ON COLUMN dispatcher.diagnoses.next_directive IS
    '3-state daemon-consumed directive (issue #3366). Values: '
    '"respawn_at=<phase>" — daemon spawns a new agent-runner with '
    'START_PHASE=<phase>; "terminal" — diagnoser explicitly done, '
    'free the slot; NULL — diagnoser did not complete, fall back to '
    'escalate AND emit diagnoser_did_not_complete. Distinguishable '
    'from terminal is the key invariant.';


-- Down Migration
--
-- Down strategy: drop the two columns. Reverting reinstates the
-- pre-#3366 schema. The empowered diagnoser path falls back to the
-- 8-action recommendation contract automatically — the supervisor
-- consumer reads `recommendation` first.
ALTER TABLE dispatcher.diagnoses
    DROP COLUMN IF EXISTS next_directive,
    DROP COLUMN IF EXISTS actions_taken;
