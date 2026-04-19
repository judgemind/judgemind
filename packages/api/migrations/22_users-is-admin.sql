-- Up Migration
--
-- Dispatcher v2 — Phase 1 Sub-task D. Adds the admin auth gate for the
-- dispatcher admin GraphQL surface (`docs/specs/dispatcher-v2-spec.md` §11).
--
-- Issue #2730 (Parent: #2725, Phase 1 epic).
--
-- Design notes
-- ------------
-- - Existing dashboards (e.g. dataQualityMetrics, dataQualityOverview)
--   gate on `users.role = 'admin'`. Dispatcher gates on the new boolean
--   column instead, per §11: "Auth: gated on `users.is_admin = true`
--   (new column, defaults false; seed drewthaler). Non-admins 404."
-- - Boolean rather than re-purposing `role` so we can grant dispatcher
--   admin without also promoting a user to the existing role-based admin
--   pages (or vice versa). The two gates are orthogonal.
-- - Non-admins receive a generic "not found" error (no field-shape
--   introspection) — that logic lives in the resolvers, not the DB.
-- - Seed drewthaler@gmail.com → is_admin=true if the row exists. This
--   is idempotent (WHERE email=...) so re-applying is a no-op, and
--   non-failing if drewthaler hasn't registered on this DB yet (dev/CI).

ALTER TABLE public.users
    ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN public.users.is_admin IS 'Grants access to the dispatcher admin GraphQL surface (§11). Orthogonal to users.role.';

-- Seed the maintainer. Idempotent; no-op if the user has not yet
-- registered on this database.
UPDATE public.users SET is_admin = TRUE WHERE email = 'drewthaler@gmail.com';


-- Down Migration
ALTER TABLE public.users DROP COLUMN IF EXISTS is_admin;
