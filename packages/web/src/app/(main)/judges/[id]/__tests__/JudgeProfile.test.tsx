import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MockedProvider, MockedResponse } from '@apollo/client/testing';
import { gql } from '@apollo/client';
import { JudgeProfile } from '../JudgeProfile';

// ---------------------------------------------------------------------------
// Mock next/link — render as an anchor tag
// ---------------------------------------------------------------------------

vi.mock('next/link', () => ({
  default: ({ href, children, ...props }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

// Mock lucide-react icons
vi.mock('lucide-react', () => ({
  BarChart3: ({ className }: { className?: string }) => (
    <span data-testid="bar-chart-icon" className={className} />
  ),
  Scale: ({ className }: { className?: string }) => (
    <span data-testid="scale-icon" className={className} />
  ),
}));

// IntersectionObserver mock
let mockObserve: ReturnType<typeof vi.fn>;
let mockDisconnect: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockObserve = vi.fn();
  mockDisconnect = vi.fn();

  vi.stubGlobal(
    'IntersectionObserver',
    vi.fn((callback: IntersectionObserverCallback) => {
      // Store callback for potential use in tests
      (globalThis as Record<string, unknown>).__intersectionCallback = callback;
      return {
        observe: mockObserve,
        disconnect: mockDisconnect,
        unobserve: vi.fn(),
      };
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// GraphQL queries (must match the component's queries exactly)
// ---------------------------------------------------------------------------

const JUDGE_ANALYTICS_QUERY = gql`
  query JudgeAnalytics($judgeId: ID!) {
    judgeAnalytics(judgeId: $judgeId) {
      judgeId
      totalRulings
      rulingsByOutcome {
        outcome
        count
      }
      rulingsByMotionType {
        motionType
        total
        granted
        denied
        grantedInPart
        other
        grantRate
      }
      earliestRuling
      latestRuling
    }
  }
`;

const JUDGE_RULINGS_QUERY = gql`
  query JudgeRulings($judgeId: ID!, $first: Int!, $after: String) {
    rulings(judgeId: $judgeId, first: $first, after: $after) {
      edges {
        cursor
        node {
          id
          hearingDate
          motionType
          outcome
          case {
            id
            caseNumber
            caseTitle
          }
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
`;

// ---------------------------------------------------------------------------
// Test data factories
// ---------------------------------------------------------------------------

function buildAnalyticsMock(
  judgeId: string,
  overrides: Partial<{
    totalRulings: number;
    rulingsByOutcome: Array<{ outcome: string; count: number }>;
    rulingsByMotionType: Array<{
      motionType: string;
      total: number;
      granted: number;
      denied: number;
      grantedInPart: number;
      other: number;
      grantRate: number;
    }>;
    earliestRuling: string | null;
    latestRuling: string | null;
  }> = {},
  options: { delay?: number } = {},
): MockedResponse {
  return {
    request: {
      query: JUDGE_ANALYTICS_QUERY,
      variables: { judgeId },
    },
    result: {
      data: {
        judgeAnalytics: {
          judgeId,
          totalRulings: overrides.totalRulings ?? 100,
          rulingsByOutcome: overrides.rulingsByOutcome ?? [
            { outcome: 'granted', count: 40 },
            { outcome: 'denied', count: 50 },
            { outcome: 'granted_in_part', count: 10 },
          ],
          rulingsByMotionType: overrides.rulingsByMotionType ?? [
            {
              motionType: 'msj',
              total: 42,
              granted: 18,
              denied: 21,
              grantedInPart: 3,
              other: 0,
              grantRate: 0.4286,
            },
            {
              motionType: 'demurrer',
              total: 31,
              granted: 12,
              denied: 17,
              grantedInPart: 2,
              other: 0,
              grantRate: 0.3871,
            },
          ],
          earliestRuling: overrides.earliestRuling ?? '2024-01-15',
          latestRuling: overrides.latestRuling ?? '2026-03-01',
        },
      },
    },
    ...(options.delay !== undefined ? { delay: options.delay } : {}),
  };
}

function buildRulingsMock(
  judgeId: string,
  overrides: Partial<{
    edges: Array<{
      cursor: string;
      node: {
        id: string;
        hearingDate: string;
        motionType: string | null;
        outcome: string | null;
        case: { id: string; caseNumber: string; caseTitle: string | null } | null;
      };
    }>;
    hasNextPage: boolean;
    endCursor: string | null;
  }> = {},
  options: { delay?: number } = {},
): MockedResponse {
  return {
    request: {
      query: JUDGE_RULINGS_QUERY,
      variables: { judgeId, first: 20 },
    },
    result: {
      data: {
        rulings: {
          edges: overrides.edges ?? [
            {
              cursor: 'c1',
              node: {
                id: 'r1',
                hearingDate: '2026-03-01',
                motionType: 'msj',
                outcome: 'granted',
                case: {
                  id: 'case-1',
                  caseNumber: '24STCV12345',
                  caseTitle: 'Smith v. Jones',
                },
              },
            },
            {
              cursor: 'c2',
              node: {
                id: 'r2',
                hearingDate: '2026-02-15',
                motionType: 'demurrer',
                outcome: 'denied',
                case: {
                  id: 'case-2',
                  caseNumber: '24STCV67890',
                  caseTitle: null,
                },
              },
            },
          ],
          pageInfo: {
            hasNextPage: overrides.hasNextPage ?? false,
            endCursor: overrides.endCursor ?? null,
          },
        },
      },
    },
    ...(options.delay !== undefined ? { delay: options.delay } : {}),
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('JudgeProfile', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders analytics summary with grant rate and total rulings', async () => {
    const mocks = [
      buildAnalyticsMock('judge-1'),
      buildRulingsMock('judge-1'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-1" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('40%')).toBeInTheDocument();
    });

    expect(screen.getByText('100')).toBeInTheDocument();
    expect(screen.getByText('Overall Grant Rate')).toBeInTheDocument();
    expect(screen.getByText('Total Rulings')).toBeInTheDocument();
  });

  it('renders motion type stats table', async () => {
    const mocks = [
      buildAnalyticsMock('judge-1'),
      buildRulingsMock('judge-1'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-1" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('MSJ')).toBeInTheDocument();
    });

    expect(screen.getAllByText('Demurrer').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Motion Type')).toBeInTheDocument();
    expect(screen.getByText('Grant Rate')).toBeInTheDocument();
  });

  it('renders date range in stats card', async () => {
    const mocks = [
      buildAnalyticsMock('judge-1'),
      buildRulingsMock('judge-1'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-1" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('Date Range')).toBeInTheDocument();
    });
  });

  it('renders recent rulings with case links', async () => {
    const mocks = [
      buildAnalyticsMock('judge-1'),
      buildRulingsMock('judge-1'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-1" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('24STCV12345')).toBeInTheDocument();
    });

    const caseLink = screen.getByText('24STCV12345').closest('a');
    expect(caseLink).toHaveAttribute('href', '/cases/case-1');

    expect(screen.getAllByText('Granted').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Denied').length).toBeGreaterThanOrEqual(1);
  });

  it('renders empty state when judge has no rulings', async () => {
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-empty', { totalRulings: 0, rulingsByOutcome: [], rulingsByMotionType: [], earliestRuling: null, latestRuling: null }),
      buildRulingsMock('judge-empty', { edges: [] }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-empty" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getAllByText(/No rulings captured for this judge yet/)).toHaveLength(2);
    });
  });

  it('renders loading skeletons initially', () => {
    const mocks = [
      buildAnalyticsMock('judge-1'),
      buildRulingsMock('judge-1'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-1" />
      </MockedProvider>,
    );

    expect(screen.getByTestId('analytics-skeleton')).toBeInTheDocument();
    expect(screen.getByTestId('rulings-skeleton')).toBeInTheDocument();
  });

  it('renders analytics error state', async () => {
    const mocks: MockedResponse[] = [
      {
        request: {
          query: JUDGE_ANALYTICS_QUERY,
          variables: { judgeId: 'judge-err' },
        },
        error: new Error('Network error'),
      },
      buildRulingsMock('judge-err', { edges: [] }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-err" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('Failed to load analytics. Please try again.')).toBeInTheDocument();
    });
  });

  it('renders sentinel element when hasNextPage is true (infinite scroll)', async () => {
    const mocks = [
      buildAnalyticsMock('judge-pag'),
      buildRulingsMock('judge-pag', { hasNextPage: true, endCursor: 'cursor-1' }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-pag" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
    });

    // IntersectionObserver should be set up
    expect(IntersectionObserver).toHaveBeenCalledWith(
      expect.any(Function),
      { rootMargin: '200px' },
    );
    expect(mockObserve).toHaveBeenCalled();
  });

  it('does not render sentinel or Load more when hasNextPage is false', async () => {
    const mocks = [
      buildAnalyticsMock('judge-1'),
      buildRulingsMock('judge-1', { hasNextPage: false }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-1" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('24STCV12345')).toBeInTheDocument();
    });

    expect(screen.queryByText('Load more')).not.toBeInTheDocument();
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('does not render a "Load more" button (uses infinite scroll instead)', async () => {
    const mocks = [
      buildAnalyticsMock('judge-pag'),
      buildRulingsMock('judge-pag', { hasNextPage: true, endCursor: 'cursor-1' }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-pag" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
    });

    expect(screen.queryByText('Load more')).not.toBeInTheDocument();
  });

  it('calls fetchMore when sentinel becomes visible', async () => {
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-fetch'),
      buildRulingsMock('judge-fetch', { hasNextPage: true, endCursor: 'cursor-fetch' }),
      // fetchMore response for the second page
      {
        request: {
          query: JUDGE_RULINGS_QUERY,
          variables: { judgeId: 'judge-fetch', first: 20, after: 'cursor-fetch' },
        },
        result: {
          data: {
            rulings: {
              edges: [
                {
                  cursor: 'c3',
                  node: {
                    id: 'r3',
                    hearingDate: '2026-01-01',
                    motionType: 'msj',
                    outcome: 'granted',
                    case: {
                      id: 'case-3',
                      caseNumber: '24STCV99999',
                      caseTitle: 'Page Two Case',
                    },
                  },
                },
              ],
              pageInfo: {
                hasNextPage: false,
                endCursor: null,
              },
            },
          },
        },
      },
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-fetch" />
      </MockedProvider>,
    );

    // Wait for initial data to load and sentinel to appear
    await waitFor(() => {
      expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
    });

    // Simulate the sentinel becoming visible
    const callback = (globalThis as Record<string, unknown>).__intersectionCallback as IntersectionObserverCallback;
    callback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    // After fetchMore, the second page data should appear
    await waitFor(() => {
      expect(screen.getByText('24STCV99999')).toBeInTheDocument();
    });
  });

  it('disconnects observer on unmount', async () => {
    const mocks = [
      buildAnalyticsMock('judge-pag'),
      buildRulingsMock('judge-pag', { hasNextPage: true, endCursor: 'cursor-1' }),
    ];

    const { unmount } = render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-pag" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
    });

    unmount();
    expect(mockDisconnect).toHaveBeenCalled();
  });

  it('renders null analytics gracefully', async () => {
    const mocks: MockedResponse[] = [
      {
        request: {
          query: JUDGE_ANALYTICS_QUERY,
          variables: { judgeId: 'judge-null' },
        },
        result: {
          data: { judgeAnalytics: null },
        },
      },
      buildRulingsMock('judge-null', { edges: [] }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-null" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getAllByText(/No rulings captured for this judge yet/)).toHaveLength(2);
    });
  });

  it('shows em-dash for ruling with no case', async () => {
    const mocks = [
      buildAnalyticsMock('judge-sparse'),
      buildRulingsMock('judge-sparse', {
        edges: [
          {
            cursor: 'c1',
            node: {
              id: 'r-sparse',
              hearingDate: '2026-01-10',
              motionType: null,
              outcome: null,
              case: null,
            },
          },
        ],
      }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-sparse" />
      </MockedProvider>,
    );

    await waitFor(() => {
      const dashes = screen.getAllByText('\u2014');
      expect(dashes.length).toBeGreaterThanOrEqual(1);
    });
  });

  it('renders ruling cards with shadcn Card components', async () => {
    const mocks = [
      buildAnalyticsMock('judge-1'),
      buildRulingsMock('judge-1'),
    ];

    const { container } = render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-1" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('24STCV12345')).toBeInTheDocument();
    });

    const cards = container.querySelectorAll('.rounded-lg.border');
    expect(cards.length).toBeGreaterThan(0);
  });

  it('renders section headings', async () => {
    const mocks = [
      buildAnalyticsMock('judge-1'),
      buildRulingsMock('judge-1'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-1" />
      </MockedProvider>,
    );

    expect(screen.getByText('Analytics')).toBeInTheDocument();
    expect(screen.getByText('Recent Rulings')).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Coordinated loading state tests (#1668)
  // -------------------------------------------------------------------------

  it('shows analytics skeleton when analytics is empty but rulings still loading', async () => {
    // Analytics resolves immediately with empty data; rulings takes a long time
    const mocks: MockedResponse[] = [
      buildAnalyticsMock(
        'judge-coord-1',
        { totalRulings: 0, rulingsByOutcome: [], rulingsByMotionType: [], earliestRuling: null, latestRuling: null },
      ),
      buildRulingsMock('judge-coord-1', { edges: [] }, { delay: 1_000_000 }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-coord-1" />
      </MockedProvider>,
    );

    // Wait for analytics to resolve (instant) while rulings is still loading
    await waitFor(() => {
      // Analytics should show skeleton (not empty message) because rulings hasn't loaded
      const skeletons = screen.getAllByTestId('analytics-skeleton');
      expect(skeletons.length).toBeGreaterThanOrEqual(1);
    });

    // The "No rulings captured" message should NOT appear for the analytics section
    // (rulings section still has its own skeleton since it's loading)
    expect(screen.queryByText(/No rulings captured for this judge yet/)).not.toBeInTheDocument();
  });

  it('shows rulings skeleton when rulings is empty but analytics still loading', async () => {
    // Rulings resolves immediately with empty data; analytics takes a long time
    const mocks: MockedResponse[] = [
      buildAnalyticsMock(
        'judge-coord-2',
        { totalRulings: 0, rulingsByOutcome: [], rulingsByMotionType: [], earliestRuling: null, latestRuling: null },
        { delay: 1_000_000 },
      ),
      buildRulingsMock('judge-coord-2', { edges: [] }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-coord-2" />
      </MockedProvider>,
    );

    // Wait for rulings to resolve (instant) while analytics is still loading
    await waitFor(() => {
      // Rulings should show skeleton (not empty message) because analytics hasn't loaded
      const skeletons = screen.getAllByTestId('rulings-skeleton');
      expect(skeletons.length).toBeGreaterThanOrEqual(1);
    });

    // The "No rulings captured" message should NOT appear
    expect(screen.queryByText(/No rulings captured for this judge yet/)).not.toBeInTheDocument();
  });

  it('shows empty messages only when both queries complete with empty data', async () => {
    // Both resolve quickly with empty data
    const mocks: MockedResponse[] = [
      buildAnalyticsMock(
        'judge-coord-3',
        { totalRulings: 0, rulingsByOutcome: [], rulingsByMotionType: [], earliestRuling: null, latestRuling: null },
      ),
      buildRulingsMock('judge-coord-3', { edges: [] }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-coord-3" />
      </MockedProvider>,
    );

    // Wait for both to resolve, then both should show the empty message
    await waitFor(() => {
      expect(screen.getAllByText(/No rulings captured for this judge yet/)).toHaveLength(2);
    });

    // Skeletons should be gone
    expect(screen.queryByTestId('analytics-skeleton')).not.toBeInTheDocument();
    expect(screen.queryByTestId('rulings-skeleton')).not.toBeInTheDocument();
  });

  it('does not show empty message for analytics when analytics has data regardless of rulings loading', async () => {
    // Analytics has data, rulings takes forever
    const mocks = [
      buildAnalyticsMock('judge-coord-4'),
      buildRulingsMock('judge-coord-4', { edges: [] }, { delay: 1_000_000 }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-coord-4" />
      </MockedProvider>,
    );

    // Analytics should show real data (not empty message or skeleton)
    await waitFor(() => {
      expect(screen.getByText('100')).toBeInTheDocument();
    });

    // No empty message should be visible
    expect(screen.queryByText(/No rulings captured for this judge yet/)).not.toBeInTheDocument();
  });
});
