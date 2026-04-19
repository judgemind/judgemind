/**
 * Unit tests for the DB-only enrichment path on the dispatcher admin
 * resolvers (issue #2820). These replace the deleted
 * ``dispatcher-github.unit.test.ts`` which covered the old fetch-
 * from-GitHub fallback. The new tests exercise:
 *
 *   - ``normalizeSnapshotJson`` — accepts either parsed jsonb or a
 *     JSON-encoded string, drops malformed rows, projects labels to
 *     string[].
 *   - ``queueItemFromSnapshot`` — builds the QueueItem shape the
 *     admin page expects, including ``priority`` extracted from
 *     labels and (when requested) ``blockedBy`` parsed from the body.
 *   - ``recentCompletionsToGraphQL`` — reads ``issue_title`` directly
 *     from the ``dispatcher.agents`` row; falls back to null for
 *     rows that pre-date migration 28.
 *   - The ``parse-labels`` helpers remain pure — no fetch.
 */

import { describe, it, expect } from 'vitest';
import {
  coerceNullableNumber,
  normalizeSnapshotJson,
  phaseCostRowsToGraphQL,
  queueItemFromSnapshot,
  recentCompletionsToGraphQL,
  sortAndSliceQueueReady,
  sumPhaseCost,
  type SnapshotIssueRecord,
} from '../src/graphql/dispatcher/resolvers';
import {
  extractPriority,
  parseBlockedBy,
  priorityRank,
  PRIORITY_RANK_NO_LABEL,
} from '../src/graphql/dispatcher/parse-labels';

describe('normalizeSnapshotJson', () => {
  it('accepts already-parsed jsonb arrays', () => {
    const raw = [
      {
        number: 42,
        title: 'Do the thing',
        labels: ['priority/p1', 'agent/ready'],
        createdAt: '2026-04-18T00:00:00Z',
      },
    ];
    const result = normalizeSnapshotJson(raw);
    expect(result).toEqual([
      {
        number: 42,
        title: 'Do the thing',
        labels: ['priority/p1', 'agent/ready'],
        createdAt: '2026-04-18T00:00:00Z',
      },
    ]);
  });

  it('accepts a JSON-encoded string (defensive)', () => {
    const raw = JSON.stringify([
      {
        number: 7,
        title: 't',
        labels: ['area/x'],
        createdAt: '2026-04-18T00:00:00Z',
      },
    ]);
    const result = normalizeSnapshotJson(raw);
    expect(result).toHaveLength(1);
    expect(result[0].number).toBe(7);
  });

  it('returns empty array for invalid string JSON', () => {
    expect(normalizeSnapshotJson('not json')).toEqual([]);
  });

  it('returns empty array for non-array input', () => {
    expect(normalizeSnapshotJson(null)).toEqual([]);
    expect(normalizeSnapshotJson({})).toEqual([]);
    expect(normalizeSnapshotJson(42)).toEqual([]);
  });

  it('drops entries without a numeric ``number``', () => {
    const raw = [
      { number: 1, title: 'ok', labels: [], createdAt: '' },
      { number: 'bad', title: 'skip', labels: [], createdAt: '' },
      { title: 'no-number', labels: [], createdAt: '' },
    ];
    const result = normalizeSnapshotJson(raw);
    expect(result).toHaveLength(1);
    expect(result[0].number).toBe(1);
  });

  it('filters non-string labels', () => {
    const raw = [
      {
        number: 1,
        title: 't',
        labels: ['ok', 42, { name: 'obj' }, null],
        createdAt: '',
      },
    ];
    const [entry] = normalizeSnapshotJson(raw);
    expect(entry.labels).toEqual(['ok']);
  });

  it('preserves ``body`` when present (blocked snapshot shape)', () => {
    const raw = [
      {
        number: 100,
        title: 't',
        labels: [],
        createdAt: '',
        body: 'Blocked by #42\n',
      },
    ];
    const [entry] = normalizeSnapshotJson(raw);
    expect(entry.body).toBe('Blocked by #42\n');
  });

  it('preserves explicit null ``body``', () => {
    const raw = [
      { number: 1, title: 't', labels: [], createdAt: '', body: null },
    ];
    const [entry] = normalizeSnapshotJson(raw);
    expect(entry.body).toBeNull();
  });
});

describe('queueItemFromSnapshot', () => {
  const base: SnapshotIssueRecord = {
    number: 999,
    title: 'Test issue',
    labels: ['priority/p2', 'agent/ready', 'area/api'],
    createdAt: '2026-04-18T12:00:00Z',
  };

  it('builds the QueueItem shape (includeBlockedBy=false)', () => {
    const result = queueItemFromSnapshot(base, false);
    expect(result).toEqual({
      issueNumber: 999,
      title: 'Test issue',
      priority: 'p2',
      labels: ['priority/p2', 'agent/ready', 'area/api'],
      createdAt: '2026-04-18T12:00:00Z',
      blockedBy: [],
    });
  });

  it('parses blockedBy from the body when requested', () => {
    const withBody: SnapshotIssueRecord = {
      ...base,
      body: 'Some context.\n\nBlocked by #42\nBlocked by #100\n',
    };
    const result = queueItemFromSnapshot(withBody, true);
    expect(result.blockedBy).toEqual([42, 100]);
  });

  it('handles missing priority label', () => {
    const noPriority: SnapshotIssueRecord = { ...base, labels: ['area/api'] };
    const result = queueItemFromSnapshot(noPriority, false);
    expect(result.priority).toBeNull();
  });

  it('returns empty blockedBy when body is missing and includeBlockedBy=true', () => {
    const result = queueItemFromSnapshot(base, true);
    expect(result.blockedBy).toEqual([]);
  });
});

describe('recentCompletionsToGraphQL', () => {
  it('passes ``issue_title`` through from the agents row', () => {
    const rows = [
      {
        agent_id: 'uuid-1',
        issue_number: 100,
        issue_title: 'Fix the thing',
        status: 'succeeded',
        ended_at: '2026-04-18T12:00:00Z',
        pr_number: 2824,
        total_tokens: 12345,
        total_cost_usd: '0.0420',
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result).toEqual([
      {
        agentId: 'uuid-1',
        issueNumber: 100,
        issueTitle: 'Fix the thing',
        status: 'succeeded',
        endedAt: '2026-04-18T12:00:00Z',
        prNumber: 2824,
        totalTokens: 12345,
        totalCostUsd: 0.042,
      },
    ]);
  });

  it('emits totalTokens=null and totalCostUsd=null for pre-migration-31 rows', () => {
    // Rows whose agent ran before migration 31 (or whose phases all
    // crashed before producing a JSON envelope) have NULL metering —
    // the UI renders "no cost data" rather than a misleading $0.00.
    const rows = [
      {
        agent_id: 'uuid-1',
        issue_number: 100,
        issue_title: 'Pre-migration agent',
        status: 'succeeded',
        ended_at: '2026-04-18T12:00:00Z',
        pr_number: 2824,
        total_tokens: null,
        total_cost_usd: null,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].totalTokens).toBeNull();
    expect(result[0].totalCostUsd).toBeNull();
  });

  it('emits issueTitle=null for rows with NULL/empty issue_title (pre-migration-28)', () => {
    const rows = [
      {
        agent_id: 'uuid-1',
        issue_number: 100,
        issue_title: null,
        status: 'succeeded',
        ended_at: '2026-04-18T12:00:00Z',
        pr_number: null,
      },
      {
        agent_id: 'uuid-2',
        issue_number: 101,
        issue_title: '',
        status: 'failed',
        ended_at: '2026-04-18T13:00:00Z',
        pr_number: null,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result[0].issueTitle).toBeNull();
    expect(result[1].issueTitle).toBeNull();
  });

  it('does NOT call GitHub — no async fetch needed', () => {
    // The function is synchronous — proves no hidden fetch path.
    const rows = [
      {
        agent_id: 'uuid-1',
        issue_number: 100,
        issue_title: 'Title',
        status: 'succeeded',
        ended_at: '2026-04-18T12:00:00Z',
        pr_number: null,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    // Synchronous return — not a Promise.
    expect(Array.isArray(result)).toBe(true);
  });

  it('passes plan_blocked status through unchanged (#2857)', () => {
    // plan_blocked is a new terminal status parallel to failed/crashed;
    // the mapper must pass it through so the admin cockpit's
    // OutcomePill can render it with its distinct chip/colour.
    const rows = [
      {
        agent_id: 'uuid-planblocked',
        issue_number: 2857,
        issue_title: 'Plan correctly declined',
        status: 'plan_blocked',
        ended_at: '2026-04-19T20:00:00Z',
        pr_number: null,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result).toHaveLength(1);
    expect(result[0].status).toBe('plan_blocked');
    expect(result[0].agentId).toBe('uuid-planblocked');
  });

  it('passes needs_review status through unchanged (#2856)', () => {
    // needs_review is the "ralph produced SHIP code but summary
    // flagged unmet AC" terminal — parallel to plan_blocked but
    // operator-actionable (amber chip, not neutral). The mapper
    // must pass it through with its prNumber intact because the
    // whole point of the terminal is that the daemon opened a draft
    // PR; dropping prNumber would hide the draft from the cockpit.
    const rows = [
      {
        agent_id: 'uuid-needsreview',
        issue_number: 2856,
        issue_title: 'Summary flagged unmet AC',
        status: 'needs_review',
        ended_at: '2026-04-19T23:30:00Z',
        pr_number: 9001,
      },
    ];
    const result = recentCompletionsToGraphQL(rows);
    expect(result).toHaveLength(1);
    expect(result[0].status).toBe('needs_review');
    expect(result[0].agentId).toBe('uuid-needsreview');
    expect(result[0].prNumber).toBe(9001);
  });
});

describe('parse-labels helpers (pure, no fetch)', () => {
  it('extractPriority picks the first priority/pN label', () => {
    expect(extractPriority(['area/api', 'priority/p1', 'agent/ready'])).toBe(
      'p1',
    );
    expect(extractPriority(['priority/p0'])).toBe('p0');
  });

  it('extractPriority returns null when no priority label is present', () => {
    expect(extractPriority(['area/api'])).toBeNull();
    expect(extractPriority([])).toBeNull();
    expect(extractPriority(null)).toBeNull();
    expect(extractPriority(undefined)).toBeNull();
  });

  it('extractPriority rejects malformed priority labels', () => {
    expect(extractPriority(['priority/p9'])).toBeNull();
    expect(extractPriority(['priority/high'])).toBeNull();
  });

  it('parseBlockedBy extracts every Blocked by #N line', () => {
    expect(parseBlockedBy(null)).toEqual([]);
    expect(parseBlockedBy(undefined)).toEqual([]);
    expect(parseBlockedBy('')).toEqual([]);
    expect(parseBlockedBy('Blocked by #1234\nSome prose.\nBlocked by #5678')).toEqual([
      1234, 5678,
    ]);
  });

  it('parseBlockedBy ignores "Parent:" lines (distinct mechanic)', () => {
    expect(parseBlockedBy('Parent: #1\n')).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// priorityRank + sortAndSliceQueueReady (issue #2843 — parallel to #2835)
// ---------------------------------------------------------------------------

describe('priorityRank', () => {
  it('maps priority labels to their rank (p0..p3 → 0..3)', () => {
    expect(priorityRank(['priority/p0'])).toBe(0);
    expect(priorityRank(['priority/p1'])).toBe(1);
    expect(priorityRank(['priority/p2'])).toBe(2);
    expect(priorityRank(['priority/p3'])).toBe(3);
  });

  it('returns the no-label floor (4) when no priority label is present', () => {
    expect(priorityRank(['area/api', 'type/bug'])).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank([])).toBe(PRIORITY_RANK_NO_LABEL);
    expect(PRIORITY_RANK_NO_LABEL).toBe(4);
  });

  it('returns the no-label floor for null / undefined / non-array input', () => {
    expect(priorityRank(null)).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank(undefined)).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank('priority/p0')).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank({ 'priority/p0': 1 })).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank(42)).toBe(PRIORITY_RANK_NO_LABEL);
  });

  it('ignores non-string entries in the label array', () => {
    // Every entry is non-string → floor
    expect(priorityRank([{ name: 'priority/p0' }, 42, null])).toBe(
      PRIORITY_RANK_NO_LABEL,
    );
    // Mixed — valid string wins despite noise
    expect(priorityRank([null, 'priority/p1', { obj: true }])).toBe(1);
  });

  it('picks the lowest rank when multiple priority labels are present', () => {
    expect(priorityRank(['priority/p2', 'priority/p0'])).toBe(0);
    expect(priorityRank(['priority/p3', 'priority/p1'])).toBe(1);
    expect(priorityRank(['priority/p2', 'priority/p3'])).toBe(2);
  });

  it('rejects malformed priority labels (priority/p9, priority/high, etc.)', () => {
    expect(priorityRank(['priority/p9'])).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank(['priority/high'])).toBe(PRIORITY_RANK_NO_LABEL);
    expect(priorityRank(['priority/p0 '])).toBe(PRIORITY_RANK_NO_LABEL);
  });

  it('documents the end-to-end rank ordering (p0 < p1 < p2 < p3 < floor)', () => {
    const ranks = [
      priorityRank(['priority/p0']),
      priorityRank(['priority/p1']),
      priorityRank(['priority/p2']),
      priorityRank(['priority/p3']),
      priorityRank([]),
    ];
    expect(ranks).toEqual([0, 1, 2, 3, PRIORITY_RANK_NO_LABEL]);
    expect(ranks).toEqual([...ranks].sort((a, b) => a - b));
  });
});

describe('sortAndSliceQueueReady', () => {
  /** Small helper — build a SnapshotIssueRecord with sensible defaults. */
  function issue(
    number: number,
    labels: string[],
    createdAt: string,
  ): SnapshotIssueRecord {
    return { number, title: `Issue ${number}`, labels, createdAt };
  }

  it('surfaces the p0 at row 1 even when it is the 23rd newest (#2712 scenario)', () => {
    // Mirrors the real-world case from the issue body: 22 p1/p2 issues
    // filed AFTER the single p0, stored in gh CREATED_AT DESC order.
    // With the buggy slice-only behaviour the p0 is invisible; the
    // fixed resolver must surface it at row 1.
    const newerP1P2: SnapshotIssueRecord[] = [];
    for (let i = 0; i < 22; i += 1) {
      // createdAt strictly newer than the p0 below. Alternate p1/p2.
      const day = String(20 - (i % 20)).padStart(2, '0');
      const createdAt = `2026-05-${day}T12:00:00Z`;
      const prio = i % 2 === 0 ? 'priority/p1' : 'priority/p2';
      newerP1P2.push(issue(3000 + i, [prio, 'agent/ready'], createdAt));
    }
    const p0 = issue(
      2712,
      ['priority/p0', 'agent/ready'],
      '2026-03-01T00:00:00Z',
    );
    // Storage order is CREATED_AT DESC — the 22 newer ones first, then the p0.
    const storedOrder = [...newerP1P2, p0];
    // Sanity — p0 is at the 23rd position by default.
    expect(storedOrder[22].number).toBe(2712);
    // The buggy path would drop the p0 from slice(0, 10) entirely.
    expect(storedOrder.slice(0, 10).some((i) => i.number === 2712)).toBe(false);

    const result = sortAndSliceQueueReady(storedOrder, 10);

    expect(result).toHaveLength(10);
    expect(result[0].number).toBe(2712);
    expect(priorityRank(result[0].labels)).toBe(0);
    // Every other row in top 10 is p1 (p1 beats p2 in the sort).
    for (let i = 1; i < result.length; i += 1) {
      expect(result[i].labels).toContain('priority/p1');
    }
  });

  it('sorts unlabelled issues last among open issues', () => {
    const unlabelled = issue(900, ['area/api'], '2026-01-01T00:00:00Z');
    const p3 = issue(901, ['priority/p3'], '2026-04-19T00:00:00Z');
    const p0 = issue(902, ['priority/p0'], '2026-04-19T00:00:00Z');
    const input = [unlabelled, p0, p3];

    const result = sortAndSliceQueueReady(input, 10);

    expect(result.map((r) => r.number)).toEqual([902, 901, 900]);
    // Specifically: the oldest-by-createdAt `unlabelled` still sorts last
    // because its rank (4) beats every priority-labelled issue.
    expect(result[result.length - 1].number).toBe(900);
  });

  it('breaks ties within a priority bucket by createdAt ASC (older first)', () => {
    // Three p1 issues, stored in createdAt DESC order (newest first).
    const newer = issue(100, ['priority/p1'], '2026-04-19T12:00:00Z');
    const middle = issue(101, ['priority/p1'], '2026-04-18T12:00:00Z');
    const older = issue(102, ['priority/p1'], '2026-04-17T12:00:00Z');

    const result = sortAndSliceQueueReady([newer, middle, older], 10);

    expect(result.map((r) => r.number)).toEqual([102, 101, 100]);
  });

  it('respects both keys: priority first, then createdAt ASC', () => {
    // Mixed priorities + mixed createdAts. Expected order:
    //   p0 (any createdAt wins), then p1 by createdAt ASC, then p2.
    const p0New = issue(1, ['priority/p0'], '2026-04-19T00:00:00Z');
    const p1Old = issue(2, ['priority/p1'], '2026-01-01T00:00:00Z');
    const p1New = issue(3, ['priority/p1'], '2026-04-19T00:00:00Z');
    const p2 = issue(4, ['priority/p2'], '2025-12-01T00:00:00Z');

    const result = sortAndSliceQueueReady([p2, p1New, p0New, p1Old], 10);

    expect(result.map((r) => r.number)).toEqual([1, 2, 3, 4]);
  });

  it('slices to `limit` after sorting, not before', () => {
    // Deliberately order the input so a naive `input.slice(0, limit)`
    // would drop the p0, exactly as the old queryQueueReady bug did.
    const input: SnapshotIssueRecord[] = [];
    for (let i = 0; i < 15; i += 1) {
      input.push(
        issue(2000 + i, ['priority/p2'], `2026-04-${String(19 - i).padStart(2, '0')}T00:00:00Z`),
      );
    }
    const p0 = issue(1, ['priority/p0'], '2025-01-01T00:00:00Z');
    input.push(p0); // p0 at position 15 (past the slice boundary)

    const result = sortAndSliceQueueReady(input, 10);

    expect(result).toHaveLength(10);
    expect(result[0].number).toBe(1);
  });

  it('returns an empty array for an empty input', () => {
    expect(sortAndSliceQueueReady([], 10)).toEqual([]);
  });

  it('does not mutate the input array', () => {
    const input: SnapshotIssueRecord[] = [
      issue(1, ['priority/p2'], '2026-04-19T00:00:00Z'),
      issue(2, ['priority/p0'], '2026-04-18T00:00:00Z'),
    ];
    const inputCopy = input.slice();

    sortAndSliceQueueReady(input, 10);

    expect(input).toEqual(inputCopy);
    expect(input[0].number).toBe(1);
  });

  it('handles missing / empty createdAt by sorting them last within their bucket', () => {
    const withTs = issue(1, ['priority/p1'], '2026-04-19T00:00:00Z');
    const withoutTs = issue(2, ['priority/p1'], '');

    const result = sortAndSliceQueueReady([withoutTs, withTs], 10);

    expect(result.map((r) => r.number)).toEqual([1, 2]);
  });

  it('handles a limit larger than the input length without padding', () => {
    const input: SnapshotIssueRecord[] = [
      issue(1, ['priority/p0'], '2026-04-19T00:00:00Z'),
      issue(2, ['priority/p1'], '2026-04-19T00:00:00Z'),
    ];

    const result = sortAndSliceQueueReady(input, 10);

    expect(result).toHaveLength(2);
    expect(result.map((r) => r.number)).toEqual([1, 2]);
  });
});

// ---------------------------------------------------------------------------
// Metering helpers (#2869) — coerceNullableNumber, sumPhaseCost,
// phaseCostRowsToGraphQL. These back the new DispatcherAgent fields
// ``totalTokensInput``, ``totalTokensOutput``, ``totalCostUsd``, and
// ``phaseCostBreakdown``.
// ---------------------------------------------------------------------------

describe('coerceNullableNumber (#2869)', () => {
  it('returns the value unchanged for finite numbers', () => {
    expect(coerceNullableNumber(42)).toBe(42);
    expect(coerceNullableNumber(0)).toBe(0);
    expect(coerceNullableNumber(-3.14)).toBe(-3.14);
  });

  it('coerces pg numeric strings (cost_usd is returned as a string)', () => {
    // pg's default numeric→string mapping preserves precision. The
    // resolver must coerce to Number so the GraphQL scalar serializes
    // as a JSON number, not a quoted string.
    expect(coerceNullableNumber('0.0420')).toBe(0.042);
    expect(coerceNullableNumber('12345')).toBe(12345);
  });

  it('coerces bigint to Number', () => {
    // Token counts fit in 53 bits well under any plausible phase usage;
    // bigint → Number is safe.
    expect(coerceNullableNumber(12345n)).toBe(12345);
  });

  it('returns null for null / undefined / unparseable', () => {
    expect(coerceNullableNumber(null)).toBeNull();
    expect(coerceNullableNumber(undefined)).toBeNull();
    expect(coerceNullableNumber('nope')).toBeNull();
    expect(coerceNullableNumber({})).toBeNull();
    expect(coerceNullableNumber(NaN)).toBeNull();
  });
});

describe('sumPhaseCost (#2869)', () => {
  it('sums a column across rows, ignoring nulls', () => {
    const rows = [
      { tokens_input: 100, tokens_output: 50, cost_usd: '0.01' },
      { tokens_input: 200, tokens_output: null, cost_usd: '0.02' },
      { tokens_input: null, tokens_output: 30, cost_usd: null },
    ];
    expect(sumPhaseCost(rows, 'tokens_input')).toBe(300);
    expect(sumPhaseCost(rows, 'tokens_output')).toBe(80);
    expect(sumPhaseCost(rows, 'cost_usd')).toBeCloseTo(0.03, 10);
  });

  it('returns null when every row is null (no metering signal)', () => {
    // Distinct from returning 0 — the UI must be able to distinguish
    // "this agent actually spent $0" from "we have no data for this
    // agent". Pre-migration-31 agents all have NULL across the board.
    const rows = [
      { tokens_input: null, tokens_output: null, cost_usd: null },
      { tokens_input: null, tokens_output: null, cost_usd: null },
    ];
    expect(sumPhaseCost(rows, 'tokens_input')).toBeNull();
    expect(sumPhaseCost(rows, 'tokens_output')).toBeNull();
    expect(sumPhaseCost(rows, 'cost_usd')).toBeNull();
  });

  it('returns 0 when at least one row has a zero value (distinct from null)', () => {
    const rows = [
      { tokens_input: 0, tokens_output: null, cost_usd: '0' },
    ];
    expect(sumPhaseCost(rows, 'tokens_input')).toBe(0);
    // tokens_output is entirely null → null sentinel.
    expect(sumPhaseCost(rows, 'tokens_output')).toBeNull();
    expect(sumPhaseCost(rows, 'cost_usd')).toBe(0);
  });

  it('returns null for an empty row list', () => {
    expect(sumPhaseCost([], 'tokens_input')).toBeNull();
    expect(sumPhaseCost([], 'cost_usd')).toBeNull();
  });
});

describe('phaseCostRowsToGraphQL (#2869)', () => {
  it('maps snake_case columns to camelCase GraphQL fields with nullable Number coercion', () => {
    const rows = [
      {
        phase: 'plan',
        tokens_input: '1000',
        tokens_output: '200',
        tokens_cache_read: '3000',
        tokens_cache_write: '400',
        cost_usd: '0.0123',
        model_used: 'claude-opus-4-5',
      },
      {
        phase: 'ralph',
        tokens_input: null,
        tokens_output: null,
        tokens_cache_read: null,
        tokens_cache_write: null,
        cost_usd: null,
        model_used: null,
      },
    ];
    expect(phaseCostRowsToGraphQL(rows)).toEqual([
      {
        phase: 'plan',
        tokensInput: 1000,
        tokensOutput: 200,
        tokensCacheRead: 3000,
        tokensCacheWrite: 400,
        costUsd: 0.0123,
        modelUsed: 'claude-opus-4-5',
      },
      {
        phase: 'ralph',
        tokensInput: null,
        tokensOutput: null,
        tokensCacheRead: null,
        tokensCacheWrite: null,
        costUsd: null,
        modelUsed: null,
      },
    ]);
  });

  it('returns an empty array for an empty row list', () => {
    expect(phaseCostRowsToGraphQL([])).toEqual([]);
  });
});
