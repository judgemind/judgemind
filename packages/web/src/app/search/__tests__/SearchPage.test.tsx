import { describe, it, expect } from 'vitest';
import {
  buildSearchParams,
  parseSearchParams,
  MOTION_TYPES,
  MOTION_TYPE_LABELS,
  OUTCOMES,
  OUTCOME_LABELS,
} from '../SearchPage';

// ---------------------------------------------------------------------------
// buildSearchParams — URL encoding of filter state (#1105)
// ---------------------------------------------------------------------------

describe('buildSearchParams', () => {
  it('includes motionTypes when non-empty', () => {
    const params = buildSearchParams({
      q: '',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: ['demurrer', 'msj'],
      outcomes: [],
    });
    expect(params.get('motion')).toBe('demurrer,msj');
  });

  it('includes outcomes when non-empty', () => {
    const params = buildSearchParams({
      q: '',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: ['granted', 'denied'],
    });
    expect(params.get('outcome')).toBe('granted,denied');
  });

  it('omits motionTypes and outcomes when empty', () => {
    const params = buildSearchParams({
      q: 'test',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: [],
    });
    expect(params.has('motion')).toBe(false);
    expect(params.has('outcome')).toBe(false);
  });

  it('includes all filter fields when populated', () => {
    const params = buildSearchParams({
      q: 'summary judgment',
      county: 'Los Angeles',
      judgeName: 'Smith',
      dateFrom: '2026-01-01',
      dateTo: '2026-03-01',
      motionTypes: ['msj'],
      outcomes: ['granted'],
    });
    expect(params.get('q')).toBe('summary judgment');
    expect(params.get('county')).toBe('Los Angeles');
    expect(params.get('judge')).toBe('Smith');
    expect(params.get('dateFrom')).toBe('2026-01-01');
    expect(params.get('dateTo')).toBe('2026-03-01');
    expect(params.get('motion')).toBe('msj');
    expect(params.get('outcome')).toBe('granted');
  });
});

// ---------------------------------------------------------------------------
// parseSearchParams — URL decoding of filter state (#1105)
// ---------------------------------------------------------------------------

describe('parseSearchParams', () => {
  it('parses motionTypes from comma-separated string', () => {
    const params = new URLSearchParams('motion=demurrer,msj');
    const state = parseSearchParams(params);
    expect(state.motionTypes).toEqual(['demurrer', 'msj']);
  });

  it('parses outcomes from comma-separated string', () => {
    const params = new URLSearchParams('outcome=granted,denied');
    const state = parseSearchParams(params);
    expect(state.outcomes).toEqual(['granted', 'denied']);
  });

  it('returns empty arrays when motion and outcome are absent', () => {
    const params = new URLSearchParams('q=test');
    const state = parseSearchParams(params);
    expect(state.motionTypes).toEqual([]);
    expect(state.outcomes).toEqual([]);
  });

  it('round-trips through buildSearchParams', () => {
    const original = {
      q: 'test',
      county: 'Los Angeles',
      judgeName: 'Smith',
      dateFrom: '2026-01-01',
      dateTo: '2026-03-01',
      motionTypes: ['demurrer', 'msj'],
      outcomes: ['granted'],
    };
    const params = buildSearchParams(original);
    const parsed = parseSearchParams(params);
    expect(parsed).toEqual(original);
  });
});

// ---------------------------------------------------------------------------
// Constants — motion types and outcomes (#1105)
// ---------------------------------------------------------------------------

describe('MOTION_TYPES', () => {
  it('has labels for every motion type', () => {
    for (const mt of MOTION_TYPES) {
      expect(MOTION_TYPE_LABELS[mt]).toBeDefined();
    }
  });
});

describe('OUTCOMES', () => {
  it('has labels for every outcome', () => {
    for (const oc of OUTCOMES) {
      expect(OUTCOME_LABELS[oc]).toBeDefined();
    }
  });
});
