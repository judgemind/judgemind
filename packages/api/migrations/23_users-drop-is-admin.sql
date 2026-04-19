-- Up Migration
--
-- Dispatcher v2 — Phase 1 follow-up. Drops the `public.users.is_admin`
-- column introduced in migration 22 (#2730, PR #2743) and consolidates
-- the dispatcher admin gate onto the existing `users.role = 'admin'`
-- convention used by the data-quality dashboards.
--
-- Issue #2751 (refactor: consolidate dispatcher admin auth onto users.role).
--
-- Rationale
-- ---------
-- Migration 22's "orthogonal admin flag" design was a hallucination from
-- the spec consolidation pass in #2724 — it was never intended. Existing
-- admin surfaces (e.g. `data-quality.ts:27`) gate on `user.role === 'admin'`,
-- and the dispatcher admin surface is simpler and more consistent using
-- the same gate. One admin concept, one gate.
--
-- Migration 22 already ran on dev via the #2743 deploy, so we cannot just
-- revert it in git — we need a new forward migration that drops the column.
-- Migration 22 is left intact as a historical record.
--
-- This migration is idempotent: it uses DROP COLUMN IF EXISTS so re-running
-- is a no-op, and the UPDATE on public.users is idempotent by its WHERE
-- clause.

ALTER TABLE public.users DROP COLUMN IF EXISTS is_admin;

-- Seed drewthaler as role='admin' if not already. This preserves the
-- maintainer-admin grant that migration 22 established via is_admin=true
-- (now that the dispatcher surface gates on role='admin').
UPDATE public.users SET role = 'admin' WHERE email = 'drewthaler@gmail.com' AND role <> 'admin';


-- Down Migration
--
-- Intentionally not re-adding the is_admin column. It was only ever used by
-- the dispatcher auth gate, which this PR re-points at users.role. A true
-- rollback would require reverting the application-layer changes too, which
-- is out of scope for a DB-only down migration.
SELECT 1;
