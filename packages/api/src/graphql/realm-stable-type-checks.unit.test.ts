/**
 * Unit tests for the realm-stable graphql-js type discriminators
 * (issue #4198).
 *
 * The helpers in `realm-stable-type-checks.ts` exist because
 * `instanceof GraphQLObjectType` silently returns `false` across the
 * vitest-ESM / Apollo-CJS realm gap. Tests here cover the
 * Symbol.toStringTag-based replacements directly.
 *
 * The cross-realm scenario itself is exercised end-to-end by the
 * cost-limit-plugin and cost-breakdown unit tests — they build a
 * schema in one realm and walk it in another. These tests pin the
 * pure-function semantics of `typeTag`, `isComposite`, and
 * `isObjectOrInterface` against synthesized objects so a future
 * refactor cannot quietly change their meaning.
 */

import { describe, expect, it } from 'vitest';

import {
  typeTag,
  isComposite,
  isObjectOrInterface,
} from './realm-stable-type-checks';

/** Synthesize a graphql-js-shaped object with a given Symbol.toStringTag. */
function tagged(tag: string, extra: Record<string, unknown> = {}): unknown {
  return { [Symbol.toStringTag]: tag, ...extra };
}

describe('typeTag', () => {
  it('returns the Symbol.toStringTag value when present', () => {
    expect(typeTag(tagged('GraphQLObjectType'))).toBe('GraphQLObjectType');
    expect(typeTag(tagged('GraphQLNonNull'))).toBe('GraphQLNonNull');
    expect(typeTag(tagged('GraphQLList'))).toBe('GraphQLList');
  });

  it('falls back to the constructor name when the symbol is missing', () => {
    class GraphQLEnumType {}
    const inst = new GraphQLEnumType();
    expect(typeTag(inst)).toBe('GraphQLEnumType');
  });

  it('returns undefined for null, undefined, primitives', () => {
    expect(typeTag(null)).toBeUndefined();
    expect(typeTag(undefined)).toBeUndefined();
    expect(typeTag('GraphQLObjectType')).toBeUndefined();
    expect(typeTag(42)).toBeUndefined();
    expect(typeTag(true)).toBeUndefined();
  });
});

describe('isComposite', () => {
  it('is true for Object, Interface, and Union', () => {
    // We cast through unknown because the helpers accept any
    // graphql-js-shaped object — the realm-bound GraphQLOutputType
    // type is irrelevant at runtime.
    expect(isComposite(tagged('GraphQLObjectType') as never)).toBe(true);
    expect(isComposite(tagged('GraphQLInterfaceType') as never)).toBe(true);
    expect(isComposite(tagged('GraphQLUnionType') as never)).toBe(true);
  });

  it('is false for Scalar, Enum, NonNull, List', () => {
    expect(isComposite(tagged('GraphQLScalarType') as never)).toBe(false);
    expect(isComposite(tagged('GraphQLEnumType') as never)).toBe(false);
    expect(isComposite(tagged('GraphQLNonNull') as never)).toBe(false);
    expect(isComposite(tagged('GraphQLList') as never)).toBe(false);
  });
});

describe('isObjectOrInterface', () => {
  it('is true for Object and Interface, NOT for Union', () => {
    // Union types do not have `.getFields()`, so this narrower
    // predicate excludes them. cost-breakdown.ts relies on this when
    // it descends into a selection set.
    expect(isObjectOrInterface(tagged('GraphQLObjectType'))).toBe(true);
    expect(isObjectOrInterface(tagged('GraphQLInterfaceType'))).toBe(true);
    expect(isObjectOrInterface(tagged('GraphQLUnionType'))).toBe(false);
  });

  it('is false for non-types', () => {
    expect(isObjectOrInterface(null)).toBe(false);
    expect(isObjectOrInterface(undefined)).toBe(false);
    expect(isObjectOrInterface({})).toBe(false);
    expect(isObjectOrInterface('GraphQLObjectType')).toBe(false);
  });
});
