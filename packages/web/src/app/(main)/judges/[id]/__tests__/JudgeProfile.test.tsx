import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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

// Mock next/navigation — useSearchParams, useRouter, usePathname
const mockReplace = vi.fn();
let mockSearchParams = new URLSearchParams();

vi.mock('next/navigation', () => ({
  useSearchParams: () => mockSearchParams,
  useRouter: () => ({ replace: mockReplace, push: vi.fn(), back: vi.fn() }),
  usePathname: () => '/judges/test-id',
}));

// Mock lucide-react icons
vi.mock('lucide-react', () => ({
  AlertCircle: ({ className }: { className?: string }) => (
    <span data-testid="alert-circle-icon" className={className} />
  ),
  BarChart3: ({ className }: { className?: string }) => (
    <span data-testid="bar-chart-icon" className={className} />
  ),
  Scale: ({ className }: { className?: string }) => (
    <span data-testid="scale-icon" className={className} />
  ),
  X: ({ className }: { className?: string }) => (
    <span data-testid="x-icon" className={className} aria-hidden="true" />
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

  // Mock scrollIntoView for jsdom (not implemented in jsdom)
  Element.prototype.scrollIntoView = vi.fn();
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
  query JudgeRulings($judgeId: ID!, $first: Int!, $after: String, $motionType: String) {
    rulings(judgeId: $judgeId, first: $first, after: $after, motionType: $motionType) {
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
    motionType: string;
  }> = {},
  options: { delay?: number } = {},
): MockedResponse {
  const variables: Record<string, unknown> = { judgeId, first: 20 };
  if (overrides.motionType) {
    variables.motionType = overrides.motionType;
  }
  return {
    request: {
      query: JUDGE_RULINGS_QUERY,
      variables,
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
    mockSearchParams = new URLSearchParams();
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

  it('renders analytics error state with soft styling', async () => {
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

    const { container } = render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-err" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('Failed to load analytics.')).toBeInTheDocument();
    });

    // Should NOT use destructive styling
    expect(container.querySelector('.border-destructive')).not.toBeInTheDocument();
    expect(container.querySelector('.text-destructive')).not.toBeInTheDocument();

    // Should use soft styling
    expect(container.querySelector('.bg-muted')).toBeInTheDocument();
    expect(container.querySelector('.text-red-700')).toBeInTheDocument();

    // Should have AlertCircle icon
    expect(screen.getByTestId('alert-circle-icon')).toBeInTheDocument();

    // Should have a Try again action
    expect(screen.getByText('Try again')).toBeInTheDocument();
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

  // Hover state tests (#1743)
  it('renders hover highlight on ruling rows using muted design token', async () => {
    const mocks = [
      buildAnalyticsMock('judge-hover'),
      buildRulingsMock('judge-hover'),
    ];

    const { container } = render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-hover" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('24STCV12345')).toBeInTheDocument();
    });

    const rows = container.querySelectorAll('.hover\\:bg-muted\\/50');
    expect(rows.length).toBeGreaterThanOrEqual(1);
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

  // -------------------------------------------------------------------------
  // Motion type filter tests (#1750)
  // -------------------------------------------------------------------------

  it('makes motion type table rows clickable with role=button', async () => {
    const mocks = [
      buildAnalyticsMock('judge-filter-1'),
      buildRulingsMock('judge-filter-1'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-filter-1" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('Motion Type Breakdown')).toBeInTheDocument();
    });

    // The row containing "MSJ" in the analytics table should have role=button
    // Use getAllByText since "MSJ" may also appear in the rulings list
    const msjCells = screen.getAllByText('MSJ');
    // The first one is in the analytics table cell
    const msjRow = msjCells[0].closest('tr');
    expect(msjRow).toHaveAttribute('role', 'button');
    expect(msjRow).toHaveAttribute('tabindex', '0');
  });

  it('shows filter pill when a motion type row is clicked', async () => {
    const user = userEvent.setup();
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-filter-2'),
      buildRulingsMock('judge-filter-2'),
      // Mock for filtered query when "demurrer" is clicked
      buildRulingsMock('judge-filter-2', {
        motionType: 'demurrer',
        edges: [
          {
            cursor: 'cf1',
            node: {
              id: 'rf1',
              hearingDate: '2026-02-15',
              motionType: 'demurrer',
              outcome: 'denied',
              case: {
                id: 'case-2',
                caseNumber: '24STCV67890',
                caseTitle: 'Filtered Case',
              },
            },
          },
        ],
      }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-filter-2" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('MSJ')).toBeInTheDocument();
    });

    // No filter pill should be visible initially
    expect(screen.queryByTestId('motion-type-filter-pill')).not.toBeInTheDocument();

    // Click on the "Demurrer" row in the analytics table
    const demurrerCells = screen.getAllByText('Demurrer');
    const demurrerRow = demurrerCells[0].closest('tr');
    expect(demurrerRow).not.toBeNull();
    await user.click(demurrerRow!);

    // Filter pill should appear with the motion type label
    await waitFor(() => {
      expect(screen.getByTestId('motion-type-filter-pill')).toBeInTheDocument();
    });
    expect(screen.getByTestId('motion-type-filter-pill')).toHaveTextContent('Demurrer');
  });

  it('highlights active row in analytics table when filter is set', async () => {
    const user = userEvent.setup();
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-filter-3'),
      buildRulingsMock('judge-filter-3'),
      buildRulingsMock('judge-filter-3', {
        motionType: 'msj',
        edges: [
          {
            cursor: 'cf1',
            node: {
              id: 'rf1',
              hearingDate: '2026-03-01',
              motionType: 'msj',
              outcome: 'granted',
              case: {
                id: 'case-1',
                caseNumber: '24STCV12345',
                caseTitle: 'MSJ Case',
              },
            },
          },
        ],
      }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-filter-3" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('Motion Type Breakdown')).toBeInTheDocument();
    });

    // Click the "MSJ" row (first match is in the analytics table)
    const msjCells = screen.getAllByText('MSJ');
    const msjRow = msjCells[0].closest('tr');
    expect(msjRow).not.toBeNull();
    await user.click(msjRow!);

    // The clicked row should have aria-pressed=true
    await waitFor(() => {
      expect(msjRow).toHaveAttribute('aria-pressed', 'true');
    });

    // Other rows should have aria-pressed=false
    const demurrerCells = screen.getAllByText('Demurrer');
    // The Demurrer cell in the analytics table
    const demurrerRow = demurrerCells[0].closest('tr');
    expect(demurrerRow).toHaveAttribute('aria-pressed', 'false');
  });

  it('clears filter when clicking the same motion type row again (toggle)', async () => {
    const user = userEvent.setup();
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-filter-4'),
      buildRulingsMock('judge-filter-4'),
      // Mock for filtered query
      buildRulingsMock('judge-filter-4', {
        motionType: 'msj',
        edges: [
          {
            cursor: 'cf1',
            node: {
              id: 'rf1',
              hearingDate: '2026-03-01',
              motionType: 'msj',
              outcome: 'granted',
              case: {
                id: 'case-1',
                caseNumber: '24STCV12345',
                caseTitle: 'MSJ Case',
              },
            },
          },
        ],
      }),
      // Mock for unfiltered query when toggled back
      buildRulingsMock('judge-filter-4'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-filter-4" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('Motion Type Breakdown')).toBeInTheDocument();
    });

    // Use getAllByText since "MSJ" appears in both table and rulings
    const msjCells = screen.getAllByText('MSJ');
    const msjRow = msjCells[0].closest('tr');

    // Click to activate filter
    await user.click(msjRow!);
    await waitFor(() => {
      expect(screen.getByTestId('motion-type-filter-pill')).toBeInTheDocument();
    });

    // Click again to clear filter
    await user.click(msjRow!);
    await waitFor(() => {
      expect(screen.queryByTestId('motion-type-filter-pill')).not.toBeInTheDocument();
    });
  });

  it('clears filter when clicking the clear button in the filter pill', async () => {
    const user = userEvent.setup();
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-filter-5'),
      buildRulingsMock('judge-filter-5'),
      buildRulingsMock('judge-filter-5', {
        motionType: 'demurrer',
        edges: [
          {
            cursor: 'cf1',
            node: {
              id: 'rf1',
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
      }),
      // Mock for unfiltered query after clear
      buildRulingsMock('judge-filter-5'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-filter-5" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('MSJ')).toBeInTheDocument();
    });

    // Click "Demurrer" to activate filter
    const demurrerCells = screen.getAllByText('Demurrer');
    const demurrerRow = demurrerCells[0].closest('tr');
    await user.click(demurrerRow!);

    await waitFor(() => {
      expect(screen.getByTestId('motion-type-filter-pill')).toBeInTheDocument();
    });

    // Click the clear button (X) in the filter pill
    const clearButton = screen.getByLabelText('Clear Demurrer filter');
    await user.click(clearButton);

    await waitFor(() => {
      expect(screen.queryByTestId('motion-type-filter-pill')).not.toBeInTheDocument();
    });
  });

  it('shows filtered empty state with clear link when filter returns no results', async () => {
    const user = userEvent.setup();
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-filter-6'),
      buildRulingsMock('judge-filter-6'),
      // Filtered query returns empty
      buildRulingsMock('judge-filter-6', {
        motionType: 'demurrer',
        edges: [],
      }),
      // Unfiltered query after clear
      buildRulingsMock('judge-filter-6'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-filter-6" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('MSJ')).toBeInTheDocument();
    });

    // Click "Demurrer" row
    const demurrerCells = screen.getAllByText('Demurrer');
    const demurrerRow = demurrerCells[0].closest('tr');
    await user.click(demurrerRow!);

    // Should show filtered empty state
    await waitFor(() => {
      expect(screen.getByText(/No Demurrer rulings found/)).toBeInTheDocument();
    });

    // Should have a "Clear filter" link
    const clearLink = screen.getByText('Clear filter');
    expect(clearLink).toBeInTheDocument();

    // Click it to clear the filter
    await user.click(clearLink);

    // Filter pill should disappear
    await waitFor(() => {
      expect(screen.queryByTestId('motion-type-filter-pill')).not.toBeInTheDocument();
    });
  });

  it('supports keyboard activation with Enter key on motion type rows', async () => {
    const user = userEvent.setup();
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-filter-7'),
      buildRulingsMock('judge-filter-7'),
      buildRulingsMock('judge-filter-7', {
        motionType: 'msj',
        edges: [
          {
            cursor: 'cf1',
            node: {
              id: 'rf1',
              hearingDate: '2026-03-01',
              motionType: 'msj',
              outcome: 'granted',
              case: {
                id: 'case-1',
                caseNumber: '24STCV12345',
                caseTitle: 'MSJ Case',
              },
            },
          },
        ],
      }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-filter-7" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('Motion Type Breakdown')).toBeInTheDocument();
    });

    const msjCells = screen.getAllByText('MSJ');
    const msjRow = msjCells[0].closest('tr');
    expect(msjRow).not.toBeNull();

    // Focus the row and press Enter
    msjRow!.focus();
    await user.keyboard('{Enter}');

    // Filter should be applied
    await waitFor(() => {
      expect(screen.getByTestId('motion-type-filter-pill')).toBeInTheDocument();
    });
    expect(screen.getByTestId('motion-type-filter-pill')).toHaveTextContent('MSJ');
  });

  // -------------------------------------------------------------------------
  // URL param sync tests (#2018)
  // -------------------------------------------------------------------------

  it('updates URL with ?motion=<type> when a motion type row is clicked', async () => {
    const user = userEvent.setup();
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-url-1'),
      buildRulingsMock('judge-url-1'),
      buildRulingsMock('judge-url-1', {
        motionType: 'demurrer',
        edges: [
          {
            cursor: 'cf1',
            node: {
              id: 'rf1',
              hearingDate: '2026-02-15',
              motionType: 'demurrer',
              outcome: 'denied',
              case: {
                id: 'case-2',
                caseNumber: '24STCV67890',
                caseTitle: 'URL Test Case',
              },
            },
          },
        ],
      }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-url-1" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('Motion Type Breakdown')).toBeInTheDocument();
    });

    // Click the "Demurrer" row
    const demurrerCells = screen.getAllByText('Demurrer');
    const demurrerRow = demurrerCells[0].closest('tr');
    await user.click(demurrerRow!);

    // URL should be updated with motion param
    await waitFor(() => {
      expect(mockReplace).toHaveBeenCalledWith(
        '/judges/test-id?motion=demurrer',
      );
    });
  });

  it('pre-selects filter when ?motion= param is present in URL on load', async () => {
    mockSearchParams = new URLSearchParams('motion=demurrer');
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-url-2'),
      buildRulingsMock('judge-url-2', {
        motionType: 'demurrer',
        edges: [
          {
            cursor: 'cf1',
            node: {
              id: 'rf1',
              hearingDate: '2026-02-15',
              motionType: 'demurrer',
              outcome: 'denied',
              case: {
                id: 'case-2',
                caseNumber: '24STCV67890',
                caseTitle: 'Preselected Case',
              },
            },
          },
        ],
      }),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-url-2" />
      </MockedProvider>,
    );

    // The filter pill should appear immediately (pre-selected from URL)
    await waitFor(() => {
      expect(screen.getByTestId('motion-type-filter-pill')).toBeInTheDocument();
    });
    expect(screen.getByTestId('motion-type-filter-pill')).toHaveTextContent('Demurrer');
  });

  it('removes the motion param from URL when filter is cleared', async () => {
    mockSearchParams = new URLSearchParams('motion=demurrer');
    const user = userEvent.setup();
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-url-3'),
      buildRulingsMock('judge-url-3', {
        motionType: 'demurrer',
        edges: [
          {
            cursor: 'cf1',
            node: {
              id: 'rf1',
              hearingDate: '2026-02-15',
              motionType: 'demurrer',
              outcome: 'denied',
              case: {
                id: 'case-2',
                caseNumber: '24STCV67890',
                caseTitle: 'Clear Test Case',
              },
            },
          },
        ],
      }),
      // Mock for unfiltered query after clear
      buildRulingsMock('judge-url-3'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-url-3" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('motion-type-filter-pill')).toBeInTheDocument();
    });

    // Click the clear button in the filter pill
    const clearButton = screen.getByLabelText('Clear Demurrer filter');
    await user.click(clearButton);

    // URL should be updated to remove the motion param
    await waitFor(() => {
      expect(mockReplace).toHaveBeenCalledWith('/judges/test-id');
    });
  });

  it('removes motion param from URL when toggling the same row off', async () => {
    const user = userEvent.setup();
    const mocks: MockedResponse[] = [
      buildAnalyticsMock('judge-url-4'),
      buildRulingsMock('judge-url-4'),
      buildRulingsMock('judge-url-4', {
        motionType: 'msj',
        edges: [
          {
            cursor: 'cf1',
            node: {
              id: 'rf1',
              hearingDate: '2026-03-01',
              motionType: 'msj',
              outcome: 'granted',
              case: {
                id: 'case-1',
                caseNumber: '24STCV12345',
                caseTitle: 'Toggle Test',
              },
            },
          },
        ],
      }),
      buildRulingsMock('judge-url-4'),
    ];

    render(
      <MockedProvider mocks={mocks}>
        <JudgeProfile judgeId="judge-url-4" />
      </MockedProvider>,
    );

    await waitFor(() => {
      expect(screen.getByText('Motion Type Breakdown')).toBeInTheDocument();
    });

    const msjCells = screen.getAllByText('MSJ');
    const msjRow = msjCells[0].closest('tr');

    // Click to activate
    await user.click(msjRow!);
    await waitFor(() => {
      expect(mockReplace).toHaveBeenCalledWith('/judges/test-id?motion=msj');
    });

    mockReplace.mockClear();

    // Click again to deactivate
    await user.click(msjRow!);
    await waitFor(() => {
      expect(mockReplace).toHaveBeenCalledWith('/judges/test-id');
    });
  });
});
