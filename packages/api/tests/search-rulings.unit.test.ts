/**
 * Unit tests for buildQuery — validates OpenSearch query construction
 * for motion type and outcome filters (#1105).
 */

import { describe, it, expect, vi } from 'vitest';
import type { Client } from '@opensearch-project/opensearch';
import type { Pool } from 'pg';
import { buildQuery, normalizeHearingDate, searchRulings } from '../src/search/search-rulings';

describe('normalizeHearingDate (#4712)', () => {
  it.each([
    ['2026-07-28', '2026-07-28'],
    ['2026-07-28T00:00:00', '2026-07-28'],
    ['2026-07-28T00:00:00+00:00', '2026-07-28'],
    ['2026-07-28 09:30:00', '2026-07-28'],
  ])('reduces %s to a calendar date', (input, expected) => {
    expect(normalizeHearingDate(input)).toBe(expected);
  });

  it('accepts a Date', () => {
    expect(normalizeHearingDate(new Date(2026, 6, 28))).toBe('2026-07-28');
    expect(normalizeHearingDate(new Date('nope'))).toBeNull();
  });

  it.each([[null], [undefined], [''], ['not-a-date'], [42]])('returns null for %s', (input) => {
    expect(normalizeHearingDate(input)).toBeNull();
  });
});

describe('searchRulings hit shaping (#4712)', () => {
  function makeOs(source: Record<string, unknown>): Client {
    return {
      search: vi.fn().mockResolvedValue({
        body: {
          hits: {
            total: { value: 1 },
            hits: [{ _id: source.document_id, _score: 1, _source: source, sort: [1, 'x'] }],
          },
        },
      }),
    } as unknown as Client;
  }

  function makePool(rows: Array<Record<string, unknown>>): Pool {
    return { query: vi.fn().mockResolvedValue({ rows }) } as unknown as Pool;
  }

  const staleSource = {
    document_id: 'aad4f50a-26ad-5ee0-984d-0259fda7d54a',
    case_number: '21STCV42883',
    case_title: 'Berenice Murillo v. United Parcel Service, Inc',
    hearing_date: '2026-07-28T00:00:00',
  };

  it('returns a date-only hearingDate when the index holds a datetime', async () => {
    const result = await searchRulings(makeOs(staleSource), makePool([]), { query: 'demurrer' });
    expect(result.edges[0].node.hearingDate).toBe('2026-07-28');
  });

  it('prefers the Postgres case title, case number and hearing date over the search doc', async () => {
    const pool = makePool([
      {
        id: '599b21b0-42b2-4434-9c35-4e4dd74ce0a9',
        document_id: staleSource.document_id,
        hearing_date: '2026-07-29',
        case_title: 'Murillo v. BNSF Railway Company',
        case_number: '21STCV42883',
      },
    ]);
    const result = await searchRulings(makeOs(staleSource), pool, { query: 'demurrer' });
    const node = result.edges[0].node;
    expect(node.rulingId).toBe('599b21b0-42b2-4434-9c35-4e4dd74ce0a9');
    expect(node.caseTitle).toBe('Murillo v. BNSF Railway Company');
    expect(node.caseNumber).toBe('21STCV42883');
    expect(node.hearingDate).toBe('2026-07-29');
  });
});

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

  it('adds a terms filter for caseTypes', () => {
    const result = buildQuery(undefined, { caseTypes: ['civil', 'family'] }) as {
      bool: { must: unknown[]; filter: unknown[] };
    };
    expect(result.bool.filter).toContainEqual({
      terms: { case_type: ['civil', 'family'] },
    });
  });

  it('does not add caseTypes filter when array is empty', () => {
    const result = buildQuery(undefined, { caseTypes: [] }) as Record<string, unknown>;
    const bool = result.bool as { filter?: unknown[] };
    if (bool?.filter) {
      for (const f of bool.filter) {
        expect(f).not.toHaveProperty('terms.case_type');
      }
    }
  });

  it('combines motionTypes, outcomes, and caseTypes with other filters', () => {
    const result = buildQuery('test', {
      county: 'Los Angeles',
      motionTypes: ['msj'],
      outcomes: ['granted'],
      caseTypes: ['civil'],
    }) as {
      bool: { must: unknown[]; filter: unknown[] };
    };
    expect(result.bool.must).toHaveLength(1);
    expect(result.bool.filter).toContainEqual({ term: { county: 'Los Angeles' } });
    expect(result.bool.filter).toContainEqual({ terms: { motion_type: ['msj'] } });
    expect(result.bool.filter).toContainEqual({ terms: { outcome: ['granted'] } });
    expect(result.bool.filter).toContainEqual({ terms: { case_type: ['civil'] } });
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
