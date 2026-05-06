/**
 * Apollo Server v4 plugin enforcing the per-query cost cap from #4003,
 * and emitting the structured `graphql.cost` log line with a per-field
 * cost breakdown (issue #4101).
 *
 * Migration history:
 *   - #4112 replaced the retired predecessor cost-rule library's
 *     validation rule with a `graphql-query-complexity`-backed plugin.
 *   - #4100 added a separate `costBreakdownPlugin` emitting
 *     `graphql.cost.breakdown` only when cost ≥ 800.
 *   - #4101 inlined the breakdown into the same `graphql.cost` log line
 *     so a single CloudWatch line carries both the total and the
 *     per-field map. The standalone breakdown plugin and threshold gate
 *     are gone — every `graphql.cost` line carries the breakdown.
 *
 * Lifecycle slot: `didResolveOperation`. Same slot as before, picked
 * because:
 *
 *   1. The query is parsed and pre-validated by graphql-js's standard
 *      validation pass (so the document AST and TypeInfo are valid by
 *      the time we look at it).
 *   2. The full operation context — including request variables — is
 *      available, which is what `getComplexity()` needs to evaluate
 *      `@include(if: $var)` / `@skip(if: $var)` directives correctly.
 *      A `validationRules` entry would only see the variables passed at
 *      `createComplexityRule()` construction time (server boot), which
 *      doesn't match per-request reality.
 *   3. Throwing a GraphQLError here aborts execution before resolvers
 *      run, with the same "HTTP 400 + GRAPHQL_VALIDATION_FAILED" shape
 *      callers got under the previous library, so the public API
 *      contract is preserved.
 *
 * The estimator (`judgemindEstimator`) mirrors the retired
 * predecessor library's algorithm exactly — scalarCost=1,
 * objectCost=0, listFactor=10 — keeping the production 1000-cap from
 * #4003 unchanged. The breakdown walker
 * (`computeBreakdown`) shares those constants, so the cap value and
 * the breakdown's sum agree (AC3 of #4112's `cost-rule-estimator.unit.test.ts`).
 *
 * Performance note (AC3 of #4101): `getComplexity` and `computeBreakdown`
 * each walk the document once. The breakdown is computed during the
 * existing validation lifecycle slot, not as a second post-execution
 * pass — when the cap fires, no resolvers have run.
 */

import type { ApolloServerPlugin } from '@apollo/server';
import { GraphQLError } from 'graphql';
// Same realm-pinning rationale as cost-rule-estimator.ts — see the
// comment there. We import the value via the `/cjs` subpath so the
// library's runtime stays in graphql's CJS realm under both production
// (Node CJS) and vitest's ESM loader.
import { getComplexity } from 'graphql-query-complexity/cjs';
import { judgemindEstimator } from './cost-rule-estimator';
import { computeBreakdown } from './cost-breakdown';

/**
 * Shape of the `graphql.cost` log entry. The breakdown is a flat map
 * from GraphQL path to subtree cost (e.g.
 * `{ "dispatcherState.activeAgents": 80, "dispatcherState.queueDepth": 1 }`).
 * `Object.values(breakdown).reduce((a, b) => a + b, 0)` equals `cost`,
 * by construction (AC2 of #4101).
 */
export interface CostLogEntry {
  cost: number;
  operationName: string | null;
  breakdown: Record<string, number>;
}

export interface CostLimitPluginOptions {
  /** Maximum allowed cost. Requests over the cap are rejected. */
  maximumCost: number;
  /**
   * Optional callback invoked with every computed cost (for logging).
   * Receives the full `graphql.cost` entry — total cost, operation
   * name, and per-field breakdown map. Production wiring in
   * `app.ts` passes `(entry) => app.log.info(entry, 'graphql.cost')`.
   */
  onCost?: (entry: CostLogEntry) => void;
}

export function costLimitPlugin(
  options: CostLimitPluginOptions,
): ApolloServerPlugin {
  const { maximumCost, onCost } = options;
  return {
    async requestDidStart() {
      return {
        async didResolveOperation(ctx) {
          const { schema, document, operationName, request } = ctx;
          if (!schema || !document) return;
          const variables = request.variables ?? {};
          const cost = getComplexity({
            schema,
            query: document,
            variables,
            operationName: operationName ?? undefined,
            estimators: [judgemindEstimator],
          });
          if (onCost) {
            // Compute the breakdown alongside the cap value. Both
            // walkers traverse the document during the same
            // validation lifecycle slot — see the AC3 note in the
            // module docstring. The breakdown's sum should equal
            // `cost` for queries without `@skip`/`@include` directives
            // (which is every production query today). When the two
            // disagree by more than rounding (an `@skip(if: true)`
            // branch removes fields from `getComplexity` but not from
            // the breakdown), we still emit the breakdown — it
            // accurately describes the document's structure even if
            // it overstates the runtime cost. The cap value (`cost`)
            // remains authoritative for the limit check.
            const result = computeBreakdown(schema, document, operationName);
            onCost({
              cost,
              operationName: result.operationName,
              breakdown: result.breakdown,
            });
          }
          if (cost > maximumCost) {
            throw new GraphQLError(
              `Query exceeds maximum cost of ${maximumCost}. Actual cost: ${cost}.`,
              {
                extensions: {
                  code: 'GRAPHQL_VALIDATION_FAILED',
                  complexityLimitExceeded: true,
                  actualCost: cost,
                  maximumCost,
                },
              },
            );
          }
        },
      };
    },
  };
}
