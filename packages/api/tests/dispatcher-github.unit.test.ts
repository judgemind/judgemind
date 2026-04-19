/**
 * Unit tests for the dispatcher GitHub helper (#2805). Focuses on the
 * pure parsing + cache behaviour — the network path is exercised via the
 * integration suite.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __resetCaches,
  extractPriority,
  fetchIssue,
  parseBlockedBy,
  searchIssues,
} from '../src/graphql/dispatcher/github';

describe('extractPriority', () => {
  it('returns the priority for a priority/pN label', () => {
    expect(extractPriority(['area/frontend', 'priority/p1', 'agent/ready'])).toBe('p1');
    expect(extractPriority(['priority/p0'])).toBe('p0');
  });

  it('returns null when no priority label is present', () => {
    expect(extractPriority(['area/frontend'])).toBeNull();
    expect(extractPriority([])).toBeNull();
  });

  it('ignores malformed labels', () => {
    expect(extractPriority(['priority/p9'])).toBeNull();
    expect(extractPriority(['priority/high'])).toBeNull();
  });
});

describe('parseBlockedBy', () => {
  it('returns [] on null/empty body', () => {
    expect(parseBlockedBy(null)).toEqual([]);
    expect(parseBlockedBy('')).toEqual([]);
  });

  it('extracts Blocked by #N references', () => {
    const body = 'Some description.\n\nBlocked by #1234\nBlocked by #5678\nParent: #2805';
    expect(parseBlockedBy(body)).toEqual([1234, 5678]);
  });

  it('ignores Parent: #N', () => {
    expect(parseBlockedBy('Parent: #1\n')).toEqual([]);
  });
});

describe('fetchIssue — cache behaviour', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    __resetCaches();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('caches the response and does not re-fetch inside the TTL', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        number: 42,
        title: 'Hello',
        labels: [{ name: 'priority/p2' }],
        created_at: '2026-04-18T00:00:00Z',
        body: 'Some body',
      }),
    });

    const first = await fetchIssue(42);
    expect(first).not.toBeNull();
    expect(first?.title).toBe('Hello');
    expect(first?.labels).toEqual(['priority/p2']);

    // Second call — should be served from cache (fetchMock called once).
    const second = await fetchIssue(42);
    expect(second).toEqual(first);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('returns null + caches null when the response is not ok', async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 404 });
    const result = await fetchIssue(999);
    expect(result).toBeNull();
    // Second call inside TTL — still null, still only one fetch.
    const second = await fetchIssue(999);
    expect(second).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('returns null when fetch itself rejects', async () => {
    fetchMock.mockRejectedValueOnce(new Error('network fail'));
    expect(await fetchIssue(1)).toBeNull();
  });
});

describe('searchIssues — cache + parse', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    __resetCaches();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('parses the search result items and caches them', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        items: [
          {
            number: 1,
            title: 'a',
            labels: [{ name: 'status/blocked' }],
            created_at: '2026-04-18T00:00:00Z',
            body: 'Blocked by #50\n',
          },
          {
            number: 2,
            title: 'b',
            labels: ['priority/p2'],
            created_at: '2026-04-17T00:00:00Z',
            body: null,
          },
        ],
      }),
    });

    const first = await searchIssues('label:status/blocked');
    expect(first).toHaveLength(2);
    expect(first[0].labels).toEqual(['status/blocked']);
    expect(first[1].labels).toEqual(['priority/p2']);

    // Cached — fetch count stays 1.
    const second = await searchIssues('label:status/blocked');
    expect(second).toEqual(first);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('returns [] on fetch failure', async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 500 });
    expect(await searchIssues('label:status/blocked')).toEqual([]);
  });
});
