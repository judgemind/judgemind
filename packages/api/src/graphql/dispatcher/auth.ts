/**
 * Auth gate for the dispatcher admin GraphQL surface.
 *
 * Non-admins must receive a generic "not found" error — not "forbidden" —
 * so the existence and field shape of the dispatcher endpoints is not
 * introspectable by non-admins (§11 Auth).
 *
 * Gates on `user.role === 'admin'` — the same convention used by the
 * existing data-quality admin dashboards (`data-quality.ts:27`). A single
 * admin concept across all admin surfaces keeps the auth model simple.
 * The only behavioural difference from `requireAdmin` in `data-quality.ts`
 * is the response shape: this gate returns a generic NOT_FOUND so the
 * dispatcher schema is not enumerable by non-admins, whereas data-quality
 * returns an explicit FORBIDDEN.
 */

import { GraphQLError } from 'graphql';
import type { AuthUser } from '../../auth';

/**
 * Throw the "not found" error used by every dispatcher resolver when the
 * caller is not an admin. A single indistinguishable error for both
 * "no auth" and "authed-but-not-admin" prevents enumeration of the admin
 * surface from non-admin sessions.
 */
export function notFound(): never {
  throw new GraphQLError('Not found', {
    extensions: { code: 'NOT_FOUND' },
  });
}

/**
 * Gate a dispatcher resolver on admin access.
 *
 * Returns the user object (narrowed to non-null) on success, throws a
 * "not found" GraphQLError on failure. Always throws the *same* error for
 * unauthenticated and non-admin callers — never distinguish between the
 * two in responses.
 */
export function requireDispatcherAdmin(user: AuthUser | null): AuthUser {
  if (!user || user.role !== 'admin') {
    notFound();
  }
  return user;
}

/**
 * Destructive commands that require a fresh re-auth (MFA) step before
 * execution per §17 Risk 6. In Phase 1 the check is a placeholder — any
 * non-empty `X-MFA-Token` header is accepted. Sub-task E or a follow-up
 * will wire the real MFA challenge flow (see TODO in resolvers.ts).
 */
export const DESTRUCTIVE_COMMANDS = new Set(['stop', 'drain', 'force_kill']);
