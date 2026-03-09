/**
 * Unit tests for DataLoader batching logic. Uses a mock pg Pool to verify
 * that loaders issue the correct SQL and group results properly.
 */

import { describe, it, expect, vi } from 'vitest';
import { createLoaders } from '../src/graphql/dataloader';
import type { Pool, QueryResult } from 'pg';

// ---------------------------------------------------------------------------
// Mock pool
// ---------------------------------------------------------------------------

function mockPool(queryFn: (text: string, params: unknown[]) => QueryResult): Pool {
  return { query: vi.fn(queryFn) } as unknown as Pool;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('createLoaders', () => {
  describe('caseLoader', () => {
    it('batches multiple case lookups into one query', async () => {
      const pool = mockPool(() => ({
        rows: [
          { id: 'case-1', case_number: 'A' },
          { id: 'case-2', case_number: 'B' },
        ],
        command: 'SELECT',
        rowCount: 2,
        oid: 0,
        fields: [],
      }));

      const loaders = createLoaders(pool);

      // Request two cases in the same tick — should batch
      const [c1, c2] = await Promise.all([
        loaders.caseLoader.load('case-1'),
        loaders.caseLoader.load('case-2'),
      ]);

      expect(c1).toMatchObject({ id: 'case-1', case_number: 'A' });
      expect(c2).toMatchObject({ id: 'case-2', case_number: 'B' });
      expect(pool.query).toHaveBeenCalledTimes(1);
    });

    it('returns null for missing case IDs', async () => {
      const pool = mockPool(() => ({
        rows: [{ id: 'case-1', case_number: 'A' }],
        command: 'SELECT',
        rowCount: 1,
        oid: 0,
        fields: [],
      }));

      const loaders = createLoaders(pool);
      const [c1, c2] = await Promise.all([
        loaders.caseLoader.load('case-1'),
        loaders.caseLoader.load('case-missing'),
      ]);

      expect(c1).toMatchObject({ id: 'case-1' });
      expect(c2).toBeNull();
    });
  });

  describe('documentFormatLoader', () => {
    it('returns format strings for document IDs', async () => {
      const pool = mockPool(() => ({
        rows: [
          { id: 'doc-1', format: 'html' },
          { id: 'doc-2', format: 'pdf' },
        ],
        command: 'SELECT',
        rowCount: 2,
        oid: 0,
        fields: [],
      }));

      const loaders = createLoaders(pool);
      const [f1, f2] = await Promise.all([
        loaders.documentFormatLoader.load('doc-1'),
        loaders.documentFormatLoader.load('doc-2'),
      ]);

      expect(f1).toBe('html');
      expect(f2).toBe('pdf');
      expect(pool.query).toHaveBeenCalledTimes(1);
    });

    it('returns null for missing document IDs', async () => {
      const pool = mockPool(() => ({
        rows: [],
        command: 'SELECT',
        rowCount: 0,
        oid: 0,
        fields: [],
      }));

      const loaders = createLoaders(pool);
      const result = await loaders.documentFormatLoader.load('doc-missing');
      expect(result).toBeNull();
    });
  });

  describe('caseJudgesLoader', () => {
    it('groups judges by case_id from case_judges table', async () => {
      const pool = mockPool((text) => {
        if (text.includes('case_judges')) {
          return {
            rows: [
              { id: 'judge-1', canonical_name: 'Smith', case_id: 'case-1' },
              { id: 'judge-2', canonical_name: 'Jones', case_id: 'case-1' },
              { id: 'judge-3', canonical_name: 'Brown', case_id: 'case-2' },
            ],
            command: 'SELECT',
            rowCount: 3,
            oid: 0,
            fields: [],
          };
        }
        // Fallback query should not be called when case_judges has results
        return { rows: [], command: 'SELECT', rowCount: 0, oid: 0, fields: [] };
      });

      const loaders = createLoaders(pool);
      const [j1, j2] = await Promise.all([
        loaders.caseJudgesLoader.load('case-1'),
        loaders.caseJudgesLoader.load('case-2'),
      ]);

      expect(j1).toHaveLength(2);
      expect(j1[0]).toMatchObject({ id: 'judge-1', canonical_name: 'Smith' });
      expect(j1[0]).not.toHaveProperty('case_id'); // should be stripped
      expect(j2).toHaveLength(1);
      expect(j2[0]).toMatchObject({ id: 'judge-3', canonical_name: 'Brown' });
      // Only one query needed (case_judges had results for all cases)
      expect(pool.query).toHaveBeenCalledTimes(1);
    });

    it('falls back to rulings for cases without case_judges entries', async () => {
      let callCount = 0;
      const pool = mockPool((text) => {
        callCount++;
        if (text.includes('case_judges')) {
          // case-1 has case_judges, case-2 does not
          return {
            rows: [
              { id: 'judge-1', canonical_name: 'Smith', case_id: 'case-1' },
            ],
            command: 'SELECT',
            rowCount: 1,
            oid: 0,
            fields: [],
          };
        }
        // Fallback via rulings for case-2
        return {
          rows: [
            { id: 'judge-3', canonical_name: 'Brown', case_id: 'case-2' },
          ],
          command: 'SELECT',
          rowCount: 1,
          oid: 0,
          fields: [],
        };
      });

      const loaders = createLoaders(pool);
      const [j1, j2] = await Promise.all([
        loaders.caseJudgesLoader.load('case-1'),
        loaders.caseJudgesLoader.load('case-2'),
      ]);

      expect(j1).toHaveLength(1);
      expect(j1[0]).toMatchObject({ id: 'judge-1' });
      expect(j2).toHaveLength(1);
      expect(j2[0]).toMatchObject({ id: 'judge-3' });
      // Two queries: case_judges + fallback rulings
      expect(callCount).toBe(2);
    });

    it('returns empty array for cases with no judges at all', async () => {
      const pool = mockPool(() => ({
        rows: [],
        command: 'SELECT',
        rowCount: 0,
        oid: 0,
        fields: [],
      }));

      const loaders = createLoaders(pool);
      const result = await loaders.caseJudgesLoader.load('case-no-judges');
      expect(result).toEqual([]);
    });
  });

  describe('casePartiesLoader', () => {
    it('groups parties by case_id with roles', async () => {
      const pool = mockPool(() => ({
        rows: [
          { id: 'party-1', canonical_name: 'Smith', party_type: 'individual', role: 'plaintiff', case_id: 'case-1' },
          { id: 'party-2', canonical_name: 'Corp Inc', party_type: 'corporation', role: 'defendant', case_id: 'case-1' },
          { id: 'party-3', canonical_name: 'Jones', party_type: 'individual', role: 'plaintiff', case_id: 'case-2' },
        ],
        command: 'SELECT',
        rowCount: 3,
        oid: 0,
        fields: [],
      }));

      const loaders = createLoaders(pool);
      const [p1, p2] = await Promise.all([
        loaders.casePartiesLoader.load('case-1'),
        loaders.casePartiesLoader.load('case-2'),
      ]);

      expect(p1).toHaveLength(2);
      expect(p1[0]).toMatchObject({ canonical_name: 'Smith', role: 'plaintiff' });
      expect(p1[0]).not.toHaveProperty('case_id'); // stripped
      expect(p2).toHaveLength(1);
      expect(p2[0]).toMatchObject({ canonical_name: 'Jones' });
      expect(pool.query).toHaveBeenCalledTimes(1);
    });

    it('returns empty array for cases with no parties', async () => {
      const pool = mockPool(() => ({
        rows: [],
        command: 'SELECT',
        rowCount: 0,
        oid: 0,
        fields: [],
      }));

      const loaders = createLoaders(pool);
      const result = await loaders.casePartiesLoader.load('case-no-parties');
      expect(result).toEqual([]);
    });
  });

  describe('courtLoader and judgeLoader (existing)', () => {
    it('courtLoader batches and returns null for missing', async () => {
      const pool = mockPool(() => ({
        rows: [{ id: 'court-1', court_name: 'Test Court' }],
        command: 'SELECT',
        rowCount: 1,
        oid: 0,
        fields: [],
      }));

      const loaders = createLoaders(pool);
      const [c1, c2] = await Promise.all([
        loaders.courtLoader.load('court-1'),
        loaders.courtLoader.load('court-missing'),
      ]);

      expect(c1).toMatchObject({ id: 'court-1' });
      expect(c2).toBeNull();
      expect(pool.query).toHaveBeenCalledTimes(1);
    });
  });
});
