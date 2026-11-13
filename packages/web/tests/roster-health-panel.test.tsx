import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, fireEvent } from '@testing-library/react';
import type { RosterFreshnessItem } from '../src/lib/data-quality-queries';

let mockQueryResult: {
  data: unknown;
  loading: boolean;
  error: Error | undefined;
  refetch: ReturnType<typeof vi.fn>;
};

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>('@apollo/client');
  return {
    ...actual,
    useQuery: () => mockQueryResult,
  };
});

import {
  RosterHealthPanel,
  RosterHealthTable,
  formatAge,
  formatCourtId,
  formatErrorClass,
} from '../src/app/(main)/admin/data-quality/RosterHealthPanel';

const HOUR = 3_600_000;

function iso(hoursAgo: number): string {
  return new Date(Date.now() - hoursAgo * HOUR).toISOString();
}

function makeItem(overrides: Partial<RosterFreshnessItem>): RosterFreshnessItem {
  return {
    courtId: 'ca_orange',
    status: 'ok',
    isStale: false,
    lastSuccessAt: iso(2),
    lastSnapshotAt: iso(200),
    lastAttemptAt: iso(2),
    lastOutcome: 'success',
    lastErrorClass: null,
    lastHttpStatus: null,
    lastErrorMessage: null,
    lastDepartmentCount: 120,
    consecutiveFailures: 0,
    ageHours: 2,
    staleThresholdHours: 168,
    ...overrides,
  };
}

const staleLA = makeItem({
  courtId: 'ca_los_angeles',
  status: 'stale',
  isStale: true,
  lastSuccessAt: iso(24 * 135),
  lastSnapshotAt: iso(24 * 135),
  lastAttemptAt: iso(1),
  lastOutcome: 'failure',
  lastErrorClass: 'http_5xx',
  lastHttpStatus: 503,
  lastErrorMessage: "HTTPStatusError: Server error '503 Service Unavailable'",
  lastDepartmentCount: null,
  consecutiveFailures: 4,
  ageHours: 24 * 135,
});

const failingKern = makeItem({
  courtId: 'ca_kern',
  status: 'failing',
  lastAttemptAt: iso(1),
  lastOutcome: 'failure',
  lastErrorClass: 'network',
  lastErrorMessage: 'ConnectTimeout: timed out',
  consecutiveFailures: 1,
});

beforeEach(() => {
  mockQueryResult = { data: undefined, loading: false, error: undefined, refetch: vi.fn() };
});

describe('RosterHealthTable', () => {
  it('flags a stale court with its error class', () => {
    render(<RosterHealthTable items={[makeItem({}), staleLA]} />);

    const row = screen.getByTestId('roster-row-ca_los_angeles');
    expect(row).toHaveAttribute('data-status', 'stale');
    expect(within(row).getByText('Stale')).toBeInTheDocument();
    expect(within(row).getByText('Los Angeles (CA)')).toBeInTheDocument();
    expect(within(row).getByText('HTTP 5xx · 503')).toBeInTheDocument();
    expect(within(row).getByText('135d ago')).toBeInTheDocument();
    expect(within(row).getByText(/4 failures in a row/)).toBeInTheDocument();

    expect(screen.getByText('1 stale')).toBeInTheDocument();
    expect(screen.getByText(/stale after 7 days/)).toBeInTheDocument();
  });

  it('shows healthy courts as OK with department count and no error', () => {
    render(<RosterHealthTable items={[makeItem({})]} />);
    const row = screen.getByTestId('roster-row-ca_orange');
    expect(within(row).getByText('OK')).toBeInTheDocument();
    expect(within(row).getByText('120 departments')).toBeInTheDocument();
    expect(within(row).getByText('—')).toBeInTheDocument();
    expect(screen.getByText('None stale')).toBeInTheDocument();
  });

  it('distinguishes failing-but-not-stale courts', () => {
    render(<RosterHealthTable items={[failingKern]} />);
    const row = screen.getByTestId('roster-row-ca_kern');
    expect(within(row).getByText('Failing')).toBeInTheDocument();
    expect(within(row).getByText('Network / timeout')).toBeInTheDocument();
    expect(screen.getByText(/1 failing/)).toBeInTheDocument();
  });

  it('renders courts with no recorded attempt', () => {
    render(
      <RosterHealthTable
        items={[
          makeItem({
            courtId: 'ca_riverside',
            status: 'stale',
            isStale: true,
            lastSuccessAt: null,
            lastAttemptAt: null,
            lastOutcome: null,
            lastDepartmentCount: null,
          }),
        ]}
      />,
    );
    const row = screen.getByTestId('roster-row-ca_riverside');
    expect(within(row).getByText('Never')).toBeInTheDocument();
    expect(within(row).getByText('Not recorded')).toBeInTheDocument();
  });

  it('shows an empty state when no courts are reported', () => {
    render(<RosterHealthTable items={[]} />);
    expect(screen.getByText(/No roster fetches recorded yet/)).toBeInTheDocument();
  });
});

describe('RosterHealthPanel', () => {
  it('renders the table from the rosterFreshness query', () => {
    mockQueryResult.data = { rosterFreshness: [staleLA] };
    render(<RosterHealthPanel />);
    expect(screen.getByRole('heading', { name: 'Judge Roster Freshness' })).toBeInTheDocument();
    expect(screen.getByTestId('roster-row-ca_los_angeles')).toHaveAttribute('data-status', 'stale');
  });

  it('shows a skeleton while loading', () => {
    mockQueryResult.loading = true;
    const { container } = render(<RosterHealthPanel />);
    expect(container.querySelector('.animate-pulse')).not.toBeNull();
  });

  it('shows an inline error with retry on failure', () => {
    mockQueryResult.error = new Error('boom');
    render(<RosterHealthPanel />);
    expect(screen.getByText('Failed to load roster freshness.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(mockQueryResult.refetch).toHaveBeenCalled();
  });
});

describe('formatters', () => {
  it('formats court ids', () => {
    expect(formatCourtId('ca_san_francisco')).toBe('San Francisco (CA)');
    expect(formatCourtId('weird')).toBe('weird');
  });

  it('formats ages', () => {
    const now = new Date('2026-11-13T12:00:00Z');
    expect(formatAge(null, now)).toBe('Never');
    expect(formatAge('garbage', now)).toBe('Unknown');
    expect(formatAge('2026-11-13T11:50:00Z', now)).toBe('10m ago');
    expect(formatAge('2026-11-13T02:00:00Z', now)).toBe('10h ago');
    expect(formatAge('2026-11-03T12:00:00Z', now)).toBe('10d ago');
  });

  it('formats error classes', () => {
    expect(formatErrorClass(makeItem({}))).toBeNull();
    expect(
      formatErrorClass(makeItem({ lastOutcome: 'failure', lastErrorClass: 'http_403', lastHttpStatus: 403 })),
    ).toBe('HTTP 403 (blocked)');
    expect(
      formatErrorClass(makeItem({ lastOutcome: 'failure', lastErrorClass: 'brand_new' })),
    ).toBe('brand_new');
  });
});
