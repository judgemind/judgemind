/**
 * Unit tests for the distinctCounties and distinctJudgeNames resolvers.
 * Mocks the pg Pool — no database needed.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { resolvers } from '../src/graphql/resolvers';

// ---------------------------------------------------------------------------
// Mock context
// ---------------------------------------------------------------------------

function createMockPool(queryResult: { rows: Record<string, unknown>[] }) {
  return {
    query: vi.fn().mockResolvedValue(queryResult),
  };
}

function createContext(pool: ReturnType<typeof createMockPool>) {
  return {
    pool,
    loaders: {} as never,
    user: null,
    ip: '127.0.0.1',
    reply: {} as never,
    cookieHeader: '',
    opensearch: {} as never,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('distinctCounties resolver', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns sorted county names from active courts', async () => {
    const pool = createMockPool({
      rows: [
        { county: 'Fresno' },
        { county: 'Los Angeles' },
        { county: 'Orange' },
      ],
    });
    const ctx = createContext(pool);

    const result = await resolvers.Query.distinctCounties(null, {}, ctx);

    expect(result).toEqual(['Fresno', 'Los Angeles', 'Orange']);
    expect(pool.query).toHaveBeenCalledWith(
      expect.stringContaining('SELECT DISTINCT county FROM courts'),
    );
    expect(pool.query).toHaveBeenCalledWith(
      expect.stringContaining('is_active = true'),
    );
  });

  it('returns an empty array when no courts exist', async () => {
    const pool = createMockPool({ rows: [] });
    const ctx = createContext(pool);

    const result = await resolvers.Query.distinctCounties(null, {}, ctx);

    expect(result).toEqual([]);
  });
});

describe('distinctJudgeNames resolver', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns sorted judge names from active judges', async () => {
    const pool = createMockPool({
      rows: [
        { canonical_name: 'Bradley, Elizabeth L.' },
        { canonical_name: 'Brazile, Kevin C.' },
        { canonical_name: 'Crowfoot, William A.' },
      ],
    });
    const ctx = createContext(pool);

    const result = await resolvers.Query.distinctJudgeNames(null, {}, ctx);

    expect(result).toEqual([
      'Bradley, Elizabeth L.',
      'Brazile, Kevin C.',
      'Crowfoot, William A.',
    ]);
    expect(pool.query).toHaveBeenCalledWith(
      expect.stringContaining('SELECT DISTINCT canonical_name FROM judges'),
    );
    expect(pool.query).toHaveBeenCalledWith(
      expect.stringContaining('is_active = true'),
    );
  });

  it('returns an empty array when no judges exist', async () => {
    const pool = createMockPool({ rows: [] });
    const ctx = createContext(pool);

    const result = await resolvers.Query.distinctJudgeNames(null, {}, ctx);

    expect(result).toEqual([]);
  });
});
