import { DispatcherDashboard } from './DispatcherDashboard';

/**
 * /admin/dispatcher — dispatcher v2 admin page (see docs/specs/dispatcher-v2-spec.md §11).
 *
 * The 404 behavior for non-admins is implemented in `DispatcherDashboard`
 * (client component) because the admin role is only known after the
 * `AuthProvider` has hydrated on the client. Rendering a server `notFound()`
 * would require reading the session cookie on the server, which the web
 * app intentionally does not do (auth state is Apollo-client only).
 *
 * The GraphQL resolver also returns a generic `NOT_FOUND` error for
 * non-admins — see `packages/api/src/graphql/dispatcher/auth.ts` — so the
 * existence of the dispatcher schema is not enumerable from a non-admin
 * session.
 */
export default function DispatcherPage() {
  return (
    <div className="mx-auto max-w-6xl">
      <DispatcherDashboard />
    </div>
  );
}
