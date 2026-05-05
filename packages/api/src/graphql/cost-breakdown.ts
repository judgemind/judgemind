/**
 * Per-field cost breakdown logger for the polled cockpit query
 * (`DispatcherState`) and any other operation flirting with the #4003
 * 1000-cost cap.
 *
 * Issue #4100. The `graphql-validation-complexity` library only exposes
 * the total cost via `onCost` — when an operation overshoots the cap and
 * the API returns HTTP 400, operators have to recompute the per-field
 * breakdown by hand to identify the next slim. This plugin runs the same
 * cost algorithm as `ComplexityVisitor` (vendored in
 * `node_modules/graphql-validation-complexity/lib/ComplexityVisitor.js`)
 * but emits a top-level-field breakdown alongside the total whenever the
 * total exceeds `COST_LOG_THRESHOLD`. Logging only on near-cap operations
 * keeps the volume bounded — the dispatcher polls every 2s and we don't
 * want a per-request breakdown for every cheap query.
 *
 * Algorithm (mirrors `ComplexityVisitor`):
 *   - Walk each Field node in the operation's selection set.
 *   - On enter, multiply the running `costFactor` by the field's type
 *     factor: `listFactor` (default 10) for each list-of-X wrapper,
 *     unwrapping NonNull along the way.
 *   - On enter, add `costFactor * fieldCost` to the running total. Field
 *     cost is `objectCost` (default 0) for object types and `scalarCost`
 *     (default 1) for scalar/enum types — JSON and DateTime are scalars.
 *   - On leave, restore `costFactor`.
 *
 * Apollo's default `addTypename` injects an extra `__typename` field
 * into every selection set, so the breakdown also accounts for that
 * implicit cost (~250 units on the dispatcher state query — see #4100
 * comment thread).
 */

import type { ApolloServerPlugin } from '@apollo/server';
import {
  type DocumentNode,
  type FieldNode,
  type GraphQLSchema,
  type GraphQLOutputType,
  type OperationDefinitionNode,
  type SelectionSetNode,
  GraphQLObjectType,
  GraphQLInterfaceType,
  GraphQLList,
  GraphQLNonNull,
  Kind,
} from 'graphql';

/**
 * Default cost-rule options must match the `createComplexityLimitRule`
 * call in `app.ts` (which uses the library's defaults). Kept here as
 * named constants so a future option change in `app.ts` can be mirrored
 * by editing one place.
 */
const SCALAR_COST = 1;
const OBJECT_COST = 0;
const LIST_FACTOR = 10;

/**
 * Threshold (inclusive) above which the breakdown is logged. The #4003
 * cap is 1000; we log at 800 so operators see breakdowns for queries
 * that are within 200 units of the cap (early-warning band) without
 * spamming logs for routine queries. The threshold is intentionally a
 * compile-time constant — a config-knob would invite drift between
 * environments.
 */
const COST_LOG_THRESHOLD = 800;

/**
 * Cap on the number of top-level fields we emit in the breakdown log.
 * Prevents a pathological deep query from blowing the log line size
 * past CloudWatch's 256 KB per-event ceiling. Top-level fields on the
 * dispatcher state query are well under 20.
 */
const BREAKDOWN_FIELD_CAP = 20;

interface FieldCostEntry {
  /** The dotted path of selection segments, e.g. "queueReady.blockedBy". */
  path: string;
  /** Total cost contributed by this top-level field and its descendants. */
  cost: number;
}

/**
 * Unwrap NonNull/List wrappers and return the unwrapped (named) type
 * plus the cumulative list-factor multiplier picked up along the way.
 */
function unwrapType(
  type: GraphQLOutputType,
  factor: number = 1,
): { named: GraphQLOutputType; factor: number } {
  if (type instanceof GraphQLNonNull) {
    return unwrapType(type.ofType, factor);
  }
  if (type instanceof GraphQLList) {
    return unwrapType(type.ofType, factor * LIST_FACTOR);
  }
  return { named: type, factor };
}

/**
 * Cost of a single Field node (without descending into its selection
 * set yet). Mirrors `ComplexityVisitor.getTypeCost`: scalars/enums cost
 * `SCALAR_COST`, object/interface types cost `OBJECT_COST`.
 */
function leafFieldCost(named: GraphQLOutputType): number {
  if (named instanceof GraphQLObjectType || named instanceof GraphQLInterfaceType) {
    return OBJECT_COST;
  }
  return SCALAR_COST;
}

/**
 * Walk a selection set under `parentType` with running `costFactor`.
 * Adds each Field's cost contribution onto `accumulator` and returns
 * the total cost for this subtree.
 *
 * `__typename` is treated as a String! scalar (cost 1 × current factor),
 * matching what Apollo Client injects on every selection set.
 */
function walkSelectionSet(
  schema: GraphQLSchema,
  selectionSet: SelectionSetNode | undefined,
  parentType: GraphQLObjectType | GraphQLInterfaceType,
  costFactor: number,
  fragments: Map<string, DocumentNode['definitions'][number]>,
): number {
  if (!selectionSet) return 0;
  let total = 0;
  for (const sel of selectionSet.selections) {
    if (sel.kind === Kind.FIELD) {
      total += walkField(schema, sel, parentType, costFactor, fragments);
    } else if (sel.kind === Kind.INLINE_FRAGMENT) {
      const condName = sel.typeCondition?.name.value ?? parentType.name;
      const condType = schema.getType(condName);
      if (
        condType instanceof GraphQLObjectType ||
        condType instanceof GraphQLInterfaceType
      ) {
        total += walkSelectionSet(
          schema,
          sel.selectionSet,
          condType,
          costFactor,
          fragments,
        );
      }
    } else if (sel.kind === Kind.FRAGMENT_SPREAD) {
      const frag = fragments.get(sel.name.value);
      if (frag && frag.kind === Kind.FRAGMENT_DEFINITION) {
        const condName = frag.typeCondition.name.value;
        const condType = schema.getType(condName);
        if (
          condType instanceof GraphQLObjectType ||
          condType instanceof GraphQLInterfaceType
        ) {
          total += walkSelectionSet(
            schema,
            frag.selectionSet,
            condType,
            costFactor,
            fragments,
          );
        }
      }
    }
  }
  return total;
}

function walkField(
  schema: GraphQLSchema,
  field: FieldNode,
  parentType: GraphQLObjectType | GraphQLInterfaceType,
  costFactor: number,
  fragments: Map<string, DocumentNode['definitions'][number]>,
): number {
  const fieldName = field.name.value;
  // Apollo's auto-injected `__typename` is a String! scalar — no
  // selection set, no list factor.
  if (fieldName === '__typename') {
    return costFactor * SCALAR_COST;
  }
  const fieldDef = parentType.getFields()[fieldName];
  if (!fieldDef) {
    // Unknown field — graphql-js validation will reject the operation
    // before it reaches us, so this branch is a defensive no-op.
    return 0;
  }
  const { named, factor: typeFactor } = unwrapType(fieldDef.type);
  const newFactor = costFactor * typeFactor;
  let cost = newFactor * leafFieldCost(named);
  if (
    field.selectionSet &&
    (named instanceof GraphQLObjectType || named instanceof GraphQLInterfaceType)
  ) {
    cost += walkSelectionSet(
      schema,
      field.selectionSet,
      named,
      newFactor,
      fragments,
    );
  }
  return cost;
}

/**
 * Compute the per-top-level-field cost breakdown for one operation.
 * The "top-level" fields are the immediate children of the operation's
 * root selection set — for the cockpit's polled query that is just
 * `dispatcherState`, but if a future query selects multiple roots they
 * each get their own row.
 *
 * Returns an empty array when the operation has no resolvable root type
 * (e.g., a subscription with no schema entry).
 */
export function computeBreakdown(
  schema: GraphQLSchema,
  document: DocumentNode,
  operationName?: string | null,
): { total: number; fields: FieldCostEntry[]; operationName: string | null } {
  const fragments = new Map<string, DocumentNode['definitions'][number]>();
  for (const def of document.definitions) {
    if (def.kind === Kind.FRAGMENT_DEFINITION) {
      fragments.set(def.name.value, def);
    }
  }
  const op = pickOperation(document, operationName);
  if (!op) return { total: 0, fields: [], operationName: operationName ?? null };
  const rootType =
    op.operation === 'query'
      ? schema.getQueryType()
      : op.operation === 'mutation'
        ? schema.getMutationType()
        : schema.getSubscriptionType();
  if (!rootType) {
    return { total: 0, fields: [], operationName: op.name?.value ?? null };
  }
  const fields: FieldCostEntry[] = [];
  let total = 0;
  for (const sel of op.selectionSet.selections) {
    if (sel.kind !== Kind.FIELD) continue;
    const cost = walkField(schema, sel, rootType, 1, fragments);
    fields.push({ path: sel.name.value, cost });
    total += cost;
  }
  fields.sort((a, b) => b.cost - a.cost);
  return { total, fields, operationName: op.name?.value ?? null };
}

function pickOperation(
  document: DocumentNode,
  operationName?: string | null,
): OperationDefinitionNode | null {
  const ops = document.definitions.filter(
    (d): d is OperationDefinitionNode => d.kind === Kind.OPERATION_DEFINITION,
  );
  if (ops.length === 0) return null;
  if (operationName) {
    return ops.find((o) => o.name?.value === operationName) ?? null;
  }
  return ops[0];
}

/**
 * Apollo Server plugin that emits a cost breakdown log line whenever
 * a request's computed cost meets `COST_LOG_THRESHOLD`. Logs through
 * the `log.info` callback so the existing JSON log shape (Pino in
 * Fastify) keeps the breakdown structured and queryable from
 * CloudWatch Insights.
 *
 * Threshold is checked against our own walker's total — if a future
 * graphql-validation-complexity option drift causes the two totals to
 * disagree, the breakdown still fires when our computation crosses
 * 800. The two totals should agree on the polled query (verified by
 * the regression test in `cost-breakdown.test.ts`).
 */
export function costBreakdownPlugin(
  log: (entry: {
    cost: number;
    operationName: string | null;
    breakdown: FieldCostEntry[];
  }) => void,
): ApolloServerPlugin {
  return {
    async requestDidStart() {
      return {
        async didResolveOperation(ctx) {
          const { schema, document, operationName } = ctx;
          if (!schema || !document) return;
          const result = computeBreakdown(schema, document, operationName);
          if (result.total >= COST_LOG_THRESHOLD) {
            log({
              cost: result.total,
              operationName: result.operationName,
              breakdown: result.fields.slice(0, BREAKDOWN_FIELD_CAP),
            });
          }
        },
      };
    },
  };
}

export const __testing = {
  COST_LOG_THRESHOLD,
  SCALAR_COST,
  OBJECT_COST,
  LIST_FACTOR,
};
