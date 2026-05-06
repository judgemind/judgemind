/**
 * Per-field cost breakdown for the GraphQL `graphql.cost` log line.
 *
 * Issue #4101 inlines the breakdown into the existing `graphql.cost`
 * structured log so a single CloudWatch line tells operators which
 * selection drove a request's cost — no more re-deriving the breakdown
 * by hand from the schema's per-field weights. The breakdown is a
 * `{ [path]: cost }` map keyed by GraphQL path, e.g.
 * `dispatcherState.activeAgents`, `dispatcherState.recentCompletions`.
 *
 * Earlier iteration (issue #4100) emitted a separate
 * `graphql.cost.breakdown` log line gated on `cost >= 800`. That worked
 * for diagnosing cap rejections but didn't help operators inspecting a
 * specific request — they had to grep the breakdown line that may not
 * exist for their query. #4101 collapses the two lines into one and
 * removes the threshold gate so every `graphql.cost` line carries the
 * answer.
 *
 * Algorithm (mirrors `cost-rule-estimator.ts`):
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
 *
 * Emission depth: the breakdown emits one map entry per field at depth
 * `BREAKDOWN_DEPTH` (default 2). For each top-level operation field
 *   - If it is scalar/leaf: emit `{<top>: cost}` at depth 1.
 *   - If composite: emit one entry per direct child (`<top>.<child>:
 *     cost`) where `cost` is the full subtree cost rolled up to that
 *     child, plus an additional `<top>:__self` entry capturing the
 *     top-level field's own contribution (object cost — usually 0 — and
 *     any auto-injected `__typename` at that level). Sub-children of
 *     composite children are NOT recursed further; their subtree costs
 *     are rolled into the depth-2 entry.
 *   - Sum of all emitted values equals the operation's total cost. AC2
 *     of #4101 pins this invariant in `cost-breakdown.unit.test.ts`.
 *
 * Why depth=2: the polled `DispatcherState` query has one heavy
 * top-level field (`dispatcherState`) whose ten children are exactly
 * what operators want to see in the breakdown. Going deeper would
 * explode log size for queries with multiple list-of-objects branches;
 * stopping at depth 2 keeps the map under `BREAKDOWN_FIELD_CAP` entries
 * for every production query while naming the dominant cost driver.
 */

import {
  type DocumentNode,
  type FieldNode,
  type FragmentDefinitionNode,
  type GraphQLSchema,
  type GraphQLOutputType,
  type OperationDefinitionNode,
  type SelectionSetNode,
  Kind,
} from 'graphql';
// Realm-stable type discriminators — see realm-stable-type-checks.ts
// for the full background. CI guards against a future regression
// reaching for `instanceof GraphQLObjectType` again
// (`scripts/check-no-graphql-instanceof.sh`, issue #4198).
import {
  type CompositeTypeLike,
  isComposite,
  isObjectOrInterface,
  typeTag,
} from './realm-stable-type-checks';

/**
 * Default cost-rule options must match the constants exported by
 * `cost-rule-estimator.ts` (which are used by the production
 * `getComplexity` call in `cost-limit-plugin.ts`). Kept here as named
 * constants so a future option change can be mirrored by editing one
 * place — the `cost-rule-estimator.unit.test.ts` AC3 assertion enforces
 * agreement between the two implementations on the polled query.
 */
const SCALAR_COST = 1;
const OBJECT_COST = 0;
const LIST_FACTOR = 10;

/**
 * Depth at which the breakdown emits per-path entries. Depth 1 is the
 * operation's root selection (e.g. `dispatcherState`), depth 2 is its
 * direct children (`dispatcherState.activeAgents`). Beyond this depth
 * subtree costs are rolled up to the depth-2 ancestor. Tunable for
 * tests; production wiring uses the default.
 */
const BREAKDOWN_DEPTH = 2;

/**
 * Cap on the number of entries emitted in the breakdown map. Prevents a
 * pathological deep query from blowing the log line size past
 * CloudWatch's 256 KB per-event ceiling. Top-level fields plus their
 * direct children on the dispatcher state query are well under 20.
 *
 * When the cap is hit, the lowest-cost entries are merged into a
 * single `__truncated` bucket so AC2's sum-equals-total invariant is
 * preserved.
 */
const BREAKDOWN_FIELD_CAP = 32;

/**
 * Special suffix used when a composite field at the breakdown depth has
 * its own non-zero contribution to the total (e.g. an auto-injected
 * `__typename` directly on the field, or a non-zero `OBJECT_COST`).
 * Emitted as `<path>:__self` so the entry doesn't collide with a real
 * child field's path.
 */
const SELF_SUFFIX = ':__self';

/**
 * Special key used when the breakdown is truncated to honor
 * `BREAKDOWN_FIELD_CAP`. Carries the rolled-up cost of the dropped
 * entries so `Object.values(breakdown).reduce((a, b) => a + b, 0)`
 * still equals the operation's total cost.
 */
const TRUNCATED_KEY = '__truncated';

/**
 * Unwrap NonNull/List wrappers and return the unwrapped (named) type
 * plus the cumulative list-factor multiplier picked up along the way.
 *
 * `typeTag` (and the `isComposite` / `isObjectOrInterface` helpers
 * used elsewhere in this file) come from
 * `realm-stable-type-checks.ts` — see that file's docstring for the
 * realm-clash rationale.
 */
function unwrapType(
  type: GraphQLOutputType,
  factor: number = 1,
): { named: GraphQLOutputType; factor: number } {
  const tag = typeTag(type);
  if (tag === 'GraphQLNonNull') {
    return unwrapType(
      (type as unknown as { ofType: GraphQLOutputType }).ofType,
      factor,
    );
  }
  if (tag === 'GraphQLList') {
    return unwrapType(
      (type as unknown as { ofType: GraphQLOutputType }).ofType,
      factor * LIST_FACTOR,
    );
  }
  return { named: type, factor };
}

/**
 * Cost of a single Field node (without descending into its selection
 * set yet). Mirrors `ComplexityVisitor.getTypeCost`: scalars/enums cost
 * `SCALAR_COST`, object/interface types cost `OBJECT_COST`.
 */
function leafFieldCost(named: GraphQLOutputType): number {
  return isComposite(named) ? OBJECT_COST : SCALAR_COST;
}

/**
 * Walk a selection set under `parentType` with running `costFactor`
 * and return the total cost for this subtree. Pure walk — no
 * breakdown bookkeeping.
 *
 * `__typename` is treated as a String! scalar (cost 1 × current factor),
 * matching what Apollo Client injects on every selection set.
 */
function walkSelectionSet(
  schema: GraphQLSchema,
  selectionSet: SelectionSetNode | undefined,
  parentType: CompositeTypeLike,
  costFactor: number,
  fragments: Map<string, FragmentDefinitionNode>,
): number {
  if (!selectionSet) return 0;
  let total = 0;
  for (const sel of selectionSet.selections) {
    if (sel.kind === Kind.FIELD) {
      total += walkField(schema, sel, parentType, costFactor, fragments);
    } else if (sel.kind === Kind.INLINE_FRAGMENT) {
      const condName = sel.typeCondition?.name.value ?? parentType.name;
      const condType = schema.getType(condName);
      if (isObjectOrInterface(condType)) {
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
      if (frag) {
        const condName = frag.typeCondition.name.value;
        const condType = schema.getType(condName);
        if (isObjectOrInterface(condType)) {
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
  parentType: CompositeTypeLike,
  costFactor: number,
  fragments: Map<string, FragmentDefinitionNode>,
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
  if (field.selectionSet && isObjectOrInterface(named)) {
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
 * Walk one selection set and emit per-path costs at exactly the
 * configured depth (relative to the root operation's selection).
 *
 * - At `currentDepth < targetDepth`: descend into composite fields,
 *   emit recursively. Scalar fields at less-than-target depth are
 *   emitted at their own depth (they have no children to descend into).
 * - At `currentDepth === targetDepth`: emit each field's full subtree
 *   cost as one map entry, and stop recursing.
 *
 * `__typename` is emitted under the parent path with the `:__self`
 * suffix, alongside any non-zero object cost contribution from the
 * parent itself.
 */
function emitBreakdown(
  schema: GraphQLSchema,
  selectionSet: SelectionSetNode | undefined,
  parentType: CompositeTypeLike,
  costFactor: number,
  fragments: Map<string, FragmentDefinitionNode>,
  pathPrefix: string,
  currentDepth: number,
  targetDepth: number,
  out: Map<string, number>,
): void {
  if (!selectionSet) return;
  for (const sel of selectionSet.selections) {
    if (sel.kind === Kind.FIELD) {
      emitField(
        schema,
        sel,
        parentType,
        costFactor,
        fragments,
        pathPrefix,
        currentDepth,
        targetDepth,
        out,
      );
    } else if (sel.kind === Kind.INLINE_FRAGMENT) {
      const condName = sel.typeCondition?.name.value ?? parentType.name;
      const condType = schema.getType(condName);
      if (isObjectOrInterface(condType)) {
        emitBreakdown(
          schema,
          sel.selectionSet,
          condType,
          costFactor,
          fragments,
          pathPrefix,
          currentDepth,
          targetDepth,
          out,
        );
      }
    } else if (sel.kind === Kind.FRAGMENT_SPREAD) {
      const frag = fragments.get(sel.name.value);
      if (frag) {
        const condName = frag.typeCondition.name.value;
        const condType = schema.getType(condName);
        if (isObjectOrInterface(condType)) {
          emitBreakdown(
            schema,
            frag.selectionSet,
            condType,
            costFactor,
            fragments,
            pathPrefix,
            currentDepth,
            targetDepth,
            out,
          );
        }
      }
    }
  }
}

function emitField(
  schema: GraphQLSchema,
  field: FieldNode,
  parentType: CompositeTypeLike,
  costFactor: number,
  fragments: Map<string, FragmentDefinitionNode>,
  pathPrefix: string,
  currentDepth: number,
  targetDepth: number,
  out: Map<string, number>,
): void {
  const fieldName = field.name.value;
  const path = pathPrefix === '' ? fieldName : `${pathPrefix}.${fieldName}`;

  if (fieldName === '__typename') {
    // Auto-injected meta-field; bucket under the parent path's :__self
    // entry so we don't emit a `<top>.__typename` entry for every level.
    const selfPath =
      pathPrefix === ''
        ? `${TRUNCATED_KEY}${SELF_SUFFIX}` // operation-level typename — rare
        : `${pathPrefix}${SELF_SUFFIX}`;
    addCost(out, selfPath, costFactor * SCALAR_COST);
    return;
  }

  const fieldDef = parentType.getFields()[fieldName];
  if (!fieldDef) return; // defensive — graphql-js already rejected.

  const { named, factor: typeFactor } = unwrapType(fieldDef.type);
  const newFactor = costFactor * typeFactor;
  const ownCost = newFactor * leafFieldCost(named);

  const compositeNamed = isObjectOrInterface(named);

  if (currentDepth + 1 >= targetDepth || !compositeNamed || !field.selectionSet) {
    // Emit at this depth — roll up the entire subtree into a single
    // entry. This is the leaf of the breakdown emission tree.
    let subtree = ownCost;
    if (compositeNamed && field.selectionSet) {
      subtree += walkSelectionSet(
        schema,
        field.selectionSet,
        named,
        newFactor,
        fragments,
      );
    }
    addCost(out, path, subtree);
    return;
  }

  // currentDepth + 1 < targetDepth AND this is a composite with a
  // selection set: descend.
  if (ownCost !== 0) {
    addCost(out, `${path}${SELF_SUFFIX}`, ownCost);
  }
  emitBreakdown(
    schema,
    field.selectionSet,
    named,
    newFactor,
    fragments,
    path,
    currentDepth + 1,
    targetDepth,
    out,
  );
}

function addCost(out: Map<string, number>, path: string, cost: number): void {
  if (cost === 0) return;
  out.set(path, (out.get(path) ?? 0) + cost);
}

/**
 * Walk a document and return the per-path cost breakdown plus total.
 *
 * The breakdown map's values sum to the returned `total`, within the
 * algorithm's exact integer arithmetic — AC2 of #4101 pins this in
 * `cost-breakdown.unit.test.ts`.
 */
export function computeBreakdown(
  schema: GraphQLSchema,
  document: DocumentNode,
  operationName?: string | null,
  options: { depth?: number; fieldCap?: number } = {},
): {
  total: number;
  breakdown: Record<string, number>;
  operationName: string | null;
} {
  const depth = options.depth ?? BREAKDOWN_DEPTH;
  const fieldCap = options.fieldCap ?? BREAKDOWN_FIELD_CAP;

  const fragments = new Map<string, FragmentDefinitionNode>();
  for (const def of document.definitions) {
    if (def.kind === Kind.FRAGMENT_DEFINITION) {
      fragments.set(def.name.value, def);
    }
  }

  const op = pickOperation(document, operationName);
  if (!op) return { total: 0, breakdown: {}, operationName: operationName ?? null };

  const rootType =
    op.operation === 'query'
      ? schema.getQueryType()
      : op.operation === 'mutation'
        ? schema.getMutationType()
        : schema.getSubscriptionType();
  if (!rootType) {
    return { total: 0, breakdown: {}, operationName: op.name?.value ?? null };
  }

  const out = new Map<string, number>();
  emitBreakdown(
    schema,
    op.selectionSet,
    rootType,
    1,
    fragments,
    '',
    0,
    depth,
    out,
  );

  // Compute total by summing emitted entries — the emission walk is
  // structurally equivalent to a complete walk (every leaf in the
  // selection AST contributes exactly once, either as its own entry or
  // rolled into an ancestor's subtree entry), so the sum equals what
  // `walkSelectionSet` would have returned at the root. Pinning it this
  // way (rather than running a second `walkSelectionSet`) preserves
  // AC3's "no new traversal" requirement: one walk produces both the
  // breakdown and the total.
  let total = 0;
  for (const v of out.values()) total += v;

  // Honor BREAKDOWN_FIELD_CAP: if more entries than the cap, sort
  // descending by cost, keep the top (cap - 1) entries, and roll the
  // rest into a single `__truncated` bucket. Sum still equals total.
  const breakdown = truncateBreakdown(out, fieldCap);

  return { total, breakdown, operationName: op.name?.value ?? null };
}

function truncateBreakdown(
  out: Map<string, number>,
  fieldCap: number,
): Record<string, number> {
  if (out.size <= fieldCap) {
    return Object.fromEntries(out);
  }
  const sorted = [...out.entries()].sort((a, b) => b[1] - a[1]);
  const kept = sorted.slice(0, fieldCap - 1);
  const dropped = sorted.slice(fieldCap - 1);
  let truncatedSum = 0;
  for (const [, v] of dropped) truncatedSum += v;
  const result: Record<string, number> = Object.fromEntries(kept);
  // Merge into any existing __truncated entry (rare — happens when an
  // operation-level __typename sits in the same bucket).
  result[TRUNCATED_KEY] = (result[TRUNCATED_KEY] ?? 0) + truncatedSum;
  return result;
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
  return ops[0] ?? null;
}

export const __testing = {
  SCALAR_COST,
  OBJECT_COST,
  LIST_FACTOR,
  BREAKDOWN_DEPTH,
  BREAKDOWN_FIELD_CAP,
  SELF_SUFFIX,
  TRUNCATED_KEY,
};
