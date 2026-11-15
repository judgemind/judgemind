import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, fireEvent } from '@testing-library/react';
import type {
  RawKeyReconciliationRun,
  RawKeyReconcileScopeItem,
} from '../src/lib/data-quality-queries';

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
  RawKeyReconciliationPanel,
  RawKeyReconciliationTable,
  formatActionLabel,
  formatDayRange,
  formatSkipReasons,
} from '../src/app/(main)/admin/data-quality/RawKeyReconciliationPanel';

function makeScope(overrides: Partial<RawKeyReconcileScopeItem>): RawKeyReconcileScopeItem {
  return {
    scope: 'ca/fresno',
    rawKeys: 277,
    represented: 275,
    skipped: 2,
    unrepresented: 0,
    pending: 0,
    datedExcluded: 21,
    skippedByReason: [{ reason: 'validation_fail', count: 2 }],
    oldestUnrepresentedDay: null,
    newestUnrepresentedDay: null,
    ...overrides,
  };
}

const la = makeScope({
  scope: 'ca/los_angeles',
  rawKeys: 36677,
  represented: 31395,
  skipped: 0,
  unrepresented: 5282,
  pending: 3,
  skippedByReason: [],
  oldestUnrepresentedDay: '2026-04-11',
  newestUnrepresentedDay: '2026-07-02',
});

function makeRun(overrides: Partial<RawKeyReconciliationRun> = {}): RawKeyReconciliationRun {
  return {
    runId: 'run-1',
    startedAt: '2026-11-15T13:00:00Z',
    finishedAt: '2026-11-15T13:01:00Z',
    rawKeys: 36954,
    represented: 31670,
    skipped: 2,
    unrepresented: 5282,
    pending: 3,
    datedExcluded: 4726,
    action: 'over_threshold',
    scopes: [la, makeScope({})],
    ...overrides,
  };
}

beforeEach(() => {
  mockQueryResult = { data: undefined, loading: false, error: undefined, refetch: vi.fn() };
});

describe('RawKeyReconciliationTable', () => {
  it('shows per-scope represented / skipped / unrepresented counts', () => {
    render(<RawKeyReconciliationTable run={makeRun()} />);

    const summary = screen.getByTestId('raw-reconcile-summary');
    expect(summary).toHaveTextContent('5,282 unrepresented');
    expect(summary).toHaveTextContent('31,670 represented');
    expect(summary).toHaveTextContent('Too many to auto-heal; issue filed');
    expect(summary).toHaveTextContent('2026-11-15 13:01Z');
    expect(summary).toHaveTextContent('4,726 legacy dated keys not reconciled');

    const row = screen.getByTestId('raw-reconcile-row-ca/los_angeles');
    expect(within(row).getByText('36,677')).toBeInTheDocument();
    expect(within(row).getByText('5,282')).toBeInTheDocument();
    expect(within(row).getByText('2026-04-11 – 2026-07-02')).toBeInTheDocument();
    expect(within(row).getByText('+3 pending')).toBeInTheDocument();

    const fresno = screen.getByTestId('raw-reconcile-row-ca/fresno');
    expect(within(fresno).getByText('validation fail 2')).toBeInTheDocument();
  });

  it('says so when nothing is unrepresented', () => {
    render(
      <RawKeyReconciliationTable
        run={makeRun({ unrepresented: 0, pending: 0, datedExcluded: 0, action: 'none', scopes: [] })}
      />,
    );
    const summary = screen.getByTestId('raw-reconcile-summary');
    expect(summary).toHaveTextContent('None unrepresented');
    expect(summary).toHaveTextContent('Nothing to heal');
    expect(summary).not.toHaveTextContent('pending');
  });

  it('shows an empty state before the first run', () => {
    render(<RawKeyReconciliationTable run={null} />);
    expect(screen.getByText(/No reconciliation run recorded yet/)).toBeInTheDocument();
  });
});

describe('RawKeyReconciliationPanel', () => {
  it('renders the table from the rawKeyReconciliation query', () => {
    mockQueryResult.data = { rawKeyReconciliation: makeRun() };
    render(<RawKeyReconciliationPanel />);
    expect(screen.getByRole('heading', { name: 'Raw-Key Reconciliation' })).toBeInTheDocument();
    expect(screen.getByTestId('raw-reconcile-row-ca/los_angeles')).toBeInTheDocument();
  });

  it('shows the empty state when the query returns no run', () => {
    mockQueryResult.data = { rawKeyReconciliation: null };
    render(<RawKeyReconciliationPanel />);
    expect(screen.getByText(/No reconciliation run recorded yet/)).toBeInTheDocument();
  });

  it('shows a skeleton while loading', () => {
    mockQueryResult.loading = true;
    const { container } = render(<RawKeyReconciliationPanel />);
    expect(container.querySelector('.animate-pulse')).not.toBeNull();
  });

  it('shows an inline error with retry on failure', () => {
    mockQueryResult.error = new Error('boom');
    render(<RawKeyReconciliationPanel />);
    expect(screen.getByText('Failed to load raw-key reconciliation.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(mockQueryResult.refetch).toHaveBeenCalled();
  });
});

describe('formatters', () => {
  it('formats actions', () => {
    expect(formatActionLabel('reingest')).toBe('Auto-reingest launched');
    expect(formatActionLabel('brand_new')).toBe('brand_new');
  });

  it('formats skip reasons', () => {
    expect(formatSkipReasons(makeScope({ skippedByReason: [] }))).toBe('—');
    expect(
      formatSkipReasons(
        makeScope({
          skippedByReason: [
            { reason: 'santa_clara_placeholder', count: 1200 },
            { reason: 'brand_new', count: 1 },
          ],
        }),
      ),
    ).toBe('SC placeholder 1,200, brand_new 1');
  });

  it('formats day ranges', () => {
    expect(formatDayRange(makeScope({}))).toBeNull();
    expect(
      formatDayRange(
        makeScope({ oldestUnrepresentedDay: '2026-06-01', newestUnrepresentedDay: '2026-06-01' }),
      ),
    ).toBe('2026-06-01');
  });
});
