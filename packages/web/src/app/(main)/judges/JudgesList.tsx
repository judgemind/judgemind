'use client';

import { useState, useCallback, useEffect } from 'react';
import { useQuery, gql } from '@apollo/client';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { Search } from 'lucide-react';
import { InfiniteScrollTrigger } from '@/components/InfiniteScrollTrigger';
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const JUDGES_QUERY = gql`
  query Judges($first: Int!, $after: String) {
    judges(first: $first, after: $after) {
      edges {
        cursor
        node {
          id
          canonicalName
          department
          isActive
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

interface JudgeNode {
  id: string;
  canonicalName: string;
  department: string | null;
  isActive: boolean;
  court: {
    courtName: string;
    county: string;
  } | null;
}

interface JudgesData {
  judges: {
    edges: Array<{ cursor: string; node: JudgeNode }>;
    pageInfo: { hasNextPage: boolean; endCursor: string | null };
  };
}

const PAGE_SIZE = 20;

function SkeletonRow() {
  return (
    <TableRow>
      <TableCell>
        <Skeleton className="h-4 w-40" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-4 w-24" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-4 w-16" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-5 w-16 rounded-full" />
      </TableCell>
    </TableRow>
  );
}

export function JudgesList() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [nameFilter, setNameFilter] = useState(searchParams.get('name') ?? '');

  useEffect(() => {
    setNameFilter(searchParams.get('name') ?? '');
  }, [searchParams]);

  const updateUrl = useCallback(() => {
    const params = new URLSearchParams();
    if (nameFilter) params.set('name', nameFilter);
    const search = params.toString();
    router.replace(search ? `/judges?${search}` : '/judges');
  }, [nameFilter, router]);

  useEffect(() => {
    updateUrl();
  }, [updateUrl]);

  const { data, loading, error, fetchMore } = useQuery<JudgesData>(JUDGES_QUERY, {
    variables: {
      first: PAGE_SIZE,
    },
    notifyOnNetworkStatusChange: true,
  });

  const edges = data?.judges.edges ?? [];
  const pageInfo = data?.judges.pageInfo;

  const { handleLoadMore } = useInfiniteScroll<JudgesData>({
    hasNextPage: pageInfo?.hasNextPage,
    endCursor: pageInfo?.endCursor,
    loading,
    fetchMore,
    merge: useCallback(
      (prev: JudgesData, incoming: JudgesData) => ({
        judges: {
          ...incoming.judges,
          edges: [...prev.judges.edges, ...incoming.judges.edges],
        },
      }),
      [],
    ),
  });

  // Client-side name filter (the API doesn't support text search on judges)
  const filteredEdges = nameFilter
    ? edges.filter(({ node }) =>
        node.canonicalName.toLowerCase().includes(nameFilter.toLowerCase()),
      )
    : edges;

  return (
    <div className="space-y-6">
      {/* Filter */}
      <div>
        <div className="relative max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <Input
            type="text"
            name="judgeName"
            placeholder="Filter by judge name…"
            value={nameFilter}
            onChange={(e) => setNameFilter(e.target.value)}
            className="pl-9"
            aria-label="Judge name"
          />
        </div>
      </div>

      {/* Table */}
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Judge</TableHead>
              <TableHead>County</TableHead>
              <TableHead>Dept.</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {/* Loading skeleton */}
            {loading && edges.length === 0 && (
              <>
                {Array.from({ length: 8 }).map((_, i) => (
                  <SkeletonRow key={i} />
                ))}
              </>
            )}

            {/* Error */}
            {error && (
              <TableRow>
                <TableCell colSpan={4} className="text-center">
                  <p className="py-4 text-sm text-destructive">
                    Failed to load judges. Please try again.
                  </p>
                </TableCell>
              </TableRow>
            )}

            {/* Empty state */}
            {!loading && !error && filteredEdges.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="text-center">
                  <p className="py-4 text-sm text-muted-foreground">
                    No judges found. Try adjusting your filters.
                  </p>
                </TableCell>
              </TableRow>
            )}

            {/* Data rows */}
            {filteredEdges.map(({ node }) => (
              <TableRow key={node.id}>
                <TableCell className="font-medium">
                  <Link
                    href={`/judges/${node.id}`}
                    className="rounded-sm hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {node.canonicalName}
                  </Link>
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {node.court?.county ?? '\u2014'}
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {node.department ? `Dept. ${node.department}` : '\u2014'}
                </TableCell>
                <TableCell>
                  {!node.isActive && (
                    <Badge variant="secondary">Inactive</Badge>
                  )}
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
        <InfiniteScrollTrigger
          hasNextPage={pageInfo?.hasNextPage ?? false}
          loading={loading}
          onLoadMore={handleLoadMore}
        />
      </div>
    </div>
  );
}
