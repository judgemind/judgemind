/**
 * Unit tests for buildQuery — validates OpenSearch query construction
 * for motion type and outcome filters (#1105).
 */

import { describe, it, expect } from 'vitest';
import { buildQuery } from '../src/search/search-rulings';

describe('buildQuery', () => {
  it('returns bool with match_all and future-date filter when no query or filters', () => {
    const result = buildQuery(undefined, undefined) as {
      bool: { must: unknown[]; filter: unknown[] };
    };
    expect(result.bool.must).toEqual([{ match_all: {} }]);
    expect(result.bool.filter).toContainEqual({
      range: { hearing_date: { lte: 'now/d' } },
    });
  });

  it('builds a full-text must clause for query', () => {
    const result = buildQuery('summary judgment', undefined) as {
      bool: { must: unknown[]; filter?: unknown[] };
    };
    expect(result.bool.must).toHaveLength(1);
    expect(result.bool.must[0]).toEqual({
      match: { ruling_text: { query: 'summary judgment', operator: 'and' } },
    });
  });

  it('adds a terms filter for motionTypes', () => {
    const result = buildQuery(undefined, { motionTypes: ['demurrer', 'msj'] }) as {
      bool: { must: unknown[]; filter: unknown[] };
    };
    expect(result.bool.filter).toContainEqual({
      terms: { motion_type: ['demurrer', 'msj'] },
    });
  });

  it('adds a terms filter for outcomes', () => {
    const result = buildQuery(undefined, { outcomes: ['granted', 'denied'] }) as {
      bool: { must: unknown[]; filter: unknown[] };
    };
    expect(result.bool.filter).toContainEqual({
      terms: { outcome: ['granted', 'denied'] },
    });
  });

  it('does not add motionTypes filter when array is empty', () => {
    const result = buildQuery(undefined, { motionTypes: [] }) as Record<string, unknown>;
    // With no effective filters, only the default future-date filter applies
    const bool = result.bool as { filter?: unknown[] };
    if (bool?.filter) {
      for (const f of bool.filter) {
        expect(f).not.toHaveProperty('terms.motion_type');
      }
    }
  });

  it('does not add outcomes filter when array is empty', () => {
    const result = buildQuery(undefined, { outcomes: [] }) as Record<string, unknown>;
    const bool = result.bool as { filter?: unknown[] };
    if (bool?.filter) {
      for (const f of bool.filter) {
        expect(f).not.toHaveProperty('terms.outcome');
      }
    }
  });

  it('combines motionTypes and outcomes with other filters', () => {
    const result = buildQuery('test', {
      county: 'Los Angeles',
      motionTypes: ['msj'],
      outcomes: ['granted'],
    }) as {
      bool: { must: unknown[]; filter: unknown[] };
    };
    expect(result.bool.must).toHaveLength(1);
    expect(result.bool.filter).toContainEqual({ term: { county: 'Los Angeles' } });
    expect(result.bool.filter).toContainEqual({ terms: { motion_type: ['msj'] } });
    expect(result.bool.filter).toContainEqual({ terms: { outcome: ['granted'] } });
  });

  it('adds date range filter for dateFrom and dateTo', () => {
    const result = buildQuery(undefined, {
      dateFrom: '2026-01-01',
      dateTo: '2026-03-01',
    }) as {
      bool: { must: unknown[]; filter: unknown[] };
    };
    expect(result.bool.filter).toContainEqual({
      range: { hearing_date: { gte: '2026-01-01', lte: '2026-03-01' } },
    });
  });

  it('excludes future dates by default when no dateTo', () => {
    const result = buildQuery(undefined, { county: 'Test' }) as {
      bool: { must: unknown[]; filter: unknown[] };
    };
    const rangeFilter = result.bool.filter.find(
      (f: unknown) => typeof f === 'object' && f !== null && 'range' in f,
    ) as { range: { hearing_date: { lte: string } } } | undefined;
    expect(rangeFilter).toBeDefined();
    expect(rangeFilter?.range.hearing_date.lte).toBe('now/d');
  });

  it('includes future dates when includeFuture is true', () => {
    const result = buildQuery(undefined, { county: 'Test' }, true) as {
      bool: { must: unknown[]; filter: unknown[] };
    };
    // Should NOT have a range filter with lte: 'now/d'
    const rangeFilter = result.bool.filter.find(
      (f: unknown) => typeof f === 'object' && f !== null && 'range' in f,
    );
    expect(rangeFilter).toBeUndefined();
  });
});
