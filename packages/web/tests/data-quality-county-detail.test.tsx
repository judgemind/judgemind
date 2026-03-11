import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

let mockMetricsQueryResult: {
  data: unknown;
  loading: boolean;
  error: Error | undefined;
};

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    useQuery: () => mockMetricsQueryResult,
  };
});

// Mock recharts
vi.mock('recharts', () => ({
  LineChart: ({ children }: { children: React.ReactNode }) => <div data-testid="line-chart">{children}</div>,
  Line: () => <div />,
  XAxis: () => <div />,
  YAxis: () => <div />,
  CartesianGrid: () => <div />,
  Tooltip: () => <div />,
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Legend: () => <div />,
}));

import { CountyDetail } from '../src/app/admin/data-quality/CountyDetail';
import type { DataQualityOverviewItem } from '../src/lib/data-quality-queries';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeOverview(): DataQualityOverviewItem {
  return {
    county: 'Los Angeles',
    healthStatus: 'green',
    rulingCount24h: 42,
    fieldCompletenessPct: 95.5,
    scraperLastSuccessAgeHours: 1.2,
    lastUpdated: '2026-03-11T10:00:00Z',
  };
}

function makeMetricsData() {
  return {
    dataQualityMetrics: {
      edges: [
        {
          cursor: 'c1',
          node: {
            id: '1',
            recordedAt: '2026-03-11T10:00:00Z',
            county: 'Los Angeles',
            metricName: 'ruling_count_24h',
            metricValue: 42,
            metadata: null,
          },
        },
        {
          cursor: 'c2',
          node: {
            id: '2',
            recordedAt: '2026-03-11T10:00:00Z',
            county: 'Los Angeles',
            metricName: 'field_completeness_pct',
            metricValue: 95.5,
            metadata: JSON.stringify({ judge: 98, motion_type: 90, parties: 88 }),
          },
        },
        {
          cursor: 'c3',
          node: {
            id: '3',
            recordedAt: '2026-03-11T10:00:00Z',
            county: 'Los Angeles',
            metricName: 'scraper_last_success_age_hours',
            metricValue: 1.2,
            metadata: null,
          },
        },
      ],
      pageInfo: { hasNextPage: false, endCursor: null },
    },
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('CountyDetail', () => {
  let onBack: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    onBack = vi.fn();
    mockMetricsQueryResult = {
      data: undefined,
      loading: false,
      error: undefined,
    };
  });

  it('renders county name and health badge', () => {
    mockMetricsQueryResult.data = makeMetricsData();
    render(
      <CountyDetail county="Los Angeles" overview={makeOverview()} onBack={onBack} />,
    );
    expect(screen.getByText('Los Angeles')).toBeInTheDocument();
    expect(screen.getByText('GREEN')).toBeInTheDocument();
  });

  it('renders back button that calls onBack', () => {
    mockMetricsQueryResult.data = makeMetricsData();
    render(
      <CountyDetail county="Los Angeles" overview={makeOverview()} onBack={onBack} />,
    );
    fireEvent.click(screen.getByText('Back to overview'));
    expect(onBack).toHaveBeenCalled();
  });

  it('renders current metrics summary', () => {
    mockMetricsQueryResult.data = makeMetricsData();
    render(
      <CountyDetail county="Los Angeles" overview={makeOverview()} onBack={onBack} />,
    );
    expect(screen.getByText('42')).toBeInTheDocument();
    expect(screen.getByText('96%')).toBeInTheDocument();
    expect(screen.getByText('1h')).toBeInTheDocument();
  });

  it('renders em-dash for null metric values', () => {
    const overview: DataQualityOverviewItem = {
      ...makeOverview(),
      rulingCount24h: null,
      fieldCompletenessPct: null,
      scraperLastSuccessAgeHours: null,
    };
    mockMetricsQueryResult.data = {
      dataQualityMetrics: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } },
    };
    render(
      <CountyDetail county="Los Angeles" overview={overview} onBack={onBack} />,
    );
    // Should show em-dash for each null metric
    const dashes = screen.getAllByText('\u2014');
    expect(dashes.length).toBe(3);
  });

  it('shows loading skeleton while fetching metrics', () => {
    mockMetricsQueryResult.loading = true;
    const { container } = render(
      <CountyDetail county="Los Angeles" overview={makeOverview()} onBack={onBack} />,
    );
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('shows error message when metrics query fails', () => {
    mockMetricsQueryResult.error = new Error('Network error');
    render(
      <CountyDetail county="Los Angeles" overview={makeOverview()} onBack={onBack} />,
    );
    expect(
      screen.getByText('Failed to load metrics. Please try again.'),
    ).toBeInTheDocument();
  });

  it('renders chart titles when data is loaded', () => {
    mockMetricsQueryResult.data = makeMetricsData();
    render(
      <CountyDetail county="Los Angeles" overview={makeOverview()} onBack={onBack} />,
    );
    expect(screen.getByText('Ruling Ingest Rate')).toBeInTheDocument();
    // "Field Completeness" appears as both a summary card label and chart title
    const completenessElements = screen.getAllByText('Field Completeness');
    expect(completenessElements.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Scraper Uptime (Hours Since Last Success)')).toBeInTheDocument();
  });

  it('renders field-level completeness breakdown from metadata', () => {
    mockMetricsQueryResult.data = makeMetricsData();
    render(
      <CountyDetail county="Los Angeles" overview={makeOverview()} onBack={onBack} />,
    );
    expect(screen.getByText('Field-Level Completeness')).toBeInTheDocument();
    expect(screen.getByText('judge')).toBeInTheDocument();
    expect(screen.getByText('motion type')).toBeInTheDocument();
    expect(screen.getByText('parties')).toBeInTheDocument();
  });

  it('renders date range selector buttons', () => {
    mockMetricsQueryResult.data = makeMetricsData();
    render(
      <CountyDetail county="Los Angeles" overview={makeOverview()} onBack={onBack} />,
    );
    expect(screen.getByText('7d')).toBeInTheDocument();
    expect(screen.getByText('14d')).toBeInTheDocument();
    expect(screen.getByText('30d')).toBeInTheDocument();
    expect(screen.getByText('90d')).toBeInTheDocument();
  });
});
