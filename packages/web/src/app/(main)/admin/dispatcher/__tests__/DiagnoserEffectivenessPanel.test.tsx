/**
 * Tests for DiagnoserEffectivenessPanel (issue #2800).
 *
 * Covers:
 *   - Empty state: renders the "No diagnoses with resolved outcomes yet." copy.
 *   - Rollup rows: renders one table row per (recommendedAction × observedOutcome × day) bucket.
 *   - Loading state: renders skeleton placeholders before data arrives.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { DiagnoserEffectivenessRow, WeeklyDiagnoserReportData } from '@/lib/dispatcher-queries';

// ---------------------------------------------------------------------------
// Apollo useQuery mock — lets each test drive loading/data independently.
// ---------------------------------------------------------------------------

let mockQueryResult: {
  data: WeeklyDiagnoserReportData | undefined;
  loading: boolean;
  error: Error | undefined;
};

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>('@apollo/client');
  return {
    ...actual,
    useQuery: () => mockQueryResult,
  };
});

import { DiagnoserEffectivenessPanel } from '../DiagnoserEffectivenessPanel';

function makeRow(overrides: Partial<DiagnoserEffectivenessRow> = {}): DiagnoserEffectivenessRow {
  return {
    recommendedAction: 'retry',
    observedOutcome: 'succeeded',
    count: 3,
    day: '2026-04-18T00:00:00.000Z',
    ...overrides,
  };
}

describe('DiagnoserEffectivenessPanel', () => {
  it('renders the empty-state copy when query returns []', () => {
    mockQueryResult = {
      data: { weeklyDiagnoserReport: [] },
      loading: false,
      error: undefined,
    };
    render(<DiagnoserEffectivenessPanel />);
    expect(
      screen.getByText(/no diagnoses with resolved outcomes yet/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('renders one table row per (recommendedAction × observedOutcome × day) tuple', () => {
    mockQueryResult = {
      data: {
        weeklyDiagnoserReport: [
          makeRow({ recommendedAction: 'retry', observedOutcome: 'succeeded', count: 5, day: '2026-04-18T00:00:00.000Z' }),
          makeRow({ recommendedAction: 'retry', observedOutcome: 'failed', count: 2, day: '2026-04-18T00:00:00.000Z' }),
          makeRow({ recommendedAction: 'skip', observedOutcome: 'failed', count: 1, day: '2026-04-17T00:00:00.000Z' }),
        ],
      },
      loading: false,
      error: undefined,
    };
    render(<DiagnoserEffectivenessPanel />);
    const rows = screen.getAllByTestId('diagnoser-effectiveness-row');
    expect(rows).toHaveLength(3);
    // First row: retry / succeeded / 5
    expect(rows[0]).toHaveTextContent('2026-04-18');
    expect(rows[0]).toHaveTextContent('retry');
    expect(rows[0]).toHaveTextContent('succeeded');
    expect(rows[0]).toHaveTextContent('5');
    // Second row: retry / failed / 2
    expect(rows[1]).toHaveTextContent('failed');
    expect(rows[1]).toHaveTextContent('2');
    // Third row: skip / failed / 1
    expect(rows[2]).toHaveTextContent('2026-04-17');
    expect(rows[2]).toHaveTextContent('skip');
    expect(rows[2]).toHaveTextContent('1');
  });

  it('renders skeleton placeholders while loading with no data yet', () => {
    mockQueryResult = {
      data: undefined,
      loading: true,
      error: undefined,
    };
    render(<DiagnoserEffectivenessPanel />);
    const skeletons = screen.getAllByTestId('diagnoser-row-skeleton');
    expect(skeletons.length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText(/no diagnoses/i)).toBeNull();
  });
});
