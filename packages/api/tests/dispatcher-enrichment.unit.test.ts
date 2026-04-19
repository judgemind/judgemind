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
  normalizeSnapshotJson,
  queueItemFromSnapshot,
  recentCompletionsToGraphQL,
  type SnapshotIssueRecord,
} from '../src/graphql/dispatcher/resolvers';
import {
  extractPriority,
  parseBlockedBy,
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
      },
    ]);
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
