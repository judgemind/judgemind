'use client';

import { useState, useEffect, useCallback } from 'react';
import { useQuery, gql } from '@apollo/client';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import {
  formatDate,
  formatLabel,
  formatMotionType,
  formatJudgeName,
  OUTCOME_LABELS,
  MOTION_TYPE_LABELS,
} from '@/lib/display-helpers';
import { Autocomplete } from '@/components/Autocomplete';
import { InfiniteScrollTrigger } from '@/components/InfiniteScrollTrigger';
import { OutcomeBadge } from '@/components/OutcomeBadge';
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll';
import { CASE_TYPES, OUTCOMES, MOTION_TYPES, useCountyOptions } from '@/lib/filter-options';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';

const RULINGS_QUERY = gql`
  query Rulings(
    $county: String
    $caseType: String
    $outcome: String
    $motionType: String
    $dateFrom: String
    $dateTo: String
    $first: Int!
    $after: String
  ) {
    rulings(
      county: $county
      caseType: $caseType
      outcome: $outcome
      motionType: $motionType
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
            caseType
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
    caseType: string | null;
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

export function RulingsFeed() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [county, setCounty] = useState(searchParams.get('county') ?? '');
  const [caseType, setCaseType] = useState(searchParams.get('caseType') ?? '');
  const [outcome, setOutcome] = useState(searchParams.get('outcome') ?? '');
  const [motionType, setMotionType] = useState(searchParams.get('motionType') ?? '');
  const [dateFrom, setDateFrom] = useState(searchParams.get('dateFrom') ?? '');
  const [dateTo, setDateTo] = useState(searchParams.get('dateTo') ?? '');
  const countyOptions = useCountyOptions();

  useEffect(() => {
    setCounty(searchParams.get('county') ?? '');
    setCaseType(searchParams.get('caseType') ?? '');
    setOutcome(searchParams.get('outcome') ?? '');
    setMotionType(searchParams.get('motionType') ?? '');
    setDateFrom(searchParams.get('dateFrom') ?? '');
    setDateTo(searchParams.get('dateTo') ?? '');
  }, [searchParams]);

  const updateUrl = useCallback(() => {
    const params = new URLSearchParams();
    if (county) params.set('county', county);
    if (caseType) params.set('caseType', caseType);
    if (outcome) params.set('outcome', outcome);
    if (motionType) params.set('motionType', motionType);
    if (dateFrom) params.set('dateFrom', dateFrom);
    if (dateTo) params.set('dateTo', dateTo);
    const search = params.toString();
    router.replace(search ? `/rulings?${search}` : '/rulings');
  }, [county, caseType, outcome, motionType, dateFrom, dateTo, router]);

  useEffect(() => {
    updateUrl();
  }, [updateUrl]);

  const { data, loading, error, fetchMore } = useQuery<RulingsData>(RULINGS_QUERY, {
    variables: {
      county: county || undefined,
      caseType: caseType || undefined,
      outcome: outcome || undefined,
      motionType: motionType || undefined,
      dateFrom: dateFrom || undefined,
      dateTo: dateTo || undefined,
      first: PAGE_SIZE,
    },
    notifyOnNetworkStatusChange: true,
  });

  const edges = data?.rulings.edges ?? [];
  const pageInfo = data?.rulings.pageInfo;

  const { handleLoadMore } = useInfiniteScroll<RulingsData>({
    hasNextPage: pageInfo?.hasNextPage,
    endCursor: pageInfo?.endCursor,
    loading,
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
  });

  return (
    <div className="space-y-6">
      {/* Filters */}
      <div className="flex flex-wrap gap-3">
        <Autocomplete
          value={county}
          onChange={setCounty}
          options={countyOptions}
          placeholder="County (e.g. Los Angeles)"
          aria-label="County"
          className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        />
        <Select
          value={caseType || 'all'}
          onValueChange={(v) => setCaseType(v === 'all' ? '' : v)}
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
        <Select
          value={outcome || 'all'}
          onValueChange={(v) => setOutcome(v === 'all' ? '' : v)}
        >
          <SelectTrigger className="w-auto min-w-[140px]" aria-label="Outcome" name="outcome">
            <SelectValue placeholder="All outcomes" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All outcomes</SelectItem>
            {OUTCOMES.map((o) => (
              <SelectItem key={o} value={o}>
                {OUTCOME_LABELS[o] ?? o}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={motionType || 'all'}
          onValueChange={(v) => setMotionType(v === 'all' ? '' : v)}
        >
          <SelectTrigger className="w-auto min-w-[140px]" aria-label="Motion type" name="motionType">
            <SelectValue placeholder="All motions" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All motions</SelectItem>
            {MOTION_TYPES.map((mt) => (
              <SelectItem key={mt} value={mt}>
                {MOTION_TYPE_LABELS[mt] ?? mt}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          type="date"
          name="dateFrom"
          value={dateFrom}
          onChange={(e) => setDateFrom(e.target.value)}
          aria-label="Hearings from"
          title="Hearings from"
          className="w-auto"
        />
        <Input
          type="date"
          name="dateTo"
          value={dateTo}
          onChange={(e) => setDateTo(e.target.value)}
          aria-label="Hearings to"
          title="Hearings to"
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
        <p className="p-8 text-center text-sm text-red-500 dark:text-red-400">
          Failed to load rulings. Please try again.
        </p>
      )}

      {/* Empty state */}
      {!loading && !error && edges.length === 0 && (
        <p className="p-8 text-center text-muted-foreground">
          No rulings found. Try adjusting your filters, or check back after scrapers have run.
        </p>
      )}

      {/* Ruling rows */}
      {edges.length > 0 && (
        <div>
          <div className="divide-y rounded-lg border">
            {edges.map(({ node }) => (
              <div key={node.id} className="px-4 py-3 transition-colors hover:bg-muted/50">
                <div className="flex items-start justify-between gap-4">
                  {/* Left: case info, judge, badges */}
                  <div className="min-w-0 flex-1">
                    {/* Title + outcome badge on same line */}
                    <div className="flex items-center gap-2">
                      {node.case ? (
                        <Link
                          href={`/rulings/${node.id}`}
                          className="truncate rounded-sm font-medium text-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          {node.case.caseNumber}
                          {node.case.caseTitle ? ` \u2014 ${node.case.caseTitle}` : ''}
                        </Link>
                      ) : (
                        <span className="text-muted-foreground">{'\u2014'}</span>
                      )}
                      <OutcomeBadge outcome={node.outcome} className="shrink-0" />
                    </div>

                    {/* Court/judge metadata — subordinate */}
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground/70">
                      {node.case?.court && (
                        <span>{node.case.court.county} {node.department ? `\u00B7 Dept. ${node.department}` : ''}</span>
                      )}
                      <span>{formatJudgeName(node.judge)}</span>
                    </div>

                    {/* Supporting badges — muted so they don't compete with outcome */}
                    <div className="mt-1.5 flex flex-wrap gap-1.5">
                      <Badge variant="outline" className="text-[11px] font-normal text-muted-foreground/70">
                        {formatMotionType(node.motionType)}
                      </Badge>
                      {node.case?.caseType && (
                        <Badge variant="outline" className="text-[11px] font-normal text-muted-foreground/70" data-testid="case-type-badge">
                          {formatLabel(node.case.caseType)}
                        </Badge>
                      )}
                    </div>
                  </div>

                  {/* Right: date */}
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {formatDate(node.hearingDate)}
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
