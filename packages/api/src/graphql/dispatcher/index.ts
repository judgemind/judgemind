/**
 * Dispatcher admin GraphQL module — feeds the admin page at `/admin/dispatcher`.
 *
 * Exports:
 *   - `dispatcherTypeDefs`   → GraphQL SDL for State / Agent / Failure / ...
 *   - `dispatcherResolvers`  → resolvers for queries, mutation, and nested types
 *   - `dispatcherScalarResolvers` → DateTime + JSON scalars
 *
 * See `docs/specs/dispatcher-v2-spec.md` §11 for the surface definition.
 */

export { dispatcherTypeDefs } from './schema';
export {
  dispatcherResolvers,
  dispatcherScalarResolvers,
  agentRowToGraphQL,
  commandRowToGraphQL,
  failureRowToGraphQL,
  insertCommandIdempotent,
  runRowToGraphQL,
  transitionRowToGraphQL,
} from './resolvers';
export { requireDispatcherAdmin, notFound, DESTRUCTIVE_COMMANDS } from './auth';
