import { describe, it, expect, vi, beforeEach } from 'vitest';
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

  it('renders load more button when hasNextPage is true', async () => {
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
      expect(screen.getByText('Load more')).toBeInTheDocument();
    });
  });

  it('does not render load more button when hasNextPage is false', async () => {
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
});
