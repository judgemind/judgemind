'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import { useQuery, gql } from '@apollo/client';
import Link from 'next/link';
import {
  formatDate,
  formatOutcome,
  formatMotionType,
  formatJudgeName,
} from '@/lib/display-helpers';
import { Autocomplete } from '@/components/Autocomplete';
import { useCountyOptions } from '@/lib/filter-options';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';

const RULINGS_QUERY = gql`
  query Rulings(
    $county: String
    $dateFrom: String
    $dateTo: String
    $first: Int!
    $after: String
  ) {
    rulings(
      county: $county
      dateFrom: $dateFrom
      dateTo: $dateTo
      first: $first
      after: $after
    ) {
      edges {
        cursor
        node {
          id
          hearingDate
          outcome
          motionType
          department
          case {
            id
            caseNumber
            caseTitle
            court {
              county
              courtName
            }
          }
          judge {
            canonicalName
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

interface RulingNode {
  id: string;
  hearingDate: string;
  outcome: string | null;
  motionType: string | null;
  department: string | null;
  case: {
    id: string;
    caseNumber: string;
    caseTitle: string | null;
    court: {
      county: string;
      courtName: string;
    };
  } | null;
  judge: {
    canonicalName: string;
  } | null;
}

interface RulingsData {
  rulings: {
    edges: Array<{ cursor: string; node: RulingNode }>;
    pageInfo: { hasNextPage: boolean; endCursor: string | null };
  };
}

const PAGE_SIZE = 20;

const OUTCOME_VARIANT: Record<string, string> = {
  granted: 'bg-green-100 text-green-800 border-green-200 dark:bg-green-900 dark:text-green-200 dark:border-green-800',
  denied: 'bg-red-100 text-red-800 border-red-200 dark:bg-red-900 dark:text-red-200 dark:border-red-800',
  granted_in_part: 'bg-yellow-100 text-yellow-800 border-yellow-200 dark:bg-yellow-900 dark:text-yellow-200 dark:border-yellow-800',
  denied_in_part: 'bg-yellow-100 text-yellow-800 border-yellow-200 dark:bg-yellow-900 dark:text-yellow-200 dark:border-yellow-800',
};

function SkeletonCard() {
  return (
    <Card data-testid="skeleton-card">
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1 space-y-2">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-3 w-1/2" />
            <div className="flex gap-2 pt-1">
              <Skeleton className="h-5 w-16 rounded-full" />
              <Skeleton className="h-5 w-20 rounded-full" />
            </div>
          </div>
          <Skeleton className="h-3 w-20 shrink-0" />
        </div>
      </CardContent>
    </Card>
  );
}

export function RulingsFeed() {
  const [county, setCounty] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const countyOptions = useCountyOptions();

  const { data, loading, error, fetchMore } = useQuery<RulingsData>(RULINGS_QUERY, {
    variables: {
      county: county || undefined,
      dateFrom: dateFrom || undefined,
      dateTo: dateTo || undefined,
      first: PAGE_SIZE,
    },
    notifyOnNetworkStatusChange: true,
  });

  const edges = data?.rulings.edges ?? [];
  const pageInfo = data?.rulings.pageInfo;
  const fetchingMore = useRef(false);
  const observer = useRef<IntersectionObserver | null>(null);

  const handleLoadMore = useCallback(() => {
    if (!pageInfo?.endCursor || loading || fetchingMore.current) return;
    fetchingMore.current = true;
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
    }).finally(() => {
      fetchingMore.current = false;
    });
  }, [pageInfo?.endCursor, loading, fetchMore]);

  const sentinelRef = useCallback(
    (node: HTMLDivElement | null) => {
      if (observer.current) observer.current.disconnect();
      if (!node) return;
      observer.current = new IntersectionObserver(
        (entries) => {
          if (entries[0]?.isIntersecting) {
            handleLoadMore();
          }
        },
        { rootMargin: '200px' },
      );
      observer.current.observe(node);
    },
    [handleLoadMore],
  );

  useEffect(() => {
    return () => {
      if (observer.current) observer.current.disconnect();
    };
  }, []);

  return (
    <div>
      {/* Filters */}
      <div className="mb-4 flex flex-wrap gap-3">
        <Autocomplete
          value={county}
          onChange={setCounty}
          options={countyOptions}
          placeholder="County (e.g. Los Angeles)"
          aria-label="County"
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500"
        />
        <input
          type="date"
          value={dateFrom}
          onChange={(e) => setDateFrom(e.target.value)}
          title="Hearings from"
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
        />
        <input
          type="date"
          value={dateTo}
          onChange={(e) => setDateTo(e.target.value)}
          title="Hearings to"
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
        />
      </div>

      {/* Skeleton loading state */}
      {loading && edges.length === 0 && (
        <div className="space-y-3">
          {Array.from({ length: 8 }).map((_, i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
      )}

      {/* Error */}
      {error && (
        <p className="p-8 text-center text-sm text-red-500 dark:text-red-400">
          Failed to load rulings. Please try again.
        </p>
      )}

      {/* Empty state */}
      {!loading && !error && edges.length === 0 && (
        <p className="p-8 text-center text-slate-400 dark:text-slate-500">
          No rulings found. Try adjusting your filters, or check back after scrapers have run.
        </p>
      )}

      {/* Ruling cards */}
      {edges.length > 0 && (
        <div className="space-y-3">
          {edges.map(({ node }) => (
            <Card key={node.id} className="transition-colors hover:bg-accent/50">
              <CardContent className="p-4">
                <div className="flex items-start justify-between gap-4">
                  {/* Left: case info, judge, badges */}
                  <div className="min-w-0 flex-1">
                    {node.case ? (
                      <Link
                        href={`/rulings/${node.id}`}
                        className="block truncate font-medium text-slate-900 hover:text-brand-600 dark:text-slate-100 dark:hover:text-brand-400"
                      >
                        {node.case.caseNumber}
                        {node.case.caseTitle ? ` \u2014 ${node.case.caseTitle}` : ''}
                      </Link>
                    ) : (
                      <span className="text-slate-400">{'\u2014'}</span>
                    )}

                    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-slate-500 dark:text-slate-400">
                      {node.case?.court && (
                        <span>{node.case.court.county} {node.department ? `\u00B7 Dept. ${node.department}` : ''}</span>
                      )}
                      <span>{formatJudgeName(node.judge)}</span>
                    </div>

                    <div className="mt-2 flex flex-wrap gap-2">
                      <Badge
                        className={`${
                          node.outcome && OUTCOME_VARIANT[node.outcome]
                            ? OUTCOME_VARIANT[node.outcome]
                            : 'bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-700 dark:text-slate-300 dark:border-slate-600'
                        }`}
                      >
                        {formatOutcome(node.outcome)}
                      </Badge>
                      <Badge variant="outline" className="text-slate-600 dark:text-slate-400">
                        {formatMotionType(node.motionType)}
                      </Badge>
                    </div>
                  </div>

                  {/* Right: date */}
                  <span className="shrink-0 text-xs text-slate-500 dark:text-slate-400">
                    {formatDate(node.hearingDate)}
                  </span>
                </div>
              </CardContent>
            </Card>
          ))}

          {/* Loading indicator for infinite scroll */}
          {loading && edges.length > 0 && (
            <div className="space-y-3">
              {Array.from({ length: 3 }).map((_, i) => (
                <SkeletonCard key={`loading-${i}`} />
              ))}
            </div>
          )}

          {/* Infinite scroll sentinel */}
          {pageInfo?.hasNextPage && !loading && (
            <div ref={sentinelRef} data-testid="scroll-sentinel" className="h-1" />
          )}
        </div>
      )}
    </div>
  );
}
