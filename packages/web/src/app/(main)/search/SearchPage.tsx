'use client';

import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { useQuery, gql } from '@apollo/client';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { Search, SlidersHorizontal, Calendar, Scale, Gavel } from 'lucide-react';
import { formatDate } from '@/lib/display-helpers';
import { PAGE_TITLE, SECTION_LABEL } from '@/lib/typography';
import { sanitizeExcerptHtml } from '@/lib/sanitize-html';
import { Autocomplete } from '@/components/Autocomplete';
import { InfiniteScrollTrigger } from '@/components/InfiniteScrollTrigger';
import { OutcomeBadge } from '@/components/OutcomeBadge';
import { useCountyOptions, useJudgeNameOptions } from '@/lib/filter-options';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
  SheetDescription,
} from '@/components/ui/sheet';

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
          caseTitle
          court
          county
          state
          judgeName
          hearingDate
          motionType
          outcome
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

/** Human-readable labels for outcomes.
 *  Labels must match formatOutcome() output for consistency with OutcomeBadge. */
export const OUTCOME_LABELS: Record<string, string> = {
  granted: 'Granted',
  denied: 'Denied',
  granted_in_part: 'Granted In Part',
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
  caseTitle: string | null;
  court: string | null;
  county: string | null;
  state: string | null;
  judgeName: string | null;
  hearingDate: string | null;
  motionType: string | null;
  outcome: string | null;
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

function SkeletonRow() {
  return (
    <div className="px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1 space-y-2">
          <Skeleton className="h-5 w-2/3" />
          <Skeleton className="h-3 w-1/3" />
        </div>
        <Skeleton className="h-5 w-16" />
      </div>
      <div className="mt-3 flex gap-2">
        <Skeleton className="h-5 w-14 rounded-full" />
        <Skeleton className="h-5 w-16 rounded-full" />
      </div>
      <div className="mt-3 space-y-1.5">
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-4/5" />
      </div>
    </div>
  );
}

/** The filter sidebar content — shared between desktop sidebar and mobile sheet. */
function FilterContent({
  county,
  setCounty,
  judgeName,
  setJudgeName,
  dateFrom,
  setDateFrom,
  dateTo,
  setDateTo,
  motionTypes,
  toggleMotionType,
  outcomes: outcomeValues,
  toggleOutcome,
  countyOptions,
  judgeNameOptions,
}: {
  county: string;
  setCounty: (v: string) => void;
  judgeName: string;
  setJudgeName: (v: string) => void;
  dateFrom: string;
  setDateFrom: (v: string) => void;
  dateTo: string;
  setDateTo: (v: string) => void;
  motionTypes: string[];
  toggleMotionType: (mt: string) => void;
  outcomes: string[];
  toggleOutcome: (oc: string) => void;
  countyOptions: string[];
  judgeNameOptions: string[];
}) {
  return (
    <div className="space-y-6">
      <div>
        <h2 className={`flex items-center gap-2 ${SECTION_LABEL}`}>
          <SlidersHorizontal className="h-4 w-4" aria-hidden="true" />
          Filters
        </h2>
      </div>

      <Separator />

      {/* County */}
      <div className="space-y-2">
        <Label htmlFor="filter-county">County</Label>
        <Autocomplete
          id="filter-county"
          value={county}
          onChange={setCounty}
          options={countyOptions}
          placeholder="e.g. Los Angeles"
          aria-label="County"
          className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        />
      </div>

      {/* Judge */}
      <div className="space-y-2">
        <Label htmlFor="filter-judge">Judge</Label>
        <Autocomplete
          id="filter-judge"
          value={judgeName}
          onChange={setJudgeName}
          options={judgeNameOptions}
          placeholder="e.g. Smith, John"
          aria-label="Judge"
          className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        />
      </div>

      <Separator />

      {/* Motion type */}
      <fieldset className="space-y-2">
        <legend className="flex items-center gap-2 text-sm font-medium">
          <Scale className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          Motion Type
        </legend>
        <div className="space-y-2">
          {MOTION_TYPES.map((mt) => (
            <div key={mt} className="flex items-center gap-2">
              <Checkbox
                id={`filter-mt-${mt}`}
                checked={motionTypes.includes(mt)}
                onCheckedChange={() => toggleMotionType(mt)}
              />
              <Label
                htmlFor={`filter-mt-${mt}`}
                className="text-sm font-normal text-foreground"
              >
                {MOTION_TYPE_LABELS[mt]}
              </Label>
            </div>
          ))}
        </div>
      </fieldset>

      <Separator />

      {/* Outcome */}
      <fieldset className="space-y-2">
        <legend className="flex items-center gap-2 text-sm font-medium">
          <Gavel className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          Outcome
        </legend>
        <div className="space-y-2">
          {OUTCOMES.map((oc) => (
            <div key={oc} className="flex items-center gap-2">
              <Checkbox
                id={`filter-oc-${oc}`}
                checked={outcomeValues.includes(oc)}
                onCheckedChange={() => toggleOutcome(oc)}
              />
              <Label
                htmlFor={`filter-oc-${oc}`}
                className="text-sm font-normal text-foreground"
              >
                {OUTCOME_LABELS[oc]}
              </Label>
            </div>
          ))}
        </div>
      </fieldset>

      <Separator />

      {/* Date range */}
      <div className="space-y-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Calendar className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          Date Range
        </div>
        <div className="space-y-2">
          <Label htmlFor="filter-date-from">Date from</Label>
          <Input
            id="filter-date-from"
            type="date"
            value={dateFrom}
            onChange={(e) => setDateFrom(e.target.value)}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="filter-date-to">Date to</Label>
          <Input
            id="filter-date-to"
            type="date"
            value={dateTo}
            onChange={(e) => setDateTo(e.target.value)}
          />
        </div>
      </div>
    </div>
  );
}

/** A single search result row. */
function ResultRow({ node }: { node: SearchHitNode }) {
  const motionLabel = node.motionType ? MOTION_TYPE_LABELS[node.motionType] ?? node.motionType : null;

  return (
    <Link href={`/rulings/${node.rulingId}`} className="block transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2">
      <div className="px-4 py-3">
        {/* Top row: case info + hearing date */}
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-base font-semibold text-foreground">
              {node.caseTitle ?? node.caseNumber ?? 'Unknown Case'}
            </h3>
            {node.caseTitle && node.caseNumber && (
              <p className="mt-0.5 truncate text-xs text-muted-foreground">
                {node.caseNumber}
              </p>
            )}
          </div>
          {node.hearingDate && (
            <span className="shrink-0 text-xs text-muted-foreground">
              {formatDate(node.hearingDate)}
            </span>
          )}
        </div>

        {/* Metadata row */}
        <p className="mt-1 truncate text-sm text-muted-foreground">
          {node.judgeName ? `Judge ${node.judgeName}` : ''}
          {node.judgeName && node.county ? ' \u00B7 ' : ''}
          {node.county ?? ''}
        </p>

        {/* Badges for motion type and outcome */}
        {(motionLabel || node.outcome) && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {motionLabel && (
              <Badge variant="secondary">{motionLabel}</Badge>
            )}
            {node.outcome && (
              <OutcomeBadge outcome={node.outcome} />
            )}
          </div>
        )}

        {/* Excerpt */}
        {node.excerpt && (
          <p
            className="mt-3 line-clamp-3 text-sm text-muted-foreground"
            dangerouslySetInnerHTML={{ __html: sanitizeExcerptHtml(node.excerpt) }}
          />
        )}
      </div>
    </Link>
  );
}

export function SearchPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const countyOptions = useCountyOptions();
  const judgeNameOptions = useJudgeNameOptions();

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
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false);

  // Track whether the user has submitted a search
  const [hasSearched, setHasSearched] = useState(
    initialState.q !== '' ||
      initialState.county !== '' ||
      initialState.judgeName !== '' ||
      initialState.dateFrom !== '' ||
      initialState.dateTo !== '' ||
      initialState.motionTypes.length > 0 ||
      initialState.outcomes.length > 0,
  );

  // Build GraphQL variables
  const hasFilters =
    county !== '' ||
    judgeName !== '' ||
    dateFrom !== '' ||
    dateTo !== '' ||
    motionTypes.length > 0 ||
    outcomes.length > 0;

  const filters = useMemo(() => {
    const f: Record<string, string | string[]> = {};
    if (county) f.county = county;
    if (judgeName) f.judgeName = judgeName;
    if (dateFrom) f.dateFrom = dateFrom;
    if (dateTo) f.dateTo = dateTo;
    if (motionTypes.length > 0) f.motionTypes = motionTypes;
    if (outcomes.length > 0) f.outcomes = outcomes;
    return Object.keys(f).length > 0 ? f : undefined;
  }, [county, judgeName, dateFrom, dateTo, motionTypes, outcomes]);

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
  const fetchingMore = useRef(false);
  const queryGeneration = useRef(0);

  // Increment generation when filters that refetch data change,
  // so in-flight fetchMore calls don't corrupt the new results.
  useEffect(() => {
    queryGeneration.current += 1;
  }, [q, county, judgeName, dateFrom, dateTo, motionTypes, outcomes, hasSearched]);

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

  const handleLoadMore = useCallback(() => {
    if (!pageInfo?.endCursor || loading || fetchingMore.current) return;
    fetchingMore.current = true;
    const currentGeneration = queryGeneration.current;
    fetchMore({
      variables: { after: pageInfo.endCursor },
      updateQuery(prev, { fetchMoreResult }) {
        if (!fetchMoreResult) return prev;
        // If filters changed since this fetch started, discard the stale results
        if (queryGeneration.current !== currentGeneration) return prev;
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
    }).finally(() => {
      fetchingMore.current = false;
    });
  }, [pageInfo?.endCursor, loading, fetchMore]);

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

  const filterProps = {
    county,
    setCounty,
    judgeName,
    setJudgeName,
    dateFrom,
    setDateFrom,
    dateTo,
    setDateTo,
    motionTypes,
    toggleMotionType,
    outcomes,
    toggleOutcome,
    countyOptions,
    judgeNameOptions,
  };

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      {/* Page header */}
      <div>
        <h1 className={PAGE_TITLE}>
          Search Rulings
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Full-text search across California tentative rulings.
        </p>
      </div>

      {/* Search bar */}
      <form onSubmit={handleSubmit}>
        <div className="flex gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <Input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search by keyword, case number, judge, or party…"
              className="pl-9"
              aria-label="Search query"
            />
          </div>
          <Button type="submit">Search</Button>
          {/* Mobile filter toggle */}
          <Sheet open={mobileFiltersOpen} onOpenChange={setMobileFiltersOpen}>
            <SheetTrigger asChild>
              <Button variant="outline" size="icon" className="lg:hidden" aria-label="Open filters">
                <SlidersHorizontal className="h-4 w-4" aria-hidden="true" />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="w-80 overflow-y-auto">
              <SheetHeader>
                <SheetTitle>Filters</SheetTitle>
                <SheetDescription>Refine your search results</SheetDescription>
              </SheetHeader>
              <div className="mt-4">
                <FilterContent {...filterProps} />
              </div>
            </SheetContent>
          </Sheet>
        </div>
      </form>

      {/* Main content: sidebar filters + results */}
      <div className="flex gap-6">
        {/* Desktop filter sidebar */}
        <aside aria-label="Search filters" className="hidden w-64 shrink-0 lg:block">
          <Card>
            <CardContent className="p-4">
              <FilterContent {...filterProps} />
            </CardContent>
          </Card>
        </aside>

        {/* Results area */}
        <div className="min-w-0 flex-1">
          {/* Before first search */}
          {!hasSearched && (
            <Card className="border-dashed">
              <CardContent className="flex flex-col items-center justify-center py-16">
                <Search className="mb-4 h-12 w-12 text-muted-foreground/50" aria-hidden="true" />
                <p className="text-lg font-medium text-muted-foreground">
                  Enter a search term to begin
                </p>
                <p className="mt-1 text-sm text-muted-foreground">
                  Search by keyword, case number, judge name, or party
                </p>
              </CardContent>
            </Card>
          )}

          {/* Loading state */}
          {hasSearched && loading && edges.length === 0 && (
            <div className="divide-y rounded-lg border">
              {Array.from({ length: 5 }).map((_, i) => (
                <SkeletonRow key={i} />
              ))}
            </div>
          )}

          {/* Error state */}
          {hasSearched && error && (
            <Card className="border-destructive">
              <CardContent className="py-6 text-center">
                <p className="text-sm text-destructive">
                  Failed to load search results. Please try again.
                </p>
              </CardContent>
            </Card>
          )}

          {/* Empty results */}
          {hasSearched &&
            !loading &&
            !error &&
            shouldQuery &&
            edges.length === 0 && (
              <Card className="border-dashed">
                <CardContent className="flex flex-col items-center justify-center py-16">
                  <Search className="mb-4 h-12 w-12 text-muted-foreground/50" aria-hidden="true" />
                  <p className="text-lg font-medium text-muted-foreground">
                    No results for your search
                  </p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Try broadening your filters or using different keywords
                  </p>
                </CardContent>
              </Card>
            )}

          {/* Results header */}
          {hasSearched && edges.length > 0 && (
            <p className="mb-4 text-sm font-medium text-muted-foreground">
              {totalHits.toLocaleString()} result
              {totalHits !== 1 ? 's' : ''} found
            </p>
          )}

          {/* Result rows */}
          {edges.length > 0 && (
            <div>
              <div className="divide-y rounded-lg border">
                {edges.map(({ node }) => (
                  <ResultRow key={node.rulingId} node={node} />
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
      </div>
    </div>
  );
}
