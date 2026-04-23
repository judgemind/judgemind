-- Up Migration
--
-- Dispatcher v2 — add ``log_text`` column to ``dispatcher.phase_outputs``
-- so the full ephemeral phase log (merged stdout+stderr captured from
-- ``{worktree}/tmp/claude-p-<phase>.log``) survives worktree cleanup,
-- container restart, and daemon redeploy.
--
-- Issue #2821 (scope amendment on the ``--cwd`` fix — folds the
-- full-log-capture gap into the same PR since both touch the
-- subprocess-spawn helper).
--
-- Design notes
-- ------------
-- - ``text`` (not ``jsonb``) — the log is plain text (stdout+stderr
--   stream), not structured JSON. PostgreSQL's native TOAST + pglz
--   compression applies automatically for any row that exceeds the
--   TOAST threshold (default ~2kB), so no explicit compression setting
--   is needed.
-- - Nullable — historical rows inserted before this migration have no
--   log content to back-fill, and phases that fail before producing any
--   log (e.g. ``claude`` not on PATH) legitimately have no body. Writing
--   ``NULL`` keeps the "no signal" case distinguishable from an empty
--   file.
-- - Capped by the caller, not the DB — ``scripts/dispatcher/daemon.py``
--   passes whatever the phase log produced, and the housekeeping tick
--   (#2778/#2779) prunes rows at 30 days along with ``output_json``.
--   Adding a DB-side ``CHECK (char_length(log_text) <= N)`` would turn
--   an operational cap into a hard failure the daemon has to handle;
--   the current convention is "daemon trims, DB stores". A runaway
--   multi-MB log is a daemon bug to fix at source, not a DB guard to
--   trip.
-- - No index — readers are always keyed on ``(agent_id, phase)`` which
--   is already covered by ``idx_dispatcher_phase_outputs_agent_phase``
--   (migration 21). The ``log_text`` column is read-through, never
--   searched.

ALTER TABLE dispatcher.phase_outputs
    ADD COLUMN log_text TEXT;

COMMENT ON COLUMN dispatcher.phase_outputs.log_text IS
    'Full ephemeral phase log (stdout+stderr) captured from {worktree}/tmp/claude-p-<phase>.log at phase-finish time. Nullable for historical rows and for phases that failed before producing a log. Housekeeping tick prunes at 30 days along with output_json. See #2821.';


-- Down Migration
ALTER TABLE dispatcher.phase_outputs DROP COLUMN log_text;
