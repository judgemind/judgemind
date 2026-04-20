-- Up Migration
--
-- Dispatcher v2 — add a nullable ``priority`` text column to
-- ``dispatcher.agents`` so the admin cockpit's Active agents and
-- Recently completed panels can render a priority badge without a
-- GraphQL-layer JOIN to GitHub (#2899).
--
-- Context
-- -------
-- Issue #2899 unifies the three dispatcher-admin tables on a shared
-- ``(issue, priority, title)`` prefix. The queue panels already have
-- priority because queue rows are derived from the ``dispatcher.queue_snapshots``
-- JSON (which carries per-issue labels). Active agents and Recently
-- completed rows come from ``dispatcher.agents`` directly, which had
-- ``issue_number`` + ``issue_title`` but no priority — so adding the
-- column here mirrors the ``issue_title`` denormalization that migration
-- 28 (#2820) landed for the same reason.
--
-- Design notes
-- ------------
-- - **Nullable TEXT.** Values are ``'p0'`` | ``'p1'`` | ``'p2'`` | ``'p3'``
--   | NULL (no priority label at claim time, or a pre-migration row).
--   No CHECK constraint because ``dispatcher.agents.status`` is also
--   plain text for the same reason (future labels slot in without a
--   schema migration). The admin cockpit's ``<PriorityBadge>`` is the
--   single render point — it already handles null and unknown values.
-- - **Written at claim time.** Both the daemon (``_atomic_claim``) and
--   the /task skill (``scripts/dispatcher/task_claim.py claim
--   --issue-priority``) populate this column when inserting. The value
--   is parsed from the issue's label set at the moment of the claim —
--   it intentionally reflects "priority when spawned", not "priority
--   now", so a relabel on GitHub post-claim does not retroactively
--   rewrite the admin row. This mirrors the existing
--   ``dispatcher.agents.issue_title`` pattern.
-- - **No backfill.** Historical rows keep ``priority=NULL`` — the UI
--   renders an em-dash placeholder. Backfilling would require hitting
--   GitHub for thousands of closed issues, which is exactly the kind of
--   GitHub-PAT-budget burn the operator constraint bans (see the
--   ``parse-labels.ts`` docstring in the API package).
-- - **No index.** We never filter or sort ``dispatcher.agents`` by
--   priority — it's strictly a render-time column for the admin table.
--   The existing ``idx_dispatcher_agents_running`` and
--   ``idx_dispatcher_agents_issue_number`` indexes already cover all
--   read paths.

ALTER TABLE dispatcher.agents
    ADD COLUMN priority TEXT;

COMMENT ON COLUMN dispatcher.agents.priority IS
    'Priority label captured at claim time (p0|p1|p2|p3|NULL). '
    'Written by both DispatcherDaemon._atomic_claim and '
    'scripts/dispatcher/task_claim.py. Reflects "priority when spawned"; '
    'does not retroactively update when the issue is relabeled on GitHub. '
    'Issue #2899.';


-- Down Migration
--
-- Drop the column. The UI gracefully degrades — ``<PriorityBadge
-- priority={null} />`` renders the em-dash placeholder, which is the
-- same behaviour operators see for pre-migration rows.
ALTER TABLE dispatcher.agents
    DROP COLUMN IF EXISTS priority;
