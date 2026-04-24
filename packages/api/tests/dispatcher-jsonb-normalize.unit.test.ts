/**
 * Unit tests for `normalizeJsonbString` — the shared JSONB scalar
 * normaliser used by `querySpawnFrozenUntil` and `queryCapFlippedBy`
 * (#2874).
 *
 * node-postgres unwraps JSONB scalars to native JS values by default
 * (e.g. a JSONB string `"circuit_breaker"` becomes the JS string
 * `'circuit_breaker'`). `normalizeJsonbString` accepts that unwrapped
 * shape and returns `null` for any non-string value.
 */

import { describe, expect, it } from 'vitest';
import { normalizeJsonbString } from '../src/graphql/dispatcher/resolvers';

describe('normalizeJsonbString — string inputs', () => {
  it('returns the string unchanged for a non-empty string', () => {
    expect(normalizeJsonbString('circuit_breaker')).toBe('circuit_breaker');
  });

  it('returns an empty string for an empty string input', () => {
    // pg unwraps JSONB string "" to the JS empty string ''.
    expect(normalizeJsonbString('')).toBe('');
  });

  it('returns an ISO-8601 timestamp string unchanged', () => {
    const ts = '2026-04-24T12:00:00Z';
    expect(normalizeJsonbString(ts)).toBe(ts);
  });
});

describe('normalizeJsonbString — non-string inputs return null', () => {
  it('returns null for null', () => {
    expect(normalizeJsonbString(null)).toBeNull();
  });

  it('returns null for undefined', () => {
    expect(normalizeJsonbString(undefined)).toBeNull();
  });

  it('returns null for a number', () => {
    expect(normalizeJsonbString(42)).toBeNull();
  });

  it('returns null for a boolean true', () => {
    expect(normalizeJsonbString(true)).toBeNull();
  });

  it('returns null for a boolean false', () => {
    expect(normalizeJsonbString(false)).toBeNull();
  });

  it('returns null for an object', () => {
    expect(normalizeJsonbString({ key: 'value' })).toBeNull();
  });

  it('returns null for an array', () => {
    expect(normalizeJsonbString([1, 2, 3])).toBeNull();
  });
});
