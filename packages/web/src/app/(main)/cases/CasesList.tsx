'use client';

import { useState, useCallback, useEffect } from 'react';
import { useQuery, gql } from '@apollo/client';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import {
  formatDate,
  formatLabel,
  formatMotionType,
} from '@/lib/display-helpers';
import { Autocomplete } from '@/components/Autocomplete';
import { ErrorBanner } from '@/components/ErrorBanner';
import { InfiniteScrollTrigger } from '@/components/InfiniteScrollTrigger';
import { OutcomeBadge } from '@/components/OutcomeBadge';
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll';
import { useCountyOptions } from '@/lib/filter-options';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';

const CASES_QUERY = gql`
  query Cases(
    $caseType: String
    $county: String
    $dateFrom: String
    $dateTo: String
    $first: Int!
    $after: String
  ) {
    cases(
      caseType: $caseType
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
          caseNumber
          caseTitle
          caseType
          caseStatus
          court {
            courtName
            county
          }
          latestRuling {
            hearingDate
            outcome
            motionType
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

interface LatestRuling {
  hearingDate: string;
  outcome: string | null;
  motionType: string | null;
}

interface CaseNode {
  id: string;
  caseNumber: string;
  caseTitle: string | null;
  caseType: string | null;
  caseStatus: string | null;
  court: {
    courtName: string;
    county: string;
  } | null;
  latestRuling: LatestRuling | null;
}

interface CasesData {
  cases: {
    edges: Array<{ cursor: string; node: CaseNode }>;
    pageInfo: { hasNextPage: boolean; endCursor: string | null };
  };
}

const PAGE_SIZE = 20;

/** Known case type filter options. */
const CASE_TYPES = ['civil', 'family', 'probate', 'small_claims', 'other'] as const;

function SkeletonRow() {
  return (
    <div data-testid="skeleton-row" className="px-4 py-3">
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
    </div>
  );
}

export function CasesList() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [caseNumberFilter, setCaseNumberFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState(searchParams.get('caseType') ?? '');
  const [county, setCounty] = useState(searchParams.get('county') ?? '');
  const [dateFrom, setDateFrom] = useState(searchParams.get('dateFrom') ?? '');
  const [dateTo, setDateTo] = useState(searchParams.get('dateTo') ?? '');
  const countyOptions = useCountyOptions();

  useEffect(() => {
    setTypeFilter(searchParams.get('caseType') ?? '');
    setCounty(searchParams.get('county') ?? '');
    setDateFrom(searchParams.get('dateFrom') ?? '');
    setDateTo(searchParams.get('dateTo') ?? '');
  }, [searchParams]);

  const updateUrl = useCallback(() => {
    const params = new URLSearchParams();
    if (typeFilter) params.set('caseType', typeFilter);
    if (county) params.set('county', county);
    if (dateFrom) params.set('dateFrom', dateFrom);
    if (dateTo) params.set('dateTo', dateTo);
    const search = params.toString();
    router.replace(search ? `/cases?${search}` : '/cases');
  }, [typeFilter, county, dateFrom, dateTo, router]);

  useEffect(() => {
    updateUrl();
  }, [updateUrl]);

  const { data, loading, error, fetchMore } = useQuery<CasesData>(CASES_QUERY, {
    variables: {
      caseType: typeFilter || undefined,
      county: county || undefined,
      dateFrom: dateFrom || undefined,
      dateTo: dateTo || undefined,
      first: PAGE_SIZE,
    },
    notifyOnNetworkStatusChange: true,
  });

  const edges = data?.cases.edges ?? [];
  const pageInfo = data?.cases.pageInfo;

  const { handleLoadMore } = useInfiniteScroll<CasesData>({
    hasNextPage: pageInfo?.hasNextPage,
    endCursor: pageInfo?.endCursor,
    loading,
    fetchMore,
    merge: useCallback(
      (prev: CasesData, incoming: CasesData) => ({
        cases: {
          ...incoming.cases,
          edges: [...prev.cases.edges, ...incoming.cases.edges],
        },
      }),
      [],
    ),
    filterDeps: [typeFilter, county, dateFrom, dateTo],
  });

  const filteredEdges = caseNumberFilter
    ? edges.filter(
        ({ node }) =>
          node.caseNumber.toLowerCase().includes(caseNumberFilter.toLowerCase()) ||
          (node.caseTitle?.toLowerCase().includes(caseNumberFilter.toLowerCase()) ?? false),
      )
    : edges;

  return (
    <div className="space-y-6">
      {/* Filters */}
      <div className="flex flex-wrap gap-3">
        <Input
          type="text"
          name="caseFilter"
          placeholder={"Case number or title\u2026"}
          value={caseNumberFilter}
          onChange={(e) => setCaseNumberFilter(e.target.value)}
          className="w-auto"
          aria-label="Case number or title"
        />
        <Select
          value={typeFilter || 'all'}
          onValueChange={(v) => setTypeFilter(v === 'all' ? '' : v)}
        >
          <SelectTrigger className="w-auto min-w-[140px]" aria-label="Case type" name="caseType">
            <SelectValue placeholder="All types" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types</SelectItem>
            {CASE_TYPES.map((ct) => (
              <SelectItem key={ct} value={ct}>
                {formatLabel(ct)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Autocomplete
          value={county}
          onChange={setCounty}
          options={countyOptions}
          placeholder="County (e.g. Los Angeles)"
          aria-label="County"
          className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        />
        <Input
          type="date"
          name="dateFrom"
          value={dateFrom}
          onChange={(e) => setDateFrom(e.target.value)}
          aria-label="Rulings from"
          title="Rulings from"
          className="w-auto"
        />
        <Input
          type="date"
          name="dateTo"
          value={dateTo}
          onChange={(e) => setDateTo(e.target.value)}
          aria-label="Rulings to"
          title="Rulings to"
          className="w-auto"
        />
      </div>

      {/* Skeleton loading state */}
      {loading && edges.length === 0 && (
        <div className="divide-y rounded-lg border">
          {Array.from({ length: 8 }).map((_, i) => (
            <SkeletonRow key={i} />
          ))}
        </div>
      )}

      {/* Error */}
      {error && (
        <ErrorBanner message="Failed to load cases." />
      )}

      {/* Empty state */}
      {!loading && !error && filteredEdges.length === 0 && (
        <p className="p-8 text-center text-muted-foreground">
          No cases found. Try adjusting your filters.
        </p>
      )}

      {/* Case rows */}
      {filteredEdges.length > 0 && (
        <div>
          <div className="divide-y rounded-lg border">
            {filteredEdges.map(({ node }) => (
              <div key={node.id} className="px-4 py-3 transition-colors hover:bg-muted/50">
                <div className="flex items-start justify-between gap-4">
                  {/* Left: case info, badges */}
                  <div className="min-w-0 flex-1">
                    <Link
                      href={`/cases/${node.id}`}
                      className="block truncate rounded-sm font-medium text-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {node.caseNumber}
                      {node.caseTitle ? ` \u2014 ${node.caseTitle}` : ''}
                    </Link>

                    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                      {node.court?.county && (
                        <span>{node.court.county}</span>
                      )}
                      {node.caseType && (
                        <span>{formatLabel(node.caseType)}</span>
                      )}
                      {node.latestRuling?.motionType && (
                        <span>{formatMotionType(node.latestRuling.motionType)}</span>
                      )}
                    </div>

                    {node.latestRuling && (
                      <div className="mt-2 flex flex-wrap gap-2">
                        <OutcomeBadge outcome={node.latestRuling.outcome} />
                      </div>
                    )}
                  </div>

                  {/* Right: latest ruling date */}
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {node.latestRuling ? formatDate(node.latestRuling.hearingDate) : '\u2014'}
                  </span>
                </div>
              </div>
            ))}
          </div>

          {/* Loading indicator for infinite scroll */}
          {loading && edges.length > 0 && (
            <div className="mt-3 divide-y rounded-lg border">
              {Array.from({ length: 3 }).map((_, i) => (
                <SkeletonRow key={`loading-${i}`} />
              ))}
            </div>
          )}

          {/* Infinite scroll sentinel */}
          <InfiniteScrollTrigger
            hasNextPage={pageInfo?.hasNextPage ?? false}
            loading={loading}
            onLoadMore={handleLoadMore}
          />
        </div>
      )}
    </div>
  );
}
