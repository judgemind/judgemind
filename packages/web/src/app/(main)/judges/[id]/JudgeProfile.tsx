'use client';

import { useQuery, gql } from '@apollo/client';
import Link from 'next/link';
import { BarChart3, Scale } from 'lucide-react';
import {
  formatDate,
  formatLabel,
  formatMotionType,
  formatOutcome,
  getOutcomeBadgeVariant,
  getOutcomeBadgeListClass,
} from '@/lib/display-helpers';
import { SECTION_HEADING, SECTION_LABEL } from '@/lib/typography';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

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


const PAGE_SIZE = 20;

// ---------------------------------------------------------------------------
// Skeleton components
// ---------------------------------------------------------------------------

function AnalyticsSkeleton() {
  return (
    <div className="grid gap-4 sm:grid-cols-3" data-testid="analytics-skeleton">
      {Array.from({ length: 3 }).map((_, i) => (
        <Card key={i}>
          <CardContent className="p-6">
            <Skeleton className="mb-2 h-8 w-20" />
            <Skeleton className="h-4 w-28" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function RulingsSkeleton() {
  return (
    <div data-testid="rulings-skeleton">
      <div className="divide-y rounded-lg border">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="px-4 py-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1 space-y-2">
                <Skeleton className="h-4 w-48" />
                <Skeleton className="h-3 w-32" />
              </div>
              <Skeleton className="h-5 w-16 rounded-full" />
            </div>
          </div>
        ))}
      </div>
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

  // Coordinate loading states: only show empty messages when both queries
  // have completed. If one returns empty while the other is still loading,
  // show a skeleton instead of the contradictory "No rulings captured" message.
  const bothLoaded = !analyticsLoading && !rulingsLoading;

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
        <Card className="border-destructive">
          <CardContent className="py-6 text-center">
            <p className="text-sm text-destructive">
              Failed to load analytics. Please try again.
            </p>
          </CardContent>
        </Card>
      );
    }

    if (!analytics || analytics.totalRulings === 0) {
      // If the other query is still loading, show skeleton instead of
      // the empty message to avoid contradictory UI states.
      if (!bothLoaded) return <AnalyticsSkeleton />;
      return (
        <p className="py-8 text-center text-sm text-muted-foreground">
          No rulings captured for this judge yet. Check back after the next scrape.
        </p>
      );
    }

    const overallGrantRate = computeOverallGrantRate(analytics.rulingsByOutcome);
    const dateRange = formatDateRange(analytics.earliestRuling, analytics.latestRuling);

    return (
      <div className="space-y-6">
        {/* Stats cards */}
        <div className="grid gap-4 sm:grid-cols-3">
          {overallGrantRate !== null && (
            <Card>
              <CardContent className="p-6">
                <p className="text-3xl font-bold text-foreground">
                  {Math.round(overallGrantRate * 100)}%
                </p>
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Overall Grant Rate
                </p>
              </CardContent>
            </Card>
          )}
          <Card>
            <CardContent className="p-6">
              <p className="text-3xl font-bold text-foreground">
                {analytics.totalRulings.toLocaleString()}
              </p>
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Total Rulings
              </p>
            </CardContent>
          </Card>
          {dateRange && (
            <Card>
              <CardContent className="p-6">
                <p className={SECTION_HEADING}>
                  {dateRange}
                </p>
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Date Range
                </p>
              </CardContent>
            </Card>
          )}
        </div>

        {/* Motion type stats table */}
        {analytics.rulingsByMotionType.length > 0 && (
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-base">
                <Scale className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                Motion Type Breakdown
              </CardTitle>
            </CardHeader>
            <CardContent className="pt-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Motion Type</TableHead>
                    <TableHead className="text-right">Total</TableHead>
                    <TableHead className="text-right">Granted</TableHead>
                    <TableHead className="text-right">Denied</TableHead>
                    <TableHead className="text-right">Partial</TableHead>
                    <TableHead className="text-right">Grant Rate</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {analytics.rulingsByMotionType.map((row) => (
                    <TableRow key={row.motionType}>
                      <TableCell className="font-medium">
                        {formatMotionType(row.motionType)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{row.total}</TableCell>
                      <TableCell className="text-right tabular-nums">{row.granted}</TableCell>
                      <TableCell className="text-right tabular-nums">{row.denied}</TableCell>
                      <TableCell className="text-right tabular-nums">{row.grantedInPart}</TableCell>
                      <TableCell className="text-right tabular-nums font-medium">
                        {Math.round(row.grantRate * 100)}%
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        )}

        {/* No motion type data */}
        {analytics.rulingsByMotionType.length === 0 && (
          <p className="text-sm text-muted-foreground">
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
    if (rulingsLoading && edges.length === 0) return <RulingsSkeleton />;

    if (rulingsError) {
      return (
        <Card className="border-destructive">
          <CardContent className="py-6 text-center">
            <p className="text-sm text-destructive">
              Failed to load rulings. Please try again.
            </p>
          </CardContent>
        </Card>
      );
    }

    if (!rulingsLoading && edges.length === 0) {
      // If the other query is still loading, show skeleton instead of
      // the empty message to avoid contradictory UI states.
      if (!bothLoaded) return <RulingsSkeleton />;
      return (
        <p className="py-8 text-center text-sm text-muted-foreground">
          No rulings captured for this judge yet. Check back after the next scrape.
        </p>
      );
    }

    return (
      <div>
        <div className="divide-y rounded-lg border">
          {edges.map(({ node }) => (
            <div key={node.id} className="px-4 py-3 transition-colors hover:bg-accent/50">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  {node.case ? (
                    <Link
                      href={`/cases/${node.case.id}`}
                      className="rounded-sm hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <p className="truncate text-sm font-medium text-foreground">
                        {node.case.caseNumber}
                        {node.case.caseTitle && (
                          <span className="ml-2 font-normal text-muted-foreground">
                            {node.case.caseTitle}
                          </span>
                        )}
                      </p>
                    </Link>
                  ) : (
                    <p className="text-sm text-muted-foreground">{'\u2014'}</p>
                  )}
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    <span>{formatDate(node.hearingDate)}</span>
                    {node.motionType && (
                      <>
                        <span>&middot;</span>
                        <span>{formatLabel(node.motionType)}</span>
                      </>
                    )}
                  </div>
                </div>
                <Badge
                  variant={getOutcomeBadgeVariant(node.outcome)}
                  className={getOutcomeBadgeListClass(node.outcome)}
                >
                  {formatOutcome(node.outcome)}
                </Badge>
              </div>
            </div>
          ))}
        </div>

        {/* Load more */}
        {pageInfo?.hasNextPage && (
          <div className="flex justify-center pt-2">
            <Button
              variant="outline"
              onClick={handleLoadMore}
              disabled={rulingsLoading}
            >
              {rulingsLoading ? 'Loading\u2026' : 'Load more'}
            </Button>
          </div>
        )}
      </div>
    );
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Analytics section */}
      <section>
        <h2 className={`mb-3 flex items-center gap-2 ${SECTION_LABEL}`}>
          <BarChart3 className="h-4 w-4" aria-hidden="true" />
          Analytics
        </h2>
        {renderAnalytics()}
      </section>

      {/* Recent rulings section */}
      <section>
        <h2 className={`mb-3 ${SECTION_LABEL}`}>
          Recent Rulings
        </h2>
        {renderRulings()}
      </section>
    </div>
  );
}
