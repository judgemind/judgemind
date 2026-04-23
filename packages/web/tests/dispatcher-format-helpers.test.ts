import { describe, it, expect } from 'vitest';
import {
  formatDurationMs,
  formatUptime,
  groupFailuresByCategory,
  shortAgentId,
  worktreeLogsUrl,
} from '../src/app/(main)/admin/dispatcher/format-helpers';
import { deriveDaemonStatus } from '../src/app/(main)/admin/dispatcher/DispatcherHeader';
import type { DispatcherRun } from '../src/lib/dispatcher-queries';

describe('formatDurationMs', () => {
  it('formats sub-minute durations in seconds', () => {
    expect(formatDurationMs(0)).toBe('0s');
    expect(formatDurationMs(59_000)).toBe('59s');
  });
  it('formats sub-hour durations in minutes + seconds', () => {
    expect(formatDurationMs(60_000)).toBe('1m 0s');
    expect(formatDurationMs(252_000)).toBe('4m 12s');
  });
  it('formats sub-day durations in hours + minutes', () => {
    expect(formatDurationMs(3_600_000)).toBe('1h 0m');
    expect(formatDurationMs(8_100_000)).toBe('2h 15m');
  });
  it('formats multi-day durations in days + hours', () => {
    expect(formatDurationMs(86_400_000)).toBe('1d 0h');
    expect(formatDurationMs(101_400_000)).toBe('1d 4h');
  });
  it('returns em-dash for negative or invalid input', () => {
    expect(formatDurationMs(-1)).toBe('—');
    expect(formatDurationMs(Number.NaN)).toBe('—');
    expect(formatDurationMs(Number.POSITIVE_INFINITY)).toBe('—');
  });
});

describe('formatUptime', () => {
  const now = Date.parse('2026-04-18T12:00:00Z');
  it('returns formatted duration from a valid ISO string', () => {
    expect(formatUptime('2026-04-18T11:59:00Z', now)).toBe('1m 0s');
  });
  it('returns em-dash for unparseable input', () => {
    expect(formatUptime('not-a-date', now)).toBe('—');
  });
  it('returns em-dash for timestamps in the future', () => {
    expect(formatUptime('2026-04-18T12:01:00Z', now)).toBe('—');
  });
});

describe('shortAgentId', () => {
  it('keeps short ids intact', () => {
    expect(shortAgentId('abc123')).toBe('abc123');
  });
  it('truncates long ids', () => {
    expect(shortAgentId('agent-aca547d3-and-more')).toBe('agent-ac…');
  });
});

describe('worktreeLogsUrl', () => {
  it('returns a CloudWatch URL for worktree paths with agent-<id>', () => {
    const url = worktreeLogsUrl('.claude/worktrees/agent-aca547d3');
    expect(url).toBeTruthy();
    expect(url).toContain('us-west-2');
    expect(url).toContain('agent-aca547d3');
    expect(url).toContain('$252Fecs$252Fjudgemind-dispatcher-dev');
  });
  it('returns null when no agent id can be extracted', () => {
    expect(worktreeLogsUrl('/tmp/random')).toBeNull();
  });
});

describe('deriveDaemonStatus', () => {
  const now = Date.parse('2026-04-18T12:00:00Z');
  function makeRun(overrides: Partial<DispatcherRun>): DispatcherRun {
    return {
      runId: 'run-1',
      startedAt: '2026-04-18T11:00:00Z',
      stoppedAt: null,
      heartbeatTs: '2026-04-18T11:59:00Z',
      versionSha: 'abcdef12',
      host: 'dispatcher-host',
      pid: 1234,
      ...overrides,
    };
  }

  it('returns "stopped" when no run exists', () => {
    expect(deriveDaemonStatus(null, now)).toBe('stopped');
  });

  it('returns "stopped" when the run has stoppedAt populated', () => {
    expect(deriveDaemonStatus(makeRun({ stoppedAt: '2026-04-18T11:30:00Z' }), now)).toBe('stopped');
  });

  it('returns "running" when heartbeat is fresh', () => {
    expect(deriveDaemonStatus(makeRun({ heartbeatTs: '2026-04-18T11:59:30Z' }), now)).toBe('running');
  });

  it('returns "unhealthy" when heartbeat is older than 3 minutes', () => {
    expect(deriveDaemonStatus(makeRun({ heartbeatTs: '2026-04-18T11:55:00Z' }), now)).toBe('unhealthy');
  });

  it('returns "unhealthy" for malformed heartbeatTs', () => {
    expect(deriveDaemonStatus(makeRun({ heartbeatTs: 'bogus' }), now)).toBe('unhealthy');
  });
});

describe('groupFailuresByCategory', () => {
  type F = { category: string; ts: string; id: string };

  it('returns empty array for no input', () => {
    expect(groupFailuresByCategory<F>([])).toEqual([]);
  });

  it('groups by category and counts occurrences', () => {
    const failures: F[] = [
      { id: '1', category: 'runtime_error', ts: '2026-04-18T10:00:00Z' },
      { id: '2', category: 'runtime_error', ts: '2026-04-18T11:00:00Z' },
      { id: '3', category: 'autocompact', ts: '2026-04-18T09:00:00Z' },
    ];
    const groups = groupFailuresByCategory(failures);
    expect(groups).toHaveLength(2);
    expect(groups[0].category).toBe('runtime_error');
    expect(groups[0].count).toBe(2);
    expect(groups[0].mostRecent.id).toBe('2');
    expect(groups[1].category).toBe('autocompact');
    expect(groups[1].count).toBe(1);
  });

  it('sorts categories by count desc', () => {
    const failures: F[] = [
      { id: '1', category: 'a', ts: '2026-04-18T10:00:00Z' },
      { id: '2', category: 'b', ts: '2026-04-18T11:00:00Z' },
      { id: '3', category: 'b', ts: '2026-04-18T09:00:00Z' },
      { id: '4', category: 'b', ts: '2026-04-18T08:00:00Z' },
    ];
    const groups = groupFailuresByCategory(failures);
    expect(groups.map((g) => g.category)).toEqual(['b', 'a']);
  });
});
