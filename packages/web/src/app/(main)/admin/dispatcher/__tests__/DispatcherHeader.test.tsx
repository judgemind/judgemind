import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DispatcherHeader, deriveDaemonStatus, isUnhealthy, UNHEALTHY_AGE_SECONDS } from '../DispatcherHeader';

const now = Date.parse('2026-04-18T12:00:00Z');

describe('isUnhealthy', () => {
  it('returns false for age < UNHEALTHY_AGE_SECONDS (89s)', () => {
    expect(isUnhealthy(89)).toBe(false);
  });

  it('returns false at exactly UNHEALTHY_AGE_SECONDS (90s boundary is exclusive)', () => {
    expect(isUnhealthy(UNHEALTHY_AGE_SECONDS)).toBe(false);
  });

  it('returns true for age > UNHEALTHY_AGE_SECONDS (91s)', () => {
    expect(isUnhealthy(91)).toBe(true);
  });

  it('UNHEALTHY_AGE_SECONDS is 90', () => {
    expect(UNHEALTHY_AGE_SECONDS).toBe(90);
  });
});

describe('deriveDaemonStatus', () => {
  it('null run → stopped', () => {
    expect(deriveDaemonStatus(null, now)).toBe('stopped');
  });

  it('stoppedAt set → stopped', () => {
    const run = {
      runId: 'r',
      startedAt: '2026-04-18T00:00:00Z',
      stoppedAt: '2026-04-18T11:00:00Z',
      heartbeatTs: '2026-04-18T11:00:00Z',
      versionSha: 'deadbeef',
      host: 'h',
      pid: 1,
    };
    expect(deriveDaemonStatus(run, now)).toBe('stopped');
  });

  it('stale heartbeat (91s old) → unhealthy at 90s threshold', () => {
    // 91 seconds before now = 11:58:29Z
    const heartbeatTs = new Date(now - 91 * 1000).toISOString();
    const run = {
      runId: 'r',
      startedAt: '2026-04-18T00:00:00Z',
      stoppedAt: null,
      heartbeatTs,
      versionSha: 'deadbeef',
      host: 'h',
      pid: 1,
    };
    expect(deriveDaemonStatus(run, now)).toBe('unhealthy');
  });

  it('fresh heartbeat (89s old) → running at 90s threshold', () => {
    // 89 seconds before now = 11:58:31Z
    const heartbeatTs = new Date(now - 89 * 1000).toISOString();
    const run = {
      runId: 'r',
      startedAt: '2026-04-18T00:00:00Z',
      stoppedAt: null,
      heartbeatTs,
      versionSha: 'deadbeef',
      host: 'h',
      pid: 1,
    };
    expect(deriveDaemonStatus(run, now)).toBe('running');
  });
});

describe('DispatcherHeader', () => {
  const run = {
    runId: 'r',
    startedAt: '2026-04-18T10:00:00Z',
    stoppedAt: null,
    heartbeatTs: '2026-04-18T11:59:30Z',
    versionSha: 'deadbeefcafe0000',
    host: 'ip-10-0-11-146',
    pid: 1,
  };

  it('renders the one-line header with pill + metadata (no borders)', () => {
    const { container } = render(<DispatcherHeader currentRun={run} nowMs={now} />);
    const pill = screen.getByTestId('daemon-status-pill');
    expect(pill.textContent).toMatch(/running/i);
    // uptime = 2h 0m
    expect(screen.getByText('2h 0m')).toBeInTheDocument();
    // sha truncated to 8 chars
    expect(screen.getByText('deadbeef')).toBeInTheDocument();
    expect(screen.getByText('ip-10-0-11-146')).toBeInTheDocument();
    // No decorative border/card wrapper.
    const outer = container.firstChild as HTMLElement;
    expect(outer.className).not.toContain('border');
    expect(outer.className).not.toContain('bg-card');
    expect(outer.className).not.toContain('rounded');
  });

  it('renders em-dash placeholders when no run exists', () => {
    render(<DispatcherHeader currentRun={null} nowMs={now} />);
    expect(screen.getByTestId('daemon-status-pill').textContent).toMatch(/stopped/i);
    // uptime and sha both em-dashed.
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThanOrEqual(2);
  });

  it('status pill container has aria-live polite for transition announcements', () => {
    const { container } = render(<DispatcherHeader currentRun={run} nowMs={now} />);
    const outer = container.firstChild as HTMLElement;
    expect(outer.getAttribute('aria-live')).toBe('polite');
  });
});
