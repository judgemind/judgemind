'use client';

import { useQuery, gql } from '@apollo/client';
import Link from 'next/link';
import {
  formatDate,
  formatLabel,
  formatMotionType,
  formatOutcome,
} from '../../../lib/display-helpers';

// ---------------------------------------------------------------------------
// GraphQL queries
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
// Types
// ---------------------------------------------------------------------------

interface MotionStats {
  motionType: string;
  total: number;
  granted: number;
  denied: number;
  grantedInPart: number;
  other: number;
  grantRate: number;
}

interface OutcomeCount {
  outcome: string;
  count: number;
}

interface AnalyticsData {
  judgeAnalytics: {
    judgeId: string;
    totalRulings: number;
    rulingsByOutcome: OutcomeCount[];
    rulingsByMotionType: MotionStats[];
    earliestRuling: string | null;
    latestRuling: string | null;
  } | null;
}

interface RulingNode {
  id: string;
  hearingDate: string;
  motionType: string | null;
  outcome: string | null;
  case: {
    id: string;
    caseNumber: string;
    caseTitle: string | null;
  } | null;
}

interface RulingsData {
  rulings: {
    edges: Array<{ cursor: string; node: RulingNode }>;
    pageInfo: { hasNextPage: boolean; endCursor: string | null };
  };
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const OUTCOME_BADGE: Record<string, string> = {
  granted: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  denied: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
  granted_in_part:
    'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
  denied_in_part:
    'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
};

const PAGE_SIZE = 20;

// ---------------------------------------------------------------------------
// Skeleton components
// ---------------------------------------------------------------------------

function AnalyticsSkeleton() {
  return (
    <div className="animate-pulse space-y-4" data-testid="analytics-skeleton">
      <div className="h-8 w-32 rounded bg-slate-200 dark:bg-slate-700" />
      <div className="h-4 w-64 rounded bg-slate-200 dark:bg-slate-700" />
      <div className="mt-4 space-y-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-6 w-full rounded bg-slate-200 dark:bg-slate-700" />
        ))}
      </div>
    </div>
  );
}

function RulingsSkeleton() {
  return (
    <div className="animate-pulse space-y-2" data-testid="rulings-skeleton">
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="flex gap-4 border-b border-slate-100 px-4 py-3 dark:border-slate-700">
          <div className="h-4 w-20 rounded bg-slate-200 dark:bg-slate-700" />
          <div className="h-4 w-32 rounded bg-slate-200 dark:bg-slate-700" />
          <div className="h-4 w-24 rounded bg-slate-200 dark:bg-slate-700" />
          <div className="h-4 w-16 rounded bg-slate-200 dark:bg-slate-700" />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helper: compute overall grant rate from outcome counts
// ---------------------------------------------------------------------------

function computeOverallGrantRate(
  rulingsByOutcome: OutcomeCount[],
): number | null {
  let granted = 0;
  let denied = 0;
  let partial = 0;
  for (const { outcome, count } of rulingsByOutcome) {
    if (outcome === 'granted') granted += count;
    else if (outcome === 'denied') denied += count;
    else if (outcome === 'granted_in_part' || outcome === 'denied_in_part')
      partial += count;
  }
  const denominator = granted + denied + partial;
  if (denominator === 0) return null;
  return granted / denominator;
}

// ---------------------------------------------------------------------------
// Helper: format date range string
// ---------------------------------------------------------------------------

function formatDateRange(earliest: string | null, latest: string | null): string {
  if (!earliest || !latest) return '';
  return `${formatDate(earliest)} \u2013 ${formatDate(latest)}`;
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function JudgeProfile({ judgeId }: { judgeId: string }) {
  const {
    data: analyticsData,
    loading: analyticsLoading,
    error: analyticsError,
  } = useQuery<AnalyticsData>(JUDGE_ANALYTICS_QUERY, {
    variables: { judgeId },
  });

  const {
    data: rulingsData,
    loading: rulingsLoading,
    error: rulingsError,
    fetchMore,
  } = useQuery<RulingsData>(JUDGE_RULINGS_QUERY, {
    variables: { judgeId, first: PAGE_SIZE },
    notifyOnNetworkStatusChange: true,
  });

  const analytics = analyticsData?.judgeAnalytics;
  const edges = rulingsData?.rulings.edges ?? [];
  const pageInfo = rulingsData?.rulings.pageInfo;

  function handleLoadMore() {
    if (!pageInfo?.endCursor) return;
    fetchMore({
      variables: { after: pageInfo.endCursor },
      updateQuery(prev, { fetchMoreResult }) {
        if (!fetchMoreResult) return prev;
        return {
          rulings: {
            ...fetchMoreResult.rulings,
            edges: [...prev.rulings.edges, ...fetchMoreResult.rulings.edges],
          },
        };
      },
    });
  }

  // -------------------------------------------------------------------------
  // Analytics section
  // -------------------------------------------------------------------------

  function renderAnalytics() {
    if (analyticsLoading) return <AnalyticsSkeleton />;

    if (analyticsError) {
      return (
        <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-center dark:border-red-800 dark:bg-red-900/20">
          <p className="text-sm text-red-600 dark:text-red-400">
            Failed to load analytics. Please try again.
          </p>
        </div>
      );
    }

    if (!analytics || analytics.totalRulings === 0) {
      return (
        <div className="rounded-lg border border-slate-200 p-6 text-center dark:border-slate-700">
          <p className="text-sm text-slate-500 dark:text-slate-400">
            No rulings captured for this judge yet. Check back after the next scrape.
          </p>
        </div>
      );
    }

    const overallGrantRate = computeOverallGrantRate(analytics.rulingsByOutcome);
    const dateRange = formatDateRange(analytics.earliestRuling, analytics.latestRuling);

    return (
      <div>
        {/* Summary stats */}
        <div className="flex flex-wrap items-baseline gap-6">
          {overallGrantRate !== null && (
            <div>
              <p className="text-3xl font-bold text-slate-900 dark:text-slate-100">
                {Math.round(overallGrantRate * 100)}%
              </p>
              <p className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Overall Grant Rate
              </p>
            </div>
          )}
          <div>
            <p className="text-3xl font-bold text-slate-900 dark:text-slate-100">
              {analytics.totalRulings.toLocaleString()}
            </p>
            <p className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Total Rulings
            </p>
          </div>
        </div>
        {dateRange && (
          <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
            Based on {analytics.totalRulings.toLocaleString()} rulings from {dateRange}
          </p>
        )}

        {/* Motion type stats table */}
        {analytics.rulingsByMotionType.length > 0 && (
          <div className="mt-6 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400">
                  <th className="py-2 pr-4">Motion Type</th>
                  <th className="px-4 py-2 text-right">Total</th>
                  <th className="px-4 py-2 text-right">Granted</th>
                  <th className="px-4 py-2 text-right">Denied</th>
                  <th className="px-4 py-2 text-right">Partial</th>
                  <th className="px-4 py-2 text-right">Grant Rate</th>
                </tr>
              </thead>
              <tbody>
                {analytics.rulingsByMotionType.map((row) => (
                  <tr
                    key={row.motionType}
                    className="border-b border-slate-100 dark:border-slate-700"
                  >
                    <td className="py-2 pr-4 font-medium text-slate-900 dark:text-slate-100">
                      {formatMotionType(row.motionType)}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-700 dark:text-slate-300">
                      {row.total}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-700 dark:text-slate-300">
                      {row.granted}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-700 dark:text-slate-300">
                      {row.denied}
                    </td>
                    <td className="px-4 py-2 text-right text-slate-700 dark:text-slate-300">
                      {row.grantedInPart}
                    </td>
                    <td className="px-4 py-2 text-right font-medium text-slate-900 dark:text-slate-100">
                      {Math.round(row.grantRate * 100)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* No motion type data */}
        {analytics.rulingsByMotionType.length === 0 && (
          <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">
            Motion type breakdown is not yet available for this judge.
          </p>
        )}
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Rulings section
  // -------------------------------------------------------------------------

  function renderRulings() {
    return (
      <div className="rounded-lg border border-slate-200 dark:border-slate-700">
        {/* Header row */}
        <div className="hidden grid-cols-[6rem_1fr_8rem_6rem] gap-4 border-b border-slate-200 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400 sm:grid">
          <span>Date</span>
          <span>Case</span>
          <span>Motion</span>
          <span>Outcome</span>
        </div>

        {/* Skeleton */}
        {rulingsLoading && edges.length === 0 && <RulingsSkeleton />}

        {/* Error */}
        {rulingsError && (
          <p className="p-8 text-center text-sm text-red-500 dark:text-red-400">
            Failed to load rulings. Please try again.
          </p>
        )}

        {/* Empty state */}
        {!rulingsLoading && !rulingsError && edges.length === 0 && (
          <p className="p-8 text-center text-slate-400 dark:text-slate-500">
            No rulings captured for this judge yet. Check back after the next scrape.
          </p>
        )}

        {/* Rows */}
        {edges.map(({ node }) => (
          <div
            key={node.id}
            className="border-b border-slate-100 last:border-0 dark:border-slate-700"
          >
            <div className="grid grid-cols-1 gap-1 px-4 py-3 hover:bg-slate-50 dark:hover:bg-slate-800/50 sm:grid-cols-[6rem_1fr_8rem_6rem] sm:items-center sm:gap-4">
              {/* Date */}
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {formatDate(node.hearingDate)}
              </span>

              {/* Case */}
              <span className="text-sm text-slate-900 dark:text-slate-100">
                {node.case ? (
                  <Link
                    href={`/cases/${node.case.id}`}
                    className="hover:text-brand-600 dark:hover:text-brand-400"
                  >
                    {node.case.caseNumber}
                    {node.case.caseTitle && (
                      <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
                        {node.case.caseTitle}
                      </span>
                    )}
                  </Link>
                ) : (
                  '\u2014'
                )}
              </span>

              {/* Motion type */}
              <span className="text-sm text-slate-700 dark:text-slate-300">
                {formatLabel(node.motionType)}
              </span>

              {/* Outcome badge */}
              <span
                className={`inline-flex w-fit items-center rounded px-2 py-0.5 text-xs font-medium ${
                  node.outcome && OUTCOME_BADGE[node.outcome]
                    ? OUTCOME_BADGE[node.outcome]
                    : 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300'
                }`}
              >
                {formatOutcome(node.outcome)}
              </span>
            </div>
          </div>
        ))}

        {/* Load more */}
        {pageInfo?.hasNextPage && (
          <div className="flex justify-center py-4">
            <button
              onClick={handleLoadMore}
              disabled={rulingsLoading}
              className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
            >
              {rulingsLoading ? 'Loading\u2026' : 'Load more'}
            </button>
          </div>
        )}
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div>
      {/* Analytics section */}
      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Analytics
        </h2>
        <div className="mt-3">{renderAnalytics()}</div>
      </section>

      {/* Recent rulings section */}
      <section className="mt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Recent Rulings
        </h2>
        <div className="mt-3">{renderRulings()}</div>
      </section>
    </div>
  );
}
