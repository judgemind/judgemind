'use client';

import { useState, useCallback, useEffect, useMemo } from 'react';
import { useQuery, gql } from '@apollo/client';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { formatDate } from '../rulings/RulingsFeed';

const SEARCH_RULINGS_QUERY = gql`
  query SearchRulings(
    $query: String
    $filters: RulingSearchFilters
    $first: Int
    $after: String
  ) {
    searchRulings(
      query: $query
      filters: $filters
      first: $first
      after: $after
    ) {
      edges {
        cursor
        node {
          rulingId
          caseNumber
          court
          county
          state
          judgeName
          hearingDate
          excerpt
          score
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
      totalHits
    }
  }
`;

/** All motion type filter options. */
export const MOTION_TYPES = [
  'msj',
  'mtd',
  'mil',
  'demurrer',
  'anti_slapp',
  'other',
] as const;

/** Human-readable labels for motion types. */
export const MOTION_TYPE_LABELS: Record<string, string> = {
  msj: 'MSJ',
  mtd: 'MTD',
  mil: 'MIL',
  demurrer: 'Demurrer',
  anti_slapp: 'Anti-SLAPP',
  other: 'Other',
};

/** All outcome filter options. */
export const OUTCOMES = [
  'granted',
  'denied',
  'granted_in_part',
  'moot',
  'continued',
  'other',
] as const;

/** Human-readable labels for outcomes. */
export const OUTCOME_LABELS: Record<string, string> = {
  granted: 'Granted',
  denied: 'Denied',
  granted_in_part: 'Partial',
  moot: 'Moot',
  continued: 'Continued',
  other: 'Other',
};

/** Build URL search params from the current filter state. */
export function buildSearchParams(state: {
  q: string;
  county: string;
  judgeName: string;
  dateFrom: string;
  dateTo: string;
  motionTypes: string[];
  outcomes: string[];
}): URLSearchParams {
  const params = new URLSearchParams();
  if (state.q) params.set('q', state.q);
  if (state.county) params.set('county', state.county);
  if (state.judgeName) params.set('judge', state.judgeName);
  if (state.dateFrom) params.set('dateFrom', state.dateFrom);
  if (state.dateTo) params.set('dateTo', state.dateTo);
  if (state.motionTypes.length > 0)
    params.set('motion', state.motionTypes.join(','));
  if (state.outcomes.length > 0)
    params.set('outcome', state.outcomes.join(','));
  return params;
}

/** Parse URL search params into the filter state. */
export function parseSearchParams(params: URLSearchParams): {
  q: string;
  county: string;
  judgeName: string;
  dateFrom: string;
  dateTo: string;
  motionTypes: string[];
  outcomes: string[];
} {
  return {
    q: params.get('q') ?? '',
    county: params.get('county') ?? '',
    judgeName: params.get('judge') ?? '',
    dateFrom: params.get('dateFrom') ?? '',
    dateTo: params.get('dateTo') ?? '',
    motionTypes: params.get('motion')?.split(',').filter(Boolean) ?? [],
    outcomes: params.get('outcome')?.split(',').filter(Boolean) ?? [],
  };
}

interface SearchHitNode {
  rulingId: string;
  caseNumber: string | null;
  court: string | null;
  county: string | null;
  state: string | null;
  judgeName: string | null;
  hearingDate: string | null;
  excerpt: string | null;
  score: number | null;
}

interface SearchData {
  searchRulings: {
    edges: Array<{ cursor: string; node: SearchHitNode }>;
    pageInfo: { hasNextPage: boolean; endCursor: string | null };
    totalHits: number;
  };
}

const PAGE_SIZE = 20;

function SkeletonCard() {
  return (
    <div className="animate-pulse rounded-lg border border-slate-200 p-4 dark:border-slate-700">
      <div className="flex items-start justify-between">
        <div className="flex-1 space-y-2">
          <div className="h-4 w-2/3 rounded bg-slate-200 dark:bg-slate-700" />
          <div className="h-3 w-1/3 rounded bg-slate-200 dark:bg-slate-700" />
        </div>
        <div className="h-5 w-16 rounded bg-slate-200 dark:bg-slate-700" />
      </div>
      <div className="mt-3 space-y-1.5">
        <div className="h-3 w-full rounded bg-slate-200 dark:bg-slate-700" />
        <div className="h-3 w-4/5 rounded bg-slate-200 dark:bg-slate-700" />
      </div>
    </div>
  );
}

export function SearchPage() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // Parse initial state from URL
  const initialState = useMemo(
    () => parseSearchParams(searchParams),
    [searchParams],
  );

  const [q, setQ] = useState(initialState.q);
  const [county, setCounty] = useState(initialState.county);
  const [judgeName, setJudgeName] = useState(initialState.judgeName);
  const [dateFrom, setDateFrom] = useState(initialState.dateFrom);
  const [dateTo, setDateTo] = useState(initialState.dateTo);
  const [motionTypes, setMotionTypes] = useState<string[]>(
    initialState.motionTypes,
  );
  const [outcomes, setOutcomes] = useState<string[]>(initialState.outcomes);

  // Track whether the user has submitted a search
  const [hasSearched, setHasSearched] = useState(
    initialState.q !== '' ||
      initialState.county !== '' ||
      initialState.judgeName !== '' ||
      initialState.dateFrom !== '' ||
      initialState.dateTo !== '',
  );

  // Build GraphQL variables
  const hasFilters =
    county !== '' || judgeName !== '' || dateFrom !== '' || dateTo !== '';

  const filters = useMemo(() => {
    const f: Record<string, string> = {};
    if (county) f.county = county;
    if (judgeName) f.judgeName = judgeName;
    if (dateFrom) f.dateFrom = dateFrom;
    if (dateTo) f.dateTo = dateTo;
    return Object.keys(f).length > 0 ? f : undefined;
  }, [county, judgeName, dateFrom, dateTo]);

  const shouldQuery = hasSearched && (q !== '' || hasFilters);

  const { data, loading, error, fetchMore } = useQuery<SearchData>(
    SEARCH_RULINGS_QUERY,
    {
      variables: {
        query: q || undefined,
        filters,
        first: PAGE_SIZE,
      },
      skip: !shouldQuery,
      notifyOnNetworkStatusChange: true,
    },
  );

  const edges = data?.searchRulings.edges ?? [];
  const pageInfo = data?.searchRulings.pageInfo;
  const totalHits = data?.searchRulings.totalHits ?? 0;

  // Sync URL params when search is submitted
  const updateUrl = useCallback(
    (overrides?: Partial<ReturnType<typeof parseSearchParams>>) => {
      const state = {
        q: overrides?.q ?? q,
        county: overrides?.county ?? county,
        judgeName: overrides?.judgeName ?? judgeName,
        dateFrom: overrides?.dateFrom ?? dateFrom,
        dateTo: overrides?.dateTo ?? dateTo,
        motionTypes: overrides?.motionTypes ?? motionTypes,
        outcomes: overrides?.outcomes ?? outcomes,
      };
      const params = buildSearchParams(state);
      const search = params.toString();
      router.replace(search ? `/search?${search}` : '/search');
    },
    [q, county, judgeName, dateFrom, dateTo, motionTypes, outcomes, router],
  );

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setHasSearched(true);
    updateUrl();
  }

  function handleLoadMore() {
    if (!pageInfo?.endCursor) return;
    fetchMore({
      variables: { after: pageInfo.endCursor },
      updateQuery(prev, { fetchMoreResult }) {
        if (!fetchMoreResult) return prev;
        return {
          searchRulings: {
            ...fetchMoreResult.searchRulings,
            edges: [
              ...prev.searchRulings.edges,
              ...fetchMoreResult.searchRulings.edges,
            ],
          },
        };
      },
    });
  }

  function toggleMotionType(mt: string) {
    setMotionTypes((prev) =>
      prev.includes(mt) ? prev.filter((x) => x !== mt) : [...prev, mt],
    );
  }

  function toggleOutcome(oc: string) {
    setOutcomes((prev) =>
      prev.includes(oc) ? prev.filter((x) => x !== oc) : [...prev, oc],
    );
  }

  // Sync URL when filters change (after initial search)
  useEffect(() => {
    if (hasSearched) {
      updateUrl();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [county, judgeName, dateFrom, dateTo, motionTypes, outcomes]);

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">
        Search Rulings
      </h1>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
        Full-text search across California tentative rulings.
      </p>

      {/* Search bar */}
      <form onSubmit={handleSubmit} className="mt-6">
        <div className="flex gap-2">
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search by keyword, case number, judge, or party..."
            className="flex-1 rounded-lg border border-slate-300 bg-white px-4 py-2.5 text-slate-900 placeholder-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500"
            aria-label="Search query"
          />
          <button
            type="submit"
            className="rounded-lg bg-brand-600 px-6 py-2.5 text-sm font-medium text-white hover:bg-brand-700 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:ring-offset-2 dark:focus:ring-offset-slate-900"
          >
            Search
          </button>
        </div>
      </form>

      {/* Main content: sidebar filters + results */}
      <div className="mt-6 flex flex-col gap-6 lg:flex-row">
        {/* Filter panel (sidebar) */}
        <aside className="w-full shrink-0 lg:w-64">
          <div className="space-y-5 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800/50">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Filters
            </h2>

            {/* County */}
            <div>
              <label
                htmlFor="filter-county"
                className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-400"
              >
                County
              </label>
              <input
                id="filter-county"
                type="text"
                value={county}
                onChange={(e) => setCounty(e.target.value)}
                placeholder="e.g. Los Angeles"
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900 placeholder-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500"
              />
            </div>

            {/* Judge */}
            <div>
              <label
                htmlFor="filter-judge"
                className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-400"
              >
                Judge
              </label>
              <input
                id="filter-judge"
                type="text"
                value={judgeName}
                onChange={(e) => setJudgeName(e.target.value)}
                placeholder="e.g. Smith, John"
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900 placeholder-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500"
              />
            </div>

            {/* Motion type */}
            <fieldset>
              <legend className="mb-1 text-xs font-medium text-slate-600 dark:text-slate-400">
                Motion Type
              </legend>
              <div className="space-y-1">
                {MOTION_TYPES.map((mt) => (
                  <label key={mt} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={motionTypes.includes(mt)}
                      onChange={() => toggleMotionType(mt)}
                      className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600"
                    />
                    <span className="text-slate-700 dark:text-slate-300">
                      {MOTION_TYPE_LABELS[mt]}
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>

            {/* Outcome */}
            <fieldset>
              <legend className="mb-1 text-xs font-medium text-slate-600 dark:text-slate-400">
                Outcome
              </legend>
              <div className="space-y-1">
                {OUTCOMES.map((oc) => (
                  <label key={oc} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={outcomes.includes(oc)}
                      onChange={() => toggleOutcome(oc)}
                      className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600"
                    />
                    <span className="text-slate-700 dark:text-slate-300">
                      {OUTCOME_LABELS[oc]}
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>

            {/* Date range */}
            <div>
              <label
                htmlFor="filter-date-from"
                className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-400"
              >
                Date from
              </label>
              <input
                id="filter-date-from"
                type="date"
                value={dateFrom}
                onChange={(e) => setDateFrom(e.target.value)}
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
              />
            </div>
            <div>
              <label
                htmlFor="filter-date-to"
                className="mb-1 block text-xs font-medium text-slate-600 dark:text-slate-400"
              >
                Date to
              </label>
              <input
                id="filter-date-to"
                type="date"
                value={dateTo}
                onChange={(e) => setDateTo(e.target.value)}
                className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
              />
            </div>
          </div>
        </aside>

        {/* Results area */}
        <div className="min-w-0 flex-1">
          {/* Before first search */}
          {!hasSearched && (
            <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-slate-300 py-16 dark:border-slate-600">
              <p className="text-lg font-medium text-slate-400 dark:text-slate-500">
                Enter a search term to begin
              </p>
              <p className="mt-1 text-sm text-slate-400 dark:text-slate-500">
                Search by keyword, case number, judge name, or party
              </p>
            </div>
          )}

          {/* Loading state */}
          {hasSearched && loading && edges.length === 0 && (
            <div className="space-y-4">
              {Array.from({ length: 5 }).map((_, i) => (
                <SkeletonCard key={i} />
              ))}
            </div>
          )}

          {/* Error state */}
          {hasSearched && error && (
            <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-center dark:border-red-800 dark:bg-red-900/20">
              <p className="text-sm text-red-600 dark:text-red-400">
                Failed to load search results. Please try again.
              </p>
            </div>
          )}

          {/* Empty results */}
          {hasSearched &&
            !loading &&
            !error &&
            shouldQuery &&
            edges.length === 0 && (
              <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-slate-300 py-16 dark:border-slate-600">
                <p className="text-lg font-medium text-slate-500 dark:text-slate-400">
                  No results for your search
                </p>
                <p className="mt-1 text-sm text-slate-400 dark:text-slate-500">
                  Try broadening your filters or using different keywords
                </p>
              </div>
            )}

          {/* Results header */}
          {hasSearched && edges.length > 0 && (
            <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">
              {totalHits.toLocaleString()} result
              {totalHits !== 1 ? 's' : ''} found
            </p>
          )}

          {/* Result cards */}
          {edges.length > 0 && (
            <div className="space-y-4">
              {edges.map(({ node }) => (
                <Link
                  key={node.rulingId}
                  href={`/rulings/${node.rulingId}`}
                  className="block rounded-lg border border-slate-200 bg-white p-4 transition-colors hover:border-slate-300 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-800/50 dark:hover:border-slate-600 dark:hover:bg-slate-800"
                >
                  {/* Top row: case info + hearing date */}
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <h3 className="truncate font-medium text-slate-900 dark:text-slate-100">
                        {node.caseNumber ?? 'Unknown Case'}
                      </h3>
                      <p className="mt-0.5 truncate text-sm text-slate-500 dark:text-slate-400">
                        {node.judgeName ? `Judge ${node.judgeName}` : ''}
                        {node.judgeName && node.county ? ' \u00B7 ' : ''}
                        {node.county ?? ''}
                      </p>
                    </div>
                    {node.hearingDate && (
                      <span className="shrink-0 text-xs text-slate-400 dark:text-slate-500">
                        {formatDate(node.hearingDate)}
                      </span>
                    )}
                  </div>

                  {/* Excerpt */}
                  {node.excerpt && (
                    <p
                      className="mt-2 line-clamp-3 text-sm text-slate-600 dark:text-slate-400"
                      dangerouslySetInnerHTML={{ __html: node.excerpt }}
                    />
                  )}
                </Link>
              ))}

              {/* Load more */}
              {pageInfo?.hasNextPage && (
                <div className="flex justify-center pt-2">
                  <button
                    onClick={(e) => {
                      e.preventDefault();
                      handleLoadMore();
                    }}
                    disabled={loading}
                    className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                  >
                    {loading ? 'Loading...' : 'Load more'}
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
