import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

let mockAuthResult: {
  user: { id: string; email: string; emailVerified: boolean; displayName: string | null; role: string; createdAt: string } | null;
  loading: boolean;
  setUser: ReturnType<typeof vi.fn>;
  logout: ReturnType<typeof vi.fn>;
};

let mockOverviewQueryResult: {
  data: unknown;
  loading: boolean;
  error: Error | undefined;
};

let mockMetricsQueryResult: {
  data: unknown;
  loading: boolean;
  error: Error | undefined;
};

vi.mock('@/providers/AuthProvider', () => ({
  useAuth: () => mockAuthResult,
}));

// Track which query is being called
let queryCallCount = 0;
vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    useQuery: () => {
      queryCallCount++;
      // First useQuery call is overview, second is metrics
      if (queryCallCount % 2 === 1) return mockOverviewQueryResult;
      return mockMetricsQueryResult;
    },
  };
});

// Mock recharts to avoid SVG rendering in tests
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

import { DataQualityDashboard } from '../src/app/(main)/admin/data-quality/DataQualityDashboard';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeAdminUser() {
  return {
    id: 'user-1',
    email: 'admin@example.com',
    emailVerified: true,
    displayName: 'Admin',
    role: 'admin',
    createdAt: '2026-01-01',
  };
}

function makeOverviewData() {
  return {
    dataQualityOverview: [
      {
        county: 'Los Angeles',
        healthStatus: 'green',
        rulingCount24h: 42,
        fieldCompletenessPct: 95.5,
        scraperLastSuccessAgeHours: 1.2,
        lastUpdated: '2026-03-11T10:00:00Z',
      },
      {
        county: 'Orange',
        healthStatus: 'yellow',
        rulingCount24h: 10,
        fieldCompletenessPct: 82.0,
        scraperLastSuccessAgeHours: 8.5,
        lastUpdated: '2026-03-11T09:00:00Z',
      },
      {
        county: 'San Diego',
        healthStatus: 'red',
        rulingCount24h: 0,
        fieldCompletenessPct: 45.0,
        scraperLastSuccessAgeHours: 30.0,
        lastUpdated: '2026-03-10T12:00:00Z',
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('DataQualityDashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryCallCount = 0;
    mockAuthResult = {
      user: makeAdminUser(),
      loading: false,
      setUser: vi.fn(),
      logout: vi.fn(),
    };
    mockOverviewQueryResult = {
      data: undefined,
      loading: false,
      error: undefined,
    };
    mockMetricsQueryResult = {
      data: undefined,
      loading: false,
      error: undefined,
    };
  });

  it('shows skeleton while auth is loading', () => {
    mockAuthResult.loading = true;
    const { container } = render(<DataQualityDashboard />);
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('shows access denied for non-admin users', () => {
    mockAuthResult.user = { ...makeAdminUser(), role: 'user' };
    render(<DataQualityDashboard />);
    expect(screen.getByText('Access Denied')).toBeInTheDocument();
    expect(screen.getByText(/restricted to admin users/)).toBeInTheDocument();
  });

  it('shows access denied when not authenticated', () => {
    mockAuthResult.user = null;
    render(<DataQualityDashboard />);
    expect(screen.getByText('Access Denied')).toBeInTheDocument();
  });

  it('shows skeleton while overview is loading', () => {
    mockOverviewQueryResult.loading = true;
    const { container } = render(<DataQualityDashboard />);
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('shows error message when overview query fails', () => {
    mockOverviewQueryResult.error = new Error('Network error');
    render(<DataQualityDashboard />);
    expect(
      screen.getByText('Failed to load data quality metrics. Please try again.'),
    ).toBeInTheDocument();
  });

  it('renders overview grid with county data', () => {
    mockOverviewQueryResult.data = makeOverviewData();
    mockMetricsQueryResult.data = {
      dataQualityMetrics: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } },
    };
    render(<DataQualityDashboard />);
    expect(screen.getByText('Los Angeles')).toBeInTheDocument();
    expect(screen.getByText('Orange')).toBeInTheDocument();
    expect(screen.getByText('San Diego')).toBeInTheDocument();
  });

  it('renders system health score', () => {
    mockOverviewQueryResult.data = makeOverviewData();
    mockMetricsQueryResult.data = {
      dataQualityMetrics: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } },
    };
    render(<DataQualityDashboard />);
    // 1 of 3 counties is green = 33%
    expect(screen.getByText('33%')).toBeInTheDocument();
    expect(screen.getByText(/1 of 3 counties healthy/)).toBeInTheDocument();
  });

  it('renders active alerts count', () => {
    mockOverviewQueryResult.data = makeOverviewData();
    mockMetricsQueryResult.data = {
      dataQualityMetrics: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } },
    };
    render(<DataQualityDashboard />);
    // 1 county in red status
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText(/counties in red status/)).toBeInTheDocument();
  });

  it('renders chart titles when metrics data is loaded', () => {
    mockOverviewQueryResult.data = makeOverviewData();
    mockMetricsQueryResult.data = {
      dataQualityMetrics: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } },
    };
    render(<DataQualityDashboard />);
    expect(screen.getByText('Ruling Ingest Rate (7 days)')).toBeInTheDocument();
    expect(screen.getByText('Field Completeness Trends (7 days)')).toBeInTheDocument();
    expect(screen.getByText('Scraper Uptime (7 days)')).toBeInTheDocument();
  });
});
