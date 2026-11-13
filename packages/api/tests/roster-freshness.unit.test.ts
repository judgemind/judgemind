/**
 * Unit tests for per-court roster freshness (issue #4872).
 */

import { describe, it, expect, vi } from 'vitest';
import type { Pool } from 'pg';
import {
  computeRosterFreshness,
  dataQualityResolvers,
  queryRosterFreshness,
  ROSTER_FRESHNESS_SQL,
  ROSTER_STALE_THRESHOLD_HOURS,
} from '../src/graphql/data-quality';

const NOW = new Date('2026-11-13T12:00:00Z');

function hoursAgo(h: number): string {
  return new Date(NOW.getTime() - h * 3_600_000).toISOString();
}

describe('computeRosterFreshness', () => {
  it('is ok when the latest attempt succeeded recently', () => {
    const r = computeRosterFreshness(
      {
        court_id: 'ca_orange',
        last_attempt_at: hoursAgo(2),
        last_outcome: 'success',
        last_department_count: 120,
        last_success_attempt_at: hoursAgo(2),
        last_snapshot_at: hoursAgo(300),
        consecutive_failures: '0',
      },
      NOW,
    );
    expect(r.status).toBe('ok');
    expect(r.isStale).toBe(false);
    // A deduplicated success (old snapshot) still counts as fresh.
    expect(r.lastSuccessAt).toBe(hoursAgo(2));
    expect(r.lastSnapshotAt).toBe(hoursAgo(300));
    expect(r.ageHours).toBe(2);
    expect(r.lastDepartmentCount).toBe(120);
    expect(r.consecutiveFailures).toBe(0);
    expect(r.staleThresholdHours).toBe(ROSTER_STALE_THRESHOLD_HOURS);
  });

  it('is failing when the latest attempt failed but the last success is recent', () => {
    const r = computeRosterFreshness(
      {
        court_id: 'ca_kern',
        last_attempt_at: hoursAgo(1),
        last_outcome: 'failure',
        last_error_class: 'network',
        last_success_attempt_at: hoursAgo(13),
        last_snapshot_at: null,
        consecutive_failures: 1,
      },
      NOW,
    );
    expect(r.status).toBe('failing');
    expect(r.isStale).toBe(false);
    expect(r.lastErrorClass).toBe('network');
  });

  it('is stale when the last success is older than 7 days', () => {
    const r = computeRosterFreshness(
      {
        court_id: 'ca_los_angeles',
        last_attempt_at: hoursAgo(1),
        last_outcome: 'failure',
        last_error_class: 'http_5xx',
        last_http_status: 503,
        last_error_message: "HTTPStatusError: Server error '503 Service Unavailable'",
        last_success_attempt_at: null,
        last_snapshot_at: '2026-07-01 13:30:08.583195+00',
        consecutive_failures: '12',
      },
      NOW,
    );
    expect(r.status).toBe('stale');
    expect(r.isStale).toBe(true);
    expect(r.lastSuccessAt).toBe('2026-07-01T13:30:08.583Z');
    expect(r.lastHttpStatus).toBe(503);
    expect(r.consecutiveFailures).toBe(12);
    expect(r.ageHours).toBeGreaterThan(ROSTER_STALE_THRESHOLD_HOURS);
  });

  it('is stale when no success has ever been recorded', () => {
    const r = computeRosterFreshness(
      {
        court_id: 'ca_santa_clara',
        last_attempt_at: hoursAgo(1),
        last_outcome: 'failure',
        last_error_class: 'missing_court_id',
        last_success_attempt_at: null,
        last_snapshot_at: null,
        consecutive_failures: 3,
      },
      NOW,
    );
    expect(r.status).toBe('stale');
    expect(r.lastSuccessAt).toBeNull();
    expect(r.ageHours).toBeNull();
  });

  it('uses the snapshot time when it is newer than the last success attempt', () => {
    const r = computeRosterFreshness(
      {
        court_id: 'ca_contra_costa',
        last_success_attempt_at: hoursAgo(400),
        last_snapshot_at: hoursAgo(10),
      },
      NOW,
    );
    expect(r.lastSuccessAt).toBe(hoursAgo(10));
    expect(r.status).toBe('ok');
    expect(r.lastAttemptAt).toBeNull();
    expect(r.lastOutcome).toBeNull();
    expect(r.consecutiveFailures).toBe(0);
  });

  it('is ok when a snapshot newer than the last failed attempt exists', () => {
    const r = computeRosterFreshness(
      {
        court_id: 'ca_san_francisco',
        last_attempt_at: hoursAgo(20),
        last_outcome: 'failure',
        last_error_class: 'empty_roster',
        last_success_attempt_at: null,
        last_snapshot_at: hoursAgo(5),
        consecutive_failures: 0,
      },
      NOW,
    );
    expect(r.status).toBe('ok');
    expect(r.lastSuccessAt).toBe(hoursAgo(5));
  });

  it('treats exactly the threshold age as fresh and one hour past as stale', () => {
    const fresh = computeRosterFreshness(
      { court_id: 'x', last_success_attempt_at: hoursAgo(168) },
      NOW,
    );
    const stale = computeRosterFreshness(
      { court_id: 'x', last_success_attempt_at: hoursAgo(169) },
      NOW,
    );
    expect(fresh.isStale).toBe(false);
    expect(stale.isStale).toBe(true);
  });

  it('honours a custom threshold', () => {
    const r = computeRosterFreshness(
      { court_id: 'x', last_success_attempt_at: hoursAgo(30) },
      NOW,
      24,
    );
    expect(r.isStale).toBe(true);
    expect(r.staleThresholdHours).toBe(24);
  });

  it('ignores unparseable timestamps', () => {
    const r = computeRosterFreshness(
      { court_id: 'x', last_success_attempt_at: 'not-a-date', last_http_status: 'abc' },
      NOW,
    );
    expect(r.lastSuccessAt).toBeNull();
    expect(r.lastHttpStatus).toBeNull();
    expect(r.isStale).toBe(true);
  });
});

describe('queryRosterFreshness', () => {
  it('maps every row returned by the freshness query', async () => {
    const query = vi.fn().mockResolvedValue({
      rows: [
        { court_id: 'ca_fresno', last_success_attempt_at: hoursAgo(1), last_outcome: 'success' },
        { court_id: 'ca_los_angeles', last_snapshot_at: hoursAgo(3000), last_outcome: 'failure' },
      ],
    });
    const pool = { query } as unknown as Pool;
    const result = await queryRosterFreshness(pool, NOW);
    expect(query).toHaveBeenCalledWith(ROSTER_FRESHNESS_SQL);
    expect(result.map((r) => [r.courtId, r.status])).toEqual([
      ['ca_fresno', 'ok'],
      ['ca_los_angeles', 'stale'],
    ]);
  });
});

describe('rosterFreshness resolver', () => {
  const pool = { query: vi.fn().mockResolvedValue({ rows: [] }) } as unknown as Pool;

  it('rejects unauthenticated callers', async () => {
    await expect(
      dataQualityResolvers.Query.rosterFreshness(undefined, undefined, { pool, user: null }),
    ).rejects.toThrow('Not authenticated');
  });

  it('rejects non-admin callers', async () => {
    await expect(
      dataQualityResolvers.Query.rosterFreshness(undefined, undefined, {
        pool,
        user: { id: 'u1', email: 'u@example.com', role: 'user' },
      }),
    ).rejects.toThrow('Admin access required');
  });

  it('returns rows for admins', async () => {
    const result = await dataQualityResolvers.Query.rosterFreshness(undefined, undefined, {
      pool,
      user: { id: 'a1', email: 'a@example.com', role: 'admin' },
    });
    expect(result).toEqual([]);
  });
});
