import { describe, it, expect } from 'vitest';
import {
  buildSearchParams,
  parseSearchParams,
  MOTION_TYPES,
  MOTION_TYPE_LABELS,
  OUTCOMES,
  OUTCOME_LABELS,
} from '../src/app/search/SearchPage';

describe('buildSearchParams', () => {
  it('returns empty params when all fields are empty', () => {
    const params = buildSearchParams({
      q: '',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: [],
    });
    expect(params.toString()).toBe('');
  });

  it('sets q param for query', () => {
    const params = buildSearchParams({
      q: 'summary judgment',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: [],
    });
    expect(params.get('q')).toBe('summary judgment');
  });

  it('sets county param', () => {
    const params = buildSearchParams({
      q: '',
      county: 'Los Angeles',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: [],
    });
    expect(params.get('county')).toBe('Los Angeles');
  });

  it('sets judge param from judgeName', () => {
    const params = buildSearchParams({
      q: '',
      county: '',
      judgeName: 'Smith, John',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: [],
    });
    expect(params.get('judge')).toBe('Smith, John');
  });

  it('sets date range params', () => {
    const params = buildSearchParams({
      q: '',
      county: '',
      judgeName: '',
      dateFrom: '2026-01-01',
      dateTo: '2026-03-31',
      motionTypes: [],
      outcomes: [],
    });
    expect(params.get('dateFrom')).toBe('2026-01-01');
    expect(params.get('dateTo')).toBe('2026-03-31');
  });

  it('sets motion types as comma-separated', () => {
    const params = buildSearchParams({
      q: '',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: ['msj', 'mtd'],
      outcomes: [],
    });
    expect(params.get('motion')).toBe('msj,mtd');
  });

  it('sets outcomes as comma-separated', () => {
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

  it('sets all params when all fields are filled', () => {
    const params = buildSearchParams({
      q: 'motion',
      county: 'Orange',
      judgeName: 'Doe, Jane',
      dateFrom: '2026-01-01',
      dateTo: '2026-12-31',
      motionTypes: ['msj'],
      outcomes: ['granted'],
    });
    expect(params.get('q')).toBe('motion');
    expect(params.get('county')).toBe('Orange');
    expect(params.get('judge')).toBe('Doe, Jane');
    expect(params.get('dateFrom')).toBe('2026-01-01');
    expect(params.get('dateTo')).toBe('2026-12-31');
    expect(params.get('motion')).toBe('msj');
    expect(params.get('outcome')).toBe('granted');
  });
});

describe('parseSearchParams', () => {
  it('returns empty defaults for empty params', () => {
    const result = parseSearchParams(new URLSearchParams());
    expect(result).toEqual({
      q: '',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: [],
    });
  });

  it('parses q param', () => {
    const result = parseSearchParams(new URLSearchParams('q=test'));
    expect(result.q).toBe('test');
  });

  it('parses county param', () => {
    const result = parseSearchParams(
      new URLSearchParams('county=Los+Angeles'),
    );
    expect(result.county).toBe('Los Angeles');
  });

  it('parses judge param into judgeName', () => {
    const result = parseSearchParams(
      new URLSearchParams('judge=Smith%2C+John'),
    );
    expect(result.judgeName).toBe('Smith, John');
  });

  it('parses date range params', () => {
    const result = parseSearchParams(
      new URLSearchParams('dateFrom=2026-01-01&dateTo=2026-03-31'),
    );
    expect(result.dateFrom).toBe('2026-01-01');
    expect(result.dateTo).toBe('2026-03-31');
  });

  it('parses motion types from comma-separated string', () => {
    const result = parseSearchParams(new URLSearchParams('motion=msj,mtd'));
    expect(result.motionTypes).toEqual(['msj', 'mtd']);
  });

  it('parses outcomes from comma-separated string', () => {
    const result = parseSearchParams(
      new URLSearchParams('outcome=granted,denied'),
    );
    expect(result.outcomes).toEqual(['granted', 'denied']);
  });

  it('handles empty motion/outcome strings gracefully', () => {
    const result = parseSearchParams(new URLSearchParams('motion=&outcome='));
    expect(result.motionTypes).toEqual([]);
    expect(result.outcomes).toEqual([]);
  });

  it('round-trips with buildSearchParams', () => {
    const original = {
      q: 'summary judgment',
      county: 'Los Angeles',
      judgeName: 'Smith, John',
      dateFrom: '2026-01-01',
      dateTo: '2026-12-31',
      motionTypes: ['msj', 'demurrer'],
      outcomes: ['granted', 'denied'],
    };
    const params = buildSearchParams(original);
    const parsed = parseSearchParams(params);
    expect(parsed).toEqual(original);
  });
});

describe('constants', () => {
  it('MOTION_TYPES has expected values', () => {
    expect(MOTION_TYPES).toContain('msj');
    expect(MOTION_TYPES).toContain('mtd');
    expect(MOTION_TYPES).toContain('mil');
    expect(MOTION_TYPES).toContain('demurrer');
    expect(MOTION_TYPES).toContain('anti_slapp');
    expect(MOTION_TYPES).toContain('other');
    expect(MOTION_TYPES.length).toBe(6);
  });

  it('every MOTION_TYPE has a label', () => {
    for (const mt of MOTION_TYPES) {
      expect(MOTION_TYPE_LABELS[mt]).toBeDefined();
      expect(typeof MOTION_TYPE_LABELS[mt]).toBe('string');
    }
  });

  it('OUTCOMES has expected values', () => {
    expect(OUTCOMES).toContain('granted');
    expect(OUTCOMES).toContain('denied');
    expect(OUTCOMES).toContain('granted_in_part');
    expect(OUTCOMES).toContain('moot');
    expect(OUTCOMES).toContain('continued');
    expect(OUTCOMES).toContain('other');
    expect(OUTCOMES.length).toBe(6);
  });

  it('every OUTCOME has a label', () => {
    for (const oc of OUTCOMES) {
      expect(OUTCOME_LABELS[oc]).toBeDefined();
      expect(typeof OUTCOME_LABELS[oc]).toBe('string');
    }
  });
});
