/**
 * Unit tests for judge analytics helper functions.
 * No database or network dependencies.
 */

import { describe, it, expect } from 'vitest';
import { computeGrantRate } from '../src/graphql/judge-analytics';

describe('computeGrantRate', () => {
  it('returns 0 when denominator is zero (no substantive rulings)', () => {
    expect(computeGrantRate(0, 0, 0)).toBe(0);
  });

  it('returns 1.0 when all rulings are granted', () => {
    expect(computeGrantRate(5, 0, 0)).toBe(1);
  });

  it('returns 0 when no rulings are granted', () => {
    expect(computeGrantRate(0, 5, 0)).toBe(0);
  });

  it('computes correctly with mixed outcomes', () => {
    // 3 granted, 1 denied, 1 granted_in_part => 3/5 = 0.6
    expect(computeGrantRate(3, 1, 1)).toBeCloseTo(0.6, 10);
  });

  it('excludes procedural outcomes from denominator by design', () => {
    // granted=2, denied=1, grantedInPart=1 => 2/(2+1+1) = 0.5
    // (moot/continued/etc. are not passed to this function)
    expect(computeGrantRate(2, 1, 1)).toBeCloseTo(0.5, 10);
  });

  it('handles single granted ruling', () => {
    expect(computeGrantRate(1, 0, 0)).toBe(1);
  });

  it('handles single denied ruling', () => {
    expect(computeGrantRate(0, 1, 0)).toBe(0);
  });
});
