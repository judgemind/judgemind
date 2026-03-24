'use client';

import { useState, useCallback, useEffect, useRef } from 'react';
import { useQuery, gql } from '@apollo/client';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { formatLabel } from '@/lib/display-helpers';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const CASES_QUERY = gql`
  query Cases(
    $caseType: String
    $first: Int!
    $after: String
  ) {
    cases(
      caseType: $caseType
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
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
`;

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
    <TableRow>
      <TableCell>
        <div className="flex animate-pulse motion-reduce:animate-none flex-col gap-1">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-3 w-48" />
        </div>
      </TableCell>
      <TableCell><Skeleton className="h-3 w-16" /></TableCell>
    </TableRow>
  );
}

export function CasesList() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [caseNumberFilter, setCaseNumberFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState(searchParams.get('caseType') ?? '');

  useEffect(() => {
    setTypeFilter(searchParams.get('caseType') ?? '');
  }, [searchParams]);

  const updateUrl = useCallback(() => {
    const params = new URLSearchParams();
    if (typeFilter) params.set('caseType', typeFilter);
    const search = params.toString();
    router.replace(search ? `/cases?${search}` : '/cases');
  }, [typeFilter, router]);

  useEffect(() => {
    updateUrl();
  }, [updateUrl]);

  const { data, loading, error, fetchMore } = useQuery<CasesData>(CASES_QUERY, {
    variables: {
      caseType: typeFilter || undefined,
      first: PAGE_SIZE,
    },
    notifyOnNetworkStatusChange: true,
  });

  const edges = data?.cases.edges ?? [];
  const pageInfo = data?.cases.pageInfo;
  const fetchingMore = useRef(false);
  const observer = useRef<IntersectionObserver | null>(null);
  const queryGeneration = useRef(0);

  // Increment generation when a filter that refetches data changes,
  // so in-flight fetchMore calls don't corrupt the new results.
  useEffect(() => {
    queryGeneration.current += 1;
  }, [typeFilter]);

  const filteredEdges = caseNumberFilter
    ? edges.filter(
        ({ node }) =>
          node.caseNumber.toLowerCase().includes(caseNumberFilter.toLowerCase()) ||
          (node.caseTitle?.toLowerCase().includes(caseNumberFilter.toLowerCase()) ?? false),
      )
    : edges;

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
          cases: {
            ...fetchMoreResult.cases,
            edges: [...prev.cases.edges, ...fetchMoreResult.cases.edges],
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
    <div className="space-y-6">
      <div className="flex flex-wrap gap-3">
        <Input
          type="text"
          name="caseFilter"
          placeholder="Case number or title…"
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
      </div>

      <div className="overflow-hidden rounded-lg border">
        <Table className="table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead>Case</TableHead>
              <TableHead className="w-28">Type</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {loading && edges.length === 0 && (
              <>
                {Array.from({ length: 8 }).map((_, i) => (
                  <SkeletonRow key={i} />
                ))}
              </>
            )}

            {error && (
              <TableRow>
                <TableCell colSpan={2} className="text-center">
                  <p className="py-4 text-sm text-destructive">
                    Failed to load cases. Please try again.
                  </p>
                </TableCell>
              </TableRow>
            )}

            {!loading && !error && filteredEdges.length === 0 && (
              <TableRow>
                <TableCell colSpan={2} className="text-center">
                  <p className="py-4 text-muted-foreground">
                    No cases found. Try adjusting your filters.
                  </p>
                </TableCell>
              </TableRow>
            )}

            {filteredEdges.map(({ node }) => (
              <TableRow key={node.id}>
                <TableCell className="min-w-0">
                  <Link
                    href={`/cases/${node.id}`}
                    className="block truncate rounded-sm font-medium text-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {node.court?.county ? `${node.court.county} – ` : ''}{node.caseNumber}
                  </Link>
                  {node.caseTitle && (
                    <p className="truncate text-sm text-muted-foreground">
                      {node.caseTitle}
                    </p>
                  )}
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {formatLabel(node.caseType)}
                </TableCell>
              </TableRow>
            ))}

            {/* Loading indicator for infinite scroll */}
            {loading && edges.length > 0 && (
              <>
                {Array.from({ length: 3 }).map((_, i) => (
                  <SkeletonRow key={`loading-${i}`} />
                ))}
              </>
            )}
          </TableBody>
        </Table>

        {/* Infinite scroll sentinel */}
        {pageInfo?.hasNextPage && !loading && (
          <div ref={sentinelRef} data-testid="scroll-sentinel" className="h-1" />
        )}
      </div>
    </div>
  );
}
