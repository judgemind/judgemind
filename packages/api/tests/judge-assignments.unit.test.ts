/**
 * Unit tests for judge assignments — department history derived from rulings.
 */

import { describe, it, expect, vi } from 'vitest';
import { getJudgeAssignments, getJudgeAssignmentsBatch } from '../src/graphql/judge-assignments';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function createMockPool(rows: Array<Record<string, unknown>>) {
  return {
    query: vi.fn().mockResolvedValue({ rows }),
  } as unknown;
}

// ---------------------------------------------------------------------------
// getJudgeAssignments
// ---------------------------------------------------------------------------

describe('getJudgeAssignments', () => {
  it('returns empty array for judge with no department data', async () => {
    const pool = createMockPool([]);
    const result = await getJudgeAssignments(pool as never, 'judge-1', 'Los Angeles');
    expect(result).toEqual([]);
  });

  it('returns single assignment with courthouse mapping', async () => {
    const pool = createMockPool([
      {
        department: '51',
        first_seen: '2024-01-15',
        last_seen: '2026-03-01',
        case_type: 'civil',
      },
    ]);
    const result = await getJudgeAssignments(pool as never, 'judge-1', 'Los Angeles');
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({
      department: '51',
      caseType: 'civil',
      courthouse: 'Stanley Mosk Courthouse',
      firstSeen: '2024-01-15',
      lastSeen: '2026-03-01',
    });
  });

  it('returns multiple assignments ordered by most recent first', async () => {
    const pool = createMockPool([
      {
        department: '19',
        first_seen: '2025-06-01',
        last_seen: '2026-03-01',
        case_type: 'civil',
      },
      {
        department: '51',
        first_seen: '2024-01-15',
        last_seen: '2025-05-31',
        case_type: 'civil',
      },
    ]);
    const result = await getJudgeAssignments(pool as never, 'judge-1', 'Los Angeles');
    expect(result).toHaveLength(2);
    expect(result[0].department).toBe('19');
    expect(result[1].department).toBe('51');
  });

  it('handles null case_type gracefully', async () => {
    const pool = createMockPool([
      {
        department: 'C24',
        first_seen: '2025-01-01',
        last_seen: '2026-03-01',
        case_type: null,
      },
    ]);
    const result = await getJudgeAssignments(pool as never, 'judge-1', 'Orange');
    expect(result[0].caseType).toBeNull();
    expect(result[0].courthouse).toBe('Central Justice Center, Santa Ana');
  });

  it('returns null courthouse when county has no mapping', async () => {
    const pool = createMockPool([
      {
        department: '5',
        first_seen: '2025-01-01',
        last_seen: '2026-03-01',
        case_type: 'civil',
      },
    ]);
    const result = await getJudgeAssignments(pool as never, 'judge-1', 'Fresno');
    expect(result[0].courthouse).toBeNull();
  });

  it('returns null courthouse when county is null', async () => {
    const pool = createMockPool([
      {
        department: '5',
        first_seen: '2025-01-01',
        last_seen: '2026-03-01',
        case_type: 'civil',
      },
    ]);
    const result = await getJudgeAssignments(pool as never, 'judge-1', null);
    expect(result[0].courthouse).toBeNull();
  });

  it('passes correct SQL parameters', async () => {
    const queryFn = vi.fn().mockResolvedValue({ rows: [] });
    const pool = { query: queryFn };
    await getJudgeAssignments(pool as never, 'judge-42', 'Los Angeles');
    expect(queryFn).toHaveBeenCalledTimes(1);
    const [sql, params] = queryFn.mock.calls[0];
    expect(sql).toContain('r.judge_id = $1');
    expect(params).toEqual(['judge-42']);
  });
});

// ---------------------------------------------------------------------------
// getJudgeAssignmentsBatch
// ---------------------------------------------------------------------------

describe('getJudgeAssignmentsBatch', () => {
  it('groups assignments by judge_id', async () => {
    const pool = createMockPool([
      {
        judge_id: 'judge-1',
        department: '19',
        first_seen: '2025-06-01',
        last_seen: '2026-03-01',
        case_type: 'civil',
      },
      {
        judge_id: 'judge-1',
        department: '51',
        first_seen: '2024-01-15',
        last_seen: '2025-05-31',
        case_type: 'civil',
      },
      {
        judge_id: 'judge-2',
        department: 'C24',
        first_seen: '2025-01-01',
        last_seen: '2026-03-01',
        case_type: 'family',
      },
    ]);

    const countyByJudge = new Map([
      ['judge-1', 'Los Angeles'],
      ['judge-2', 'Orange'],
    ]);

    const result = await getJudgeAssignmentsBatch(
      pool as never,
      ['judge-1', 'judge-2', 'judge-3'],
      countyByJudge,
    );

    expect(result).toHaveLength(3);
    expect(result[0]).toHaveLength(2); // judge-1 has 2 assignments
    expect(result[1]).toHaveLength(1); // judge-2 has 1 assignment
    expect(result[2]).toHaveLength(0); // judge-3 has none
  });

  it('applies courthouse mapping per judge county', async () => {
    const pool = createMockPool([
      {
        judge_id: 'judge-1',
        department: '51',
        first_seen: '2025-01-01',
        last_seen: '2026-03-01',
        case_type: 'civil',
      },
      {
        judge_id: 'judge-2',
        department: 'C24',
        first_seen: '2025-01-01',
        last_seen: '2026-03-01',
        case_type: 'civil',
      },
    ]);

    const countyByJudge = new Map([
      ['judge-1', 'Los Angeles'],
      ['judge-2', 'Orange'],
    ]);

    const result = await getJudgeAssignmentsBatch(
      pool as never,
      ['judge-1', 'judge-2'],
      countyByJudge,
    );

    expect(result[0][0].courthouse).toBe('Stanley Mosk Courthouse');
    expect(result[1][0].courthouse).toBe('Central Justice Center, Santa Ana');
  });

  it('returns empty arrays for all judges when no data', async () => {
    const pool = createMockPool([]);
    const countyByJudge = new Map<string, string | null>();
    const result = await getJudgeAssignmentsBatch(
      pool as never,
      ['judge-1', 'judge-2'],
      countyByJudge,
    );
    expect(result).toEqual([[], []]);
  });
});
