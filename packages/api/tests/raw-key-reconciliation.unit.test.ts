/**
 * Unit tests for the raw-key reconciliation dashboard query (issue #5015).
 */

import { describe, it, expect, vi } from 'vitest';
import type { Pool } from 'pg';
import {
  dataQualityResolvers,
  queryRawKeyReconciliation,
  RAW_RECONCILE_LATEST_RUN_SQL,
  RAW_RECONCILE_SCOPES_SQL,
  toReconcileScope,
} from '../src/graphql/data-quality';

describe('toReconcileScope', () => {
  it('maps counts, sorts skip reasons and finds the unrepresented day range', () => {
    const s = toReconcileScope({
      scope: 'ca/los_angeles',
      raw_keys: '36677',
      represented: 31395,
      skipped: '3',
      unrepresented: 5282,
      pending: 0,
      dated_excluded: 4705,
      skipped_by_reason: { validation_fail: 1, governor_bio_route: 2 },
      unrepresented_by_day: { '2026-06-19': 76, '2026-05-21': 169, '2026-06-02': 272 },
    });
    expect(s).toEqual({
      scope: 'ca/los_angeles',
      rawKeys: 36677,
      represented: 31395,
      skipped: 3,
      unrepresented: 5282,
      pending: 0,
      datedExcluded: 4705,
      skippedByReason: [
        { reason: 'governor_bio_route', count: 2 },
        { reason: 'validation_fail', count: 1 },
      ],
      oldestUnrepresentedDay: '2026-05-21',
      newestUnrepresentedDay: '2026-06-19',
    });
  });

  it('accepts JSON text columns and tolerates missing or bad values', () => {
    const s = toReconcileScope({
      scope: 'federal/federal',
      raw_keys: null,
      skipped_by_reason: '{"no_ruling_text": 4}',
      unrepresented_by_day: 'not json',
    });
    expect(s.rawKeys).toBe(0);
    expect(s.skippedByReason).toEqual([{ reason: 'no_ruling_text', count: 4 }]);
    expect(s.oldestUnrepresentedDay).toBeNull();
    expect(s.newestUnrepresentedDay).toBeNull();
    expect(toReconcileScope({ scope: 'x', skipped_by_reason: '[1]' }).skippedByReason).toEqual([
      { reason: '0', count: 1 },
    ]);
    expect(toReconcileScope({ scope: 'x', skipped_by_reason: 'null' }).skippedByReason).toEqual([]);
    expect(toReconcileScope({ scope: 'x', skipped_by_reason: 7 }).skippedByReason).toEqual([]);
  });
});

describe('queryRawKeyReconciliation', () => {
  it('returns null before the first recorded run', async () => {
    const query = vi.fn().mockResolvedValue({ rows: [] });
    expect(await queryRawKeyReconciliation({ query } as unknown as Pool)).toBeNull();
    expect(query).toHaveBeenCalledTimes(1);
    expect(query).toHaveBeenCalledWith(RAW_RECONCILE_LATEST_RUN_SQL);
  });

  it('reads the latest run and its scopes', async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce({
        rows: [
          {
            run_id: 'run-1',
            started_at: '2026-11-15T13:00:00Z',
            finished_at: '2026-11-15 13:01:02+00',
            raw_keys: '60532',
            represented: '43739',
            skipped: '3640',
            unrepresented: '13153',
            pending: '0',
            dated_excluded: '9431',
            action: 'over_threshold',
          },
        ],
      })
      .mockResolvedValueOnce({
        rows: [
          { scope: 'ca/los_angeles', unrepresented: 5282 },
          { scope: 'ca/fresno', unrepresented: 0 },
        ],
      });
    const run = await queryRawKeyReconciliation({ query } as unknown as Pool);
    expect(query).toHaveBeenLastCalledWith(RAW_RECONCILE_SCOPES_SQL, ['run-1']);
    expect(run).toMatchObject({
      runId: 'run-1',
      startedAt: '2026-11-15T13:00:00.000Z',
      finishedAt: '2026-11-15T13:01:02.000Z',
      rawKeys: 60532,
      unrepresented: 13153,
      datedExcluded: 9431,
      action: 'over_threshold',
    });
    expect(run?.scopes.map((s) => s.scope)).toEqual(['ca/los_angeles', 'ca/fresno']);
  });

  it('defaults a missing action to none', async () => {
    const query = vi
      .fn()
      .mockResolvedValueOnce({ rows: [{ run_id: 'r', action: null }] })
      .mockResolvedValueOnce({ rows: [] });
    const run = await queryRawKeyReconciliation({ query } as unknown as Pool);
    expect(run?.action).toBe('none');
  });
});

describe('rawKeyReconciliation resolver', () => {
  const pool = { query: vi.fn().mockResolvedValue({ rows: [] }) } as unknown as Pool;

  it('rejects unauthenticated callers', async () => {
    await expect(
      dataQualityResolvers.Query.rawKeyReconciliation(undefined, undefined, { pool, user: null }),
    ).rejects.toThrow('Not authenticated');
  });

  it('rejects non-admin callers', async () => {
    await expect(
      dataQualityResolvers.Query.rawKeyReconciliation(undefined, undefined, {
        pool,
        user: { id: 'u1', email: 'u@example.com', role: 'user' },
      }),
    ).rejects.toThrow('Admin access required');
  });

  it('returns null for admins before any run', async () => {
    const result = await dataQualityResolvers.Query.rawKeyReconciliation(undefined, undefined, {
      pool,
      user: { id: 'a1', email: 'a@example.com', role: 'admin' },
    });
    expect(result).toBeNull();
  });
});
