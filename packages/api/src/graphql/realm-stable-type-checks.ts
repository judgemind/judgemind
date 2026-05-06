/**
 * Realm-stable graphql-js type discriminators.
 *
 * Why this file exists
 * --------------------
 * graphql-js maintains separate class identities across its CJS and
 * ESM builds. The library's own `instanceOf` helper at
 * `graphql/jsutils/instanceOf.mjs` even throws when both realms have
 * loaded. Vitest's ESM loader can resolve `graphql` and
 * `graphql/index.mjs` as distinct module instances, and Apollo Server
 * pins itself to the CJS realm (see the `/cjs` subpath import in
 * `cost-limit-plugin.ts`). Consequence: a check like
 *
 *   field.type instanceof GraphQLObjectType
 *
 * silently returns `false` for a structurally-correct object type
 * when the schema and the walker live in different realms — every
 * type collapses to a scalar leaf with no error or warning.
 *
 * The realm-stable workaround is `Symbol.toStringTag`. graphql-js
 * sets it on every type class (e.g. `'GraphQLObjectType'`,
 * `'GraphQLNonNull'`), and the symbol value is the same string
 * regardless of which realm produced the class. This file centralizes
 * the helpers so the api graphql walker source has exactly one place
 * to maintain them — and `scripts/check-no-graphql-instanceof.sh`
 * enforces by CI that no future walker reaches for `instanceof`
 * again. See issues #4101 and #4198.
 *
 * Scope: `packages/api/src/graphql/`. Other directories do not
 * import from `graphql` directly so the realm question does not
 * arise.
 */

import type { GraphQLOutputType } from 'graphql';

/**
 * Read `Symbol.toStringTag` on a graphql-js type, falling back to
 * the constructor name. Returns `undefined` for non-objects.
 *
 * graphql-js sets `[Symbol.toStringTag]` on every type class, so
 * this is a realm-stable discriminator — unlike `instanceof`, which
 * is bound to a specific realm's class identity.
 */
export function typeTag(t: unknown): string | undefined {
  if (t == null || typeof t !== 'object') return undefined;
  const sym = (t as { [Symbol.toStringTag]?: string })[Symbol.toStringTag];
  if (typeof sym === 'string') return sym;
  const ctor = (t as { constructor?: { name?: string } }).constructor;
  return ctor?.name;
}

/**
 * Composite type as duck-typed by graphql-js's Object/Interface
 * surface (the only methods the api graphql walker source needs).
 * Using a structural type instead of the realm-bound
 * `GraphQLObjectType | GraphQLInterfaceType` union keeps the walker
 * realm-stable — see the file-level docstring.
 */
export interface CompositeTypeLike {
  name: string;
  getFields(): Record<string, { type: GraphQLOutputType }>;
}

/**
 * True for Object, Interface, AND Union types — i.e. anything whose
 * leaf cost in the cost-rule walker is `OBJECT_COST` rather than
 * `SCALAR_COST`. Note: Union types do NOT satisfy
 * `CompositeTypeLike` (they have no `.getFields()`); use
 * `isObjectOrInterface` when you need to narrow to something with
 * `.getFields()`.
 */
export function isComposite(named: GraphQLOutputType): boolean {
  const tag = typeTag(named);
  return (
    tag === 'GraphQLObjectType' ||
    tag === 'GraphQLInterfaceType' ||
    tag === 'GraphQLUnionType'
  );
}

/**
 * Type predicate narrowing to `CompositeTypeLike` — true only for
 * Object/Interface types (Union has no `.getFields()`). Use this
 * when you need to descend into a selection set; use `isComposite`
 * when you only need to know "does this type cost `OBJECT_COST`?".
 */
export function isObjectOrInterface(t: unknown): t is CompositeTypeLike {
  const tag = typeTag(t);
  return tag === 'GraphQLObjectType' || tag === 'GraphQLInterfaceType';
}
