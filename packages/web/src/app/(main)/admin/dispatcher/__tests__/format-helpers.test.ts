import { describe, expect, it } from 'vitest';
import {
  formatDurationMs,
  formatRelativeTime,
  formatUptime,
  groupFailuresByCategory,
  shortAgentId,
  worktreeLogsUrl,
} from '../format-helpers';

describe('formatDurationMs', () => {
  it('returns em-dash for negative or non-finite input', () => {
    expect(formatDurationMs(-1)).toBe('—');
    expect(formatDurationMs(Number.NaN)).toBe('—');
  });

  it('formats seconds', () => {
    expect(formatDurationMs(0)).toBe('0s');
    expect(formatDurationMs(12_000)).toBe('12s');
  });

  it('formats minutes and seconds', () => {
    expect(formatDurationMs(252_000)).toBe('4m 12s');
  });

  it('formats hours and minutes', () => {
    expect(formatDurationMs(2 * 3600_000 + 15 * 60_000)).toBe('2h 15m');
  });

  it('formats days and hours', () => {
    expect(formatDurationMs(28 * 3600_000)).toBe('1d 4h');
  });
});

describe('formatUptime', () => {
  it('returns em-dash on invalid timestamps', () => {
    expect(formatUptime('nonsense')).toBe('—');
  });

  it('returns em-dash on future timestamps', () => {
    const future = new Date(Date.now() + 1000).toISOString();
    expect(formatUptime(future)).toBe('—');
  });

  it('returns a duration for past timestamps', () => {
    const now = Date.parse('2026-04-18T00:00:00Z');
    const start = new Date(now - 65_000).toISOString();
    expect(formatUptime(start, now)).toBe('1m 5s');
  });
});

describe('formatRelativeTime', () => {
  const now = Date.parse('2026-04-18T12:00:00Z');

  it('returns em-dash for null/invalid', () => {
    expect(formatRelativeTime(null, now)).toBe('—');
    expect(formatRelativeTime('nonsense', now)).toBe('—');
  });

  it('returns em-dash for future timestamps', () => {
    const future = new Date(now + 1000).toISOString();
    expect(formatRelativeTime(future, now)).toBe('—');
  });

  // #2818 — defensive against backend fallbacks that emit
  // `new Date(0).toISOString()` to satisfy a non-null `DateTime!` field.
  // Without this guard, callers saw "20562 d ago" on every such row.
  it('#2818: returns em-dash for the unix epoch (new Date(0))', () => {
    expect(formatRelativeTime(new Date(0).toISOString(), now)).toBe('—');
  });

  it('#2818: returns em-dash for any pre-2000 timestamp (epoch sentinel)', () => {
    expect(formatRelativeTime('1970-01-01T00:00:00.000Z', now)).toBe('—');
    expect(formatRelativeTime('1985-06-15T12:00:00Z', now)).toBe('—');
    expect(formatRelativeTime('1999-12-31T23:59:59Z', now)).toBe('—');
  });

  it('formats seconds', () => {
    const ts = new Date(now - 5_000).toISOString();
    expect(formatRelativeTime(ts, now)).toBe('5s ago');
  });

  it('formats minutes', () => {
    const ts = new Date(now - 3 * 60_000).toISOString();
    expect(formatRelativeTime(ts, now)).toBe('3 min ago');
  });

  it('formats hours', () => {
    const ts = new Date(now - 2 * 3600_000).toISOString();
    expect(formatRelativeTime(ts, now)).toBe('2 h ago');
  });

  it('formats days', () => {
    const ts = new Date(now - 3 * 24 * 3600_000).toISOString();
    expect(formatRelativeTime(ts, now)).toBe('3 d ago');
  });
});

describe('shortAgentId', () => {
  it('returns the id unchanged when short', () => {
    expect(shortAgentId('abcd')).toBe('abcd');
  });

  it('truncates to 8 chars with ellipsis', () => {
    expect(shortAgentId('abcdef01234567')).toBe('abcdef01…');
  });
});

describe('worktreeLogsUrl', () => {
  it('returns a CloudWatch link for an agent-<id> path', () => {
    const url = worktreeLogsUrl('.claude/worktrees/agent-abcd1234');
    expect(url).not.toBeNull();
    expect(url).toContain('agent-abcd1234');
    expect(url).toContain('judgemind-dispatcher-dev');
  });

  it('returns null for an unrecognized path', () => {
    expect(worktreeLogsUrl('/tmp/random')).toBeNull();
  });
});

describe('groupFailuresByCategory', () => {
  it('returns [] on empty input', () => {
    expect(groupFailuresByCategory([])).toEqual([]);
  });

  it('groups by category and sorts by count desc', () => {
    const failures = [
      { category: 'hook_failure', ts: '2026-04-18T12:00:00Z' },
      { category: 'hook_failure', ts: '2026-04-18T12:05:00Z' },
      { category: 'supervisor_timeout', ts: '2026-04-18T12:03:00Z' },
    ];
    const groups = groupFailuresByCategory(failures);
    expect(groups).toHaveLength(2);
    expect(groups[0].category).toBe('hook_failure');
    expect(groups[0].count).toBe(2);
    // mostRecent should be the later ts
    expect(groups[0].mostRecent.ts).toBe('2026-04-18T12:05:00Z');
    expect(groups[1].category).toBe('supervisor_timeout');
  });
});
