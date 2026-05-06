'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery, gql } from '@apollo/client';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { BarChart3, ChevronRight, Scale, X } from 'lucide-react';
import { ErrorBanner } from '@/components/ErrorBanner';
import {
  formatDate,
  formatLabel,
  formatMotionType,
  computeOverallGrantRate,
  formatDateRange,
} from '@/lib/display-helpers';
import { InfiniteScrollTrigger } from '@/components/InfiniteScrollTrigger';
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll';
import { OutcomeBadge } from '@/components/OutcomeBadge';
import { SECTION_HEADING, SECTION_LABEL } from '@/lib/typography';
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

/** Minimum number of rulings before stats are considered high-confidence. */
const LOW_SAMPLE_THRESHOLD = 10;

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
// Main component
// ---------------------------------------------------------------------------

export function JudgeProfile({ judgeId }: { judgeId: string }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [motionTypeFilter, setMotionTypeFilter] = useState<string | null>(
    searchParams.get('motion'),
  );
  const rulingsSectionRef = useRef<HTMLElement>(null);

  // Sync state from URL when search params change (e.g. browser back/forward)
  useEffect(() => {
    setMotionTypeFilter(searchParams.get('motion'));
  }, [searchParams]);

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
    variables: {
      judgeId,
      first: PAGE_SIZE,
      ...(motionTypeFilter ? { motionType: motionTypeFilter } : {}),
    },
    notifyOnNetworkStatusChange: true,
  });

  const analytics = analyticsData?.judgeAnalytics;
  const edges = rulingsData?.rulings.edges ?? [];
  const pageInfo = rulingsData?.rulings.pageInfo;

  // Coordinate loading states: only show empty messages when both queries
  // have completed. If one returns empty while the other is still loading,
  // show a skeleton instead of the contradictory "No rulings captured" message.
  const bothLoaded = !analyticsLoading && !rulingsLoading;

  const updateUrl = useCallback(
    (motionType: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (motionType) {
        params.set('motion', motionType);
      } else {
        params.delete('motion');
      }
      const search = params.toString();
      router.replace(search ? `${pathname}?${search}` : pathname);
    },
    [searchParams, router, pathname],
  );

  function handleMotionTypeClick(motionType: string) {
    const newValue = motionTypeFilter === motionType ? null : motionType;
    setMotionTypeFilter(newValue);
    updateUrl(newValue);
    // Scroll the rulings section into view after a short delay to let the
    // state update propagate
    requestAnimationFrame(() => {
      rulingsSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }

  function clearMotionTypeFilter() {
    setMotionTypeFilter(null);
    updateUrl(null);
  }

  const { handleLoadMore } = useInfiniteScroll<RulingsData>({
    hasNextPage: pageInfo?.hasNextPage,
    endCursor: pageInfo?.endCursor,
    loading: rulingsLoading,
    fetchMore,
    merge: useCallback(
      (prev: RulingsData, incoming: RulingsData) => ({
        rulings: {
          ...incoming.rulings,
          edges: [...prev.rulings.edges, ...incoming.rulings.edges],
        },
      }),
      [],
    ),
    filterDeps: [motionTypeFilter],
  });

  // -------------------------------------------------------------------------
  // Analytics section
  // -------------------------------------------------------------------------

  function renderAnalytics() {
    if (analyticsLoading) return <AnalyticsSkeleton />;

    if (analyticsError) {
      return <ErrorBanner message="Failed to load analytics." />;
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
                <p
                  className={`text-3xl font-bold ${analytics.totalRulings < LOW_SAMPLE_THRESHOLD ? 'text-muted-foreground' : 'text-foreground'}`}
                  data-testid="overall-grant-rate"
                >
                  {Math.round(overallGrantRate * 100)}%
                </p>
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Overall Grant Rate
                </p>
                <p
                  className="mt-1 text-xs text-muted-foreground"
                  data-testid="overall-sample-size"
                >
                  Based on {analytics.totalRulings.toLocaleString()} {analytics.totalRulings === 1 ? 'ruling' : 'rulings'}
                  {analytics.totalRulings < LOW_SAMPLE_THRESHOLD && (
                    <span className="ml-1" data-testid="low-confidence-indicator">
                      &middot; Limited data
                    </span>
                  )}
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
              <p
                className="text-xs text-muted-foreground"
                data-testid="motion-type-click-hint"
              >
                Click a row to filter the rulings below by that motion type.
              </p>
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
                    <TableHead className="w-8">
                      <span className="sr-only">Filter</span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {analytics.rulingsByMotionType.map((row) => {
                    const isActive = motionTypeFilter === row.motionType;
                    const isLowSample = row.total < LOW_SAMPLE_THRESHOLD;
                    const motionLabel = formatMotionType(row.motionType);
                    return (
                      <TableRow
                        key={row.motionType}
                        // shadcn-accent: intentional — selected-row chrome (#2832)
                        className={`group cursor-pointer transition-colors ${isActive ? 'bg-accent' : 'hover:bg-accent/50'}`}
                        onClick={() => handleMotionTypeClick(row.motionType)}
                        role="button"
                        tabIndex={0}
                        aria-pressed={isActive}
                        data-low-sample={isLowSample || undefined}
                        data-motion-action={
                          isActive
                            ? `Currently filtering by ${motionLabel} — click to clear`
                            : `Filter rulings by ${motionLabel}`
                        }
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            handleMotionTypeClick(row.motionType);
                          }
                        }}
                      >
                        <TableCell className="font-medium">
                          <span
                            className="text-primary underline-offset-2 group-hover:underline"
                            data-testid={`motion-type-label-${row.motionType}`}
                          >
                            {motionLabel}
                          </span>
                          <span className="sr-only">
                            {isActive
                              ? '. Currently filtering by this motion type. Click to clear.'
                              : '. Click to filter rulings by this motion type.'}
                          </span>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{row.total}</TableCell>
                        <TableCell className="text-right tabular-nums">{row.granted}</TableCell>
                        <TableCell className="text-right tabular-nums">{row.denied}</TableCell>
                        <TableCell className="text-right tabular-nums">{row.grantedInPart}</TableCell>
                        <TableCell
                          className={`text-right tabular-nums ${isLowSample ? 'text-muted-foreground' : 'font-medium'}`}
                          data-testid={`grant-rate-${row.motionType}`}
                        >
                          {Math.round(row.grantRate * 100)}%
                          {isLowSample && (
                            <span className="ml-1 text-xs font-normal" data-testid={`limited-data-${row.motionType}`}>
                              (limited data)
                            </span>
                          )}
                        </TableCell>
                        <TableCell className="w-8 text-right">
                          <ChevronRight
                            className={`h-4 w-4 transition-opacity ${
                              isActive
                                ? 'text-foreground opacity-100'
                                : 'text-muted-foreground opacity-60 group-hover:opacity-100'
                            }`}
                            aria-hidden="true"
                          />
                        </TableCell>
                      </TableRow>
                    );
                  })}
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
      return <ErrorBanner message="Failed to load rulings." />;
    }

    if (!rulingsLoading && edges.length === 0) {
      // If the other query is still loading, show skeleton instead of
      // the empty message to avoid contradictory UI states.
      if (!bothLoaded) return <RulingsSkeleton />;
      if (motionTypeFilter) {
        return (
          <p className="py-8 text-center text-sm text-muted-foreground">
            No {formatMotionType(motionTypeFilter)} rulings found.{' '}
            <button
              type="button"
              onClick={clearMotionTypeFilter}
              className="font-medium text-foreground underline underline-offset-2 hover:text-foreground/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Clear filter
            </button>
          </p>
        );
      }
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
            <div key={node.id} className="px-4 py-3 transition-colors hover:bg-muted/50">
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
                <OutcomeBadge outcome={node.outcome} />
              </div>
            </div>
          ))}
        </div>

        {/* Loading indicator for infinite scroll */}
        {rulingsLoading && edges.length > 0 && (
          <div className="mt-3 divide-y rounded-lg border">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={`loading-${i}`} className="px-4 py-3">
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
        )}

        {/* Infinite scroll sentinel */}
        <InfiniteScrollTrigger
          hasNextPage={pageInfo?.hasNextPage ?? false}
          loading={rulingsLoading}
          onLoadMore={handleLoadMore}
        />
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
      <section ref={rulingsSectionRef}>
        <div className="mb-3 flex items-center gap-3">
          <h2 className={SECTION_LABEL}>
            Recent Rulings
          </h2>
          {motionTypeFilter && (
            // shadcn-accent: intentional — active-filter pill chrome (#2832)
            <span
              className="inline-flex items-center gap-1 rounded-full border border-border bg-accent px-2.5 py-0.5 text-xs font-medium text-foreground"
              data-testid="motion-type-filter-pill"
            >
              {formatMotionType(motionTypeFilter)}
              <button
                type="button"
                onClick={clearMotionTypeFilter}
                className="ml-0.5 rounded-full p-0.5 transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label={`Clear ${formatMotionType(motionTypeFilter)} filter`}
              >
                <X className="h-3 w-3" aria-hidden="true" />
              </button>
            </span>
          )}
        </div>
        {renderRulings()}
      </section>
    </div>
  );
}
