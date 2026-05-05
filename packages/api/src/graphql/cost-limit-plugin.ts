/**
 * Apollo Server v4 plugin enforcing the per-query cost cap from #4003.
 *
 * Issue #4112. Replaces the retired predecessor cost-rule library's
 * validation rule with a `graphql-query-complexity`-backed plugin. The
 * plugin runs
 * in `didResolveOperation` (same lifecycle slot as `cost-breakdown.ts`),
 * which means:
 *
 *   1. The query is parsed and pre-validated by graphql-js's standard
 *      validation pass (so the document AST and TypeInfo are valid by
 *      the time we look at it).
 *   2. The full operation context — including request variables — is
 *      available, which is what `getComplexity()` needs to evaluate
 *      `@include(if: $var)` / `@skip(if: $var)` directives correctly.
 *      A `validationRules` entry would only see the variables passed at
 *      `createComplexityRule()` construction time (server boot), which
 *      doesn't match per-request reality and made `getVariableValues()`
 *      report "Variable not provided" errors during tests/under vitest.
 *   3. Throwing a GraphQLError here aborts execution before resolvers
 *      run, with the same "HTTP 400 + GRAPHQL_VALIDATION_FAILED" shape
 *      callers got under the previous library, so the public API
 *      contract is preserved.
 *
 * The estimator (`judgemindEstimator`) mirrors the retired
 * predecessor library's algorithm exactly — scalarCost=1,
 * objectCost=0, listFactor=10 — keeping the production 1000-cap from
 * #4003 unchanged and keeping the per-field breakdown logger in
 * lockstep with the rule. See `cost-rule-estimator.ts` for the full
 * algorithm rationale.
 */

import type { ApolloServerPlugin } from '@apollo/server';
import { GraphQLError } from 'graphql';
// Same realm-pinning rationale as cost-rule-estimator.ts — see the
// comment there. We import the value via the `/cjs` subpath so the
// library's runtime stays in graphql's CJS realm under both production
// (Node CJS) and vitest's ESM loader.
import { getComplexity } from 'graphql-query-complexity/cjs';
import { judgemindEstimator } from './cost-rule-estimator';

export interface CostLimitPluginOptions {
  /** Maximum allowed cost. Requests over the cap are rejected. */
  maximumCost: number;
  /** Optional callback invoked with every computed cost (for logging). */
  onCost?: (cost: number) => void;
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
          if (onCost) onCost(cost);
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
