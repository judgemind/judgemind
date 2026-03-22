'use client';

import { useState, useCallback, useEffect } from 'react';
import { useQuery, gql } from '@apollo/client';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { formatLabel } from '@/lib/display-helpers';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
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

const CASES_QUERY = gql`
  query Cases(
    $caseStatus: String
    $caseType: String
    $first: Int!
    $after: String
  ) {
    cases(
      caseStatus: $caseStatus
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

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  closed: 'secondary',
  dismissed: 'destructive',
};

function SkeletonRow() {
  return (
    <TableRow>
      <TableCell>
        <div className="flex animate-pulse motion-reduce:animate-none flex-col gap-1">
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-3 w-48" />
        </div>
      </TableCell>
      <TableCell><Skeleton className="h-3 w-24" /></TableCell>
      <TableCell><Skeleton className="h-3 w-16" /></TableCell>
      <TableCell><Skeleton className="h-5 w-14" /></TableCell>
    </TableRow>
  );
}

export function CasesList() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [caseNumberFilter, setCaseNumberFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState(searchParams.get('status') ?? '');
  const [typeFilter, setTypeFilter] = useState(searchParams.get('caseType') ?? '');

  useEffect(() => {
    setStatusFilter(searchParams.get('status') ?? '');
    setTypeFilter(searchParams.get('caseType') ?? '');
  }, [searchParams]);

  const updateUrl = useCallback(() => {
    const params = new URLSearchParams();
    if (statusFilter) params.set('status', statusFilter);
    if (typeFilter) params.set('caseType', typeFilter);
    const search = params.toString();
    router.replace(search ? `/cases?${search}` : '/cases');
  }, [statusFilter, typeFilter, router]);

  useEffect(() => {
    updateUrl();
  }, [updateUrl]);

  const { data, loading, error, fetchMore } = useQuery<CasesData>(CASES_QUERY, {
    variables: {
      caseStatus: statusFilter || undefined,
      caseType: typeFilter || undefined,
      first: PAGE_SIZE,
    },
    notifyOnNetworkStatusChange: true,
  });

  const edges = data?.cases.edges ?? [];
  const pageInfo = data?.cases.pageInfo;

  const filteredEdges = caseNumberFilter
    ? edges.filter(
        ({ node }) =>
          node.caseNumber.toLowerCase().includes(caseNumberFilter.toLowerCase()) ||
          (node.caseTitle?.toLowerCase().includes(caseNumberFilter.toLowerCase()) ?? false),
      )
    : edges;

  function handleLoadMore() {
    if (!pageInfo?.endCursor) return;
    fetchMore({
      variables: { after: pageInfo.endCursor },
      updateQuery(prev, { fetchMoreResult }) {
        if (!fetchMoreResult) return prev;
        return {
          cases: {
            ...fetchMoreResult.cases,
            edges: [...prev.cases.edges, ...fetchMoreResult.cases.edges],
          },
        };
      },
    });
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap gap-3">
        <Input
          type="text"
          name="caseFilter"
          placeholder="Case number or title\u2026"
          value={caseNumberFilter}
          onChange={(e) => setCaseNumberFilter(e.target.value)}
          className="w-auto"
          aria-label="Case number or title"
        />
        <select
          name="caseStatus"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label="Case status"
          className="rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
        >
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="closed">Closed</option>
          <option value="dismissed">Dismissed</option>
        </select>
        <select
          name="caseType"
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          aria-label="Case type"
          className="rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
        >
          <option value="">All types</option>
          {CASE_TYPES.map((ct) => (
            <option key={ct} value={ct}>
              {formatLabel(ct)}
            </option>
          ))}
        </select>
      </div>

      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Case</TableHead>
              <TableHead>Court</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Status</TableHead>
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
                <TableCell colSpan={4} className="text-center">
                  <p className="py-4 text-sm text-destructive">
                    Failed to load cases. Please try again.
                  </p>
                </TableCell>
              </TableRow>
            )}

            {!loading && !error && filteredEdges.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="text-center">
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
                    className="block truncate font-medium text-foreground hover:text-primary"
                  >
                    {node.caseNumber}
                  </Link>
                  {node.caseTitle && (
                    <p className="truncate text-sm text-muted-foreground">
                      {node.caseTitle}
                    </p>
                  )}
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {node.court?.county ?? '\u2014'}
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {formatLabel(node.caseType)}
                </TableCell>
                <TableCell>
                  <Badge
                    variant={
                      node.caseStatus
                        ? (STATUS_VARIANT[node.caseStatus.toLowerCase()] ?? 'outline')
                        : 'outline'
                    }
                  >
                    {formatLabel(node.caseStatus)}
                  </Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {pageInfo?.hasNextPage && (
          <div className="flex justify-center py-4">
            <Button
              variant="outline"
              onClick={handleLoadMore}
              disabled={loading}
            >
              {loading ? 'Loading\u2026' : 'Load more'}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
