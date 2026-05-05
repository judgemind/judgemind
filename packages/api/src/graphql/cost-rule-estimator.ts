/**
 * Custom complexity estimator for `graphql-query-complexity`.
 *
 * Issue #4112. We migrated from an unmaintained predecessor cost-rule
 * library to the maintained `graphql-query-complexity@1.x`. The new
 * library's API is bottom-up (each field's estimator returns its own
 * cost given `childComplexity`) instead of the old library's top-down
 * walk with a running `costFactor`.
 *
 * To preserve the existing production cap of 1000 (issue #4003) and the
 * existing relationship with the per-field breakdown logger
 * (`cost-breakdown.ts`), this estimator reproduces the old library's
 * algorithm exactly:
 *
 *   - Scalar / enum / `__typename` field: cost = listFactor * scalarCost
 *   - Object / interface / union field:   cost = listFactor * (objectCost + childComplexity)
 *
 * Where `listFactor` is the cumulative `LIST_FACTOR` (10) wrappers picked
 * up by unwrapping NonNull/List from `field.type`. The running-factor
 * model from the old library is mathematically equivalent to multiplying
 * each subtree by its own field's list factor, because list factors
 * compose multiplicatively as you descend.
 *
 * Why a custom estimator instead of `simpleEstimator`: the bundled
 * `simpleEstimator` adds a fixed `defaultComplexity` per field with no
 * list-factor multiplier, so the polled `DispatcherState` query (cost
 * ~995 today under the multiplicative model) would compute to ~38 under
 * `simpleEstimator(1)`. Switching estimators would also force the
 * production cap to be re-tuned and would decouple the cost-breakdown
 * walker from the cost rule. The acceptance criteria for #4112
 * explicitly call out keeping the 1000-cap and re-enabling the
 * "rule total agrees with breakdown total" test, so we must mirror the
 * old algorithm here.
 *
 * Defaults match the old library's defaults (the call site in `app.ts`
 * never overrode them).
 */

import {
  GraphQLInterfaceType,
  GraphQLList,
  GraphQLNonNull,
  GraphQLObjectType,
  GraphQLUnionType,
  type GraphQLOutputType,
} from 'graphql';
import type { ComplexityEstimator } from 'graphql-query-complexity';

/**
 * Cost of one scalar / enum / `__typename` leaf, before list factor is
 * applied. Mirrors the old library's `scalarCost: 1` default.
 */
export const SCALAR_COST = 1;

/**
 * Cost of one object/interface/union field on its own (the children's
 * cost is added separately as `childComplexity`). Mirrors the old
 * library's `objectCost: 0` default.
 */
export const OBJECT_COST = 0;

/**
 * Multiplier applied for each `[X]` list wrapper between the field's
 * declared type and its named (innermost) type. Two nested lists yield
 * `LIST_FACTOR * LIST_FACTOR = 100`. Mirrors the old library's
 * `listFactor: 10` default.
 */
export const LIST_FACTOR = 10;

/**
 * Unwrap `NonNull`/`List` wrappers and return the named (innermost)
 * type plus the cumulative list-factor multiplier picked up along the
 * way. `NonNull` does not affect the factor; each `List` multiplies it
 * by `LIST_FACTOR`.
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
 * Returns the per-field "leaf" cost — `OBJECT_COST` for composite
 * (Object/Interface/Union) types, `SCALAR_COST` for everything else
 * (Scalar, Enum). The composite vs. leaf split mirrors the old cost-
 * rule library's `getTypeCost` (vendored under `node_modules/` before
 * this PR).
 */
function leafFieldCost(named: GraphQLOutputType): number {
  if (
    named instanceof GraphQLObjectType ||
    named instanceof GraphQLInterfaceType ||
    named instanceof GraphQLUnionType
  ) {
    return OBJECT_COST;
  }
  return SCALAR_COST;
}

/**
 * The estimator wired into `createComplexityRule({ estimators: [...] })`.
 * Returns a number for every Field — never returns `void` — because we
 * want this to be the only estimator (no fallthrough to simpleEstimator).
 *
 * `__typename` (Apollo Client's auto-injected meta-field) flows through
 * here too: graphql-query-complexity walks `TypeNameMetaFieldDef` as a
 * regular Field with `field.type = String!`, which unwraps to a scalar
 * with no list factor → returns `SCALAR_COST = 1`. This matches what the
 * old library counted, so the polled-query total stays the same after
 * the migration.
 */
export const judgemindEstimator: ComplexityEstimator = ({
  field,
  childComplexity,
}) => {
  const { named, factor } = unwrapType(field.type);
  const composite =
    named instanceof GraphQLObjectType ||
    named instanceof GraphQLInterfaceType ||
    named instanceof GraphQLUnionType;
  if (composite) {
    return factor * (OBJECT_COST + childComplexity);
  }
  return factor * leafFieldCost(named);
};
