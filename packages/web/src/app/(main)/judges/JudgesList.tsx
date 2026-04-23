'use client';

import { useState, useMemo, useCallback, useEffect } from 'react';
import { useQuery, gql } from '@apollo/client';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCountyOptions } from '@/lib/filter-options';
import Link from 'next/link';
import { Search, ArrowUpDown, ArrowUp, ArrowDown, Scale, X } from 'lucide-react';
import { ErrorBanner } from '@/components/ErrorBanner';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
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

const JUDGES_QUERY = gql`
  query Judges($first: Int!, $after: String, $county: String) {
    judges(first: $first, after: $after, county: $county) {
      edges {
        cursor
        node {
          id
          canonicalName
          department
          isActive
          rulingCount
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

const JUDGES_PAGE2_QUERY = gql`
  query JudgesPage2($first: Int!, $after: String!, $county: String) {
    judges(first: $first, after: $after, county: $county) {
      edges {
        cursor
        node {
          id
          canonicalName
          department
          isActive
          rulingCount
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
  rulingCount: number;
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

type SortKey = 'name' | 'county' | 'rulingCount';
type SortDir = 'asc' | 'desc';

/** Extract the likely surname (last whitespace-delimited token, ignoring suffixes). */
function surname(name: string): string {
  const parts = name.trim().split(/\s+/);
  const suffixes = new Set(['Jr.', 'Sr.', 'II', 'III', 'IV', 'V']);
  let idx = parts.length - 1;
  while (idx > 0 && suffixes.has(parts[idx])) idx--;
  return parts[idx].toLowerCase();
}

function sortJudges(
  judges: JudgeNode[],
  sortKey: SortKey,
  sortDir: SortDir,
): JudgeNode[] {
  const sorted = [...judges];
  sorted.sort((a, b) => {
    let cmp = 0;
    switch (sortKey) {
      case 'name':
        cmp = surname(a.canonicalName).localeCompare(surname(b.canonicalName));
        if (cmp === 0) cmp = a.canonicalName.localeCompare(b.canonicalName);
        break;
      case 'county': {
        const ac = a.court?.county ?? '';
        const bc = b.court?.county ?? '';
        cmp = ac.localeCompare(bc);
        if (cmp === 0) cmp = surname(a.canonicalName).localeCompare(surname(b.canonicalName));
        break;
      }
      case 'rulingCount':
        cmp = a.rulingCount - b.rulingCount;
        if (cmp === 0) cmp = surname(a.canonicalName).localeCompare(surname(b.canonicalName));
        break;
    }
    return sortDir === 'asc' ? cmp : -cmp;
  });
  return sorted;
}

function SkeletonRow() {
  return (
    <TableRow>
      <TableCell>
        <Skeleton className="h-4 w-48" />
      </TableCell>
      <TableCell>
        <Skeleton className="h-4 w-32" />
      </TableCell>
      <TableCell className="text-right">
        <Skeleton className="ml-auto h-4 w-12" />
      </TableCell>
    </TableRow>
  );
}

function SortIcon({ column, activeColumn, direction }: {
  column: SortKey;
  activeColumn: SortKey;
  direction: SortDir;
}) {
  if (column !== activeColumn) {
    return <ArrowUpDown className="ml-1 inline h-3 w-3 text-muted-foreground/50" />;
  }
  return direction === 'asc'
    ? <ArrowUp className="ml-1 inline h-3 w-3" />
    : <ArrowDown className="ml-1 inline h-3 w-3" />;
}

export function JudgesList() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [nameFilter, setNameFilter] = useState(searchParams.get('name') ?? '');
  const [countyFilter, setCountyFilter] = useState<string>(searchParams.get('county') ?? 'all');
  const [sortKey, setSortKey] = useState<SortKey>(
    (searchParams.get('sort') as SortKey) || 'name',
  );
  const [sortDir, setSortDir] = useState<SortDir>(
    (searchParams.get('dir') as SortDir) || 'asc',
  );
  const [selectedJudges, setSelectedJudges] = useState<Map<string, string>>(new Map());

  // Sync state from URL when searchParams change (e.g. browser back/forward)
  useEffect(() => {
    setNameFilter(searchParams.get('name') ?? '');
    setCountyFilter(searchParams.get('county') ?? 'all');
    setSortKey((searchParams.get('sort') as SortKey) || 'name');
    setSortDir((searchParams.get('dir') as SortDir) || 'asc');
  }, [searchParams]);

  // Sync state to URL
  const updateUrl = useCallback(() => {
    const params = new URLSearchParams();
    if (nameFilter) params.set('name', nameFilter);
    if (countyFilter && countyFilter !== 'all') params.set('county', countyFilter);
    if (sortKey && sortKey !== 'name') params.set('sort', sortKey);
    if (sortDir && sortDir !== 'asc') params.set('dir', sortDir);
    const search = params.toString();
    router.replace(search ? `/judges?${search}` : '/judges');
  }, [nameFilter, countyFilter, sortKey, sortDir, router]);

  useEffect(() => {
    updateUrl();
  }, [updateUrl]);

  const countyArg = countyFilter !== 'all' ? countyFilter : undefined;

  // TODO(perf): The judges list eagerly loads ALL judges in 3 sequential pages of
  // 100 for client-side sort/filter. At ~264 judges this works, but won't scale
  // past ~500. When the dataset grows, add server-side sortBy/sortDir parameters
  // to the GraphQL query and use incremental pagination instead.
  const { data, loading, error } = useQuery<JudgesData>(JUDGES_QUERY, {
    variables: { first: 100, county: countyArg },
    notifyOnNetworkStatusChange: true,
  });

  const counties = useCountyOptions();

  const page1Info = data?.judges.pageInfo;

  // Fetch page 2 if needed — skip while page 1 is loading to avoid stale cursors
  const { data: page2Data, loading: page2Loading } = useQuery<JudgesData>(JUDGES_PAGE2_QUERY, {
    variables: { first: 100, after: page1Info?.endCursor ?? '', county: countyArg },
    skip: loading || !page1Info?.hasNextPage || !page1Info?.endCursor,
  });

  const page2Info = page2Data?.judges.pageInfo;

  // Fetch page 3 if needed — skip while pages 1 or 2 are loading to avoid stale cursors
  const { data: page3Data, loading: page3Loading } = useQuery<JudgesData>(JUDGES_PAGE2_QUERY, {
    variables: { first: 100, after: page2Info?.endCursor ?? '', county: countyArg },
    skip: loading || page2Loading || !page2Info?.hasNextPage || !page2Info?.endCursor,
  });

  // Combine all pages — exclude stale data from pages still loading after a filter change
  const allEdges = useMemo(() => {
    if (loading) return [];
    const p1 = data?.judges.edges ?? [];
    const p2 = page2Loading ? [] : (page2Data?.judges.edges ?? []);
    const p3 = page3Loading ? [] : (page3Data?.judges.edges ?? []);
    return [...p1, ...p2, ...p3];
  }, [data, page2Data, page3Data, loading, page2Loading, page3Loading]);

  // Apply client-side name filter and sort
  const displayedJudges = useMemo(() => {
    let judges = allEdges.map(({ node }) => node);
    if (nameFilter) {
      const lower = nameFilter.toLowerCase();
      judges = judges.filter((j) => j.canonicalName.toLowerCase().includes(lower));
    }
    return sortJudges(judges, sortKey, sortDir);
  }, [allEdges, nameFilter, sortKey, sortDir]);

  function handleColumnSort(column: SortKey) {
    if (sortKey === column) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(column);
      setSortDir(column === 'rulingCount' ? 'desc' : 'asc');
    }
  }

  function toggleJudgeSelection(judgeId: string, judgeName: string) {
    setSelectedJudges((prev) => {
      const next = new Map(prev);
      if (next.has(judgeId)) {
        next.delete(judgeId);
      } else {
        next.set(judgeId, judgeName);
      }
      return next;
    });
  }

  function removeJudgeSelection(judgeId: string) {
    setSelectedJudges((prev) => {
      const next = new Map(prev);
      next.delete(judgeId);
      return next;
    });
  }

  function handleCompare() {
    const ids = [...selectedJudges.keys()].join(',');
    router.push(`/judges/compare?ids=${ids}`);
  }

  const selectionCount = selectedJudges.size;

  return (
    <div className="space-y-4">
      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative max-w-xs flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <Input
            type="text"
            name="judgeName"
            placeholder="Filter by name…"
            value={nameFilter}
            onChange={(e) => setNameFilter(e.target.value)}
            className="pl-9"
            aria-label="Judge name"
          />
        </div>
        <Select value={countyFilter} onValueChange={setCountyFilter}>
          <SelectTrigger className="w-[180px]" aria-label="Filter by county">
            <SelectValue placeholder="All counties" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All counties</SelectItem>
            {counties.map((c) => (
              <SelectItem key={c} value={c}>{c}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Selection chips */}
      {selectionCount > 0 && (
        <div className="flex flex-wrap items-center gap-2" data-testid="selection-tray">
          {[...selectedJudges.entries()].map(([id, name]) => {
            const sn = surname(name);
            const label = sn.charAt(0).toUpperCase() + sn.slice(1);
            return (
              <span
                key={id}
                className="inline-flex items-center gap-1 rounded-full border bg-muted/50 px-2.5 py-0.5 text-sm"
                data-testid="selection-chip"
              >
                {label}
                <button
                  type="button"
                  className="ml-0.5 rounded-full p-0.5 transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => removeJudgeSelection(id)}
                  aria-label={`Remove ${name} from comparison`}
                >
                  <X className="h-3 w-3" aria-hidden="true" />
                </button>
              </span>
            );
          })}
          <Button
            onClick={handleCompare}
            disabled={selectionCount < 2}
            size="sm"
            variant="outline"
            data-testid="compare-button"
          >
            <Scale className="mr-2 h-4 w-4" aria-hidden="true" />
            Compare ({selectionCount})
          </Button>
        </div>
      )}

      {/* Table */}
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-10">
                <span className="sr-only">Select</span>
              </TableHead>
              <TableHead>
                <button
                  type="button"
                  className="inline-flex items-center font-medium hover:text-foreground"
                  onClick={() => handleColumnSort('name')}
                >
                  Name
                  <SortIcon column="name" activeColumn={sortKey} direction={sortDir} />
                </button>
              </TableHead>
              <TableHead>
                <button
                  type="button"
                  className="inline-flex items-center font-medium hover:text-foreground"
                  onClick={() => handleColumnSort('county')}
                >
                  County
                  <SortIcon column="county" activeColumn={sortKey} direction={sortDir} />
                </button>
              </TableHead>
              <TableHead className="text-right">
                <button
                  type="button"
                  className="inline-flex items-center font-medium hover:text-foreground"
                  onClick={() => handleColumnSort('rulingCount')}
                >
                  Rulings
                  <SortIcon column="rulingCount" activeColumn={sortKey} direction={sortDir} />
                </button>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {/* Loading skeleton */}
            {loading && allEdges.length === 0 && (
              <>
                {Array.from({ length: 10 }).map((_, i) => (
                  <SkeletonRow key={i} />
                ))}
              </>
            )}

            {/* Error */}
            {error && (
              <TableRow>
                <TableCell colSpan={4}>
                  <ErrorBanner message="Failed to load judges." />
                </TableCell>
              </TableRow>
            )}

            {/* Empty state */}
            {!loading && !error && displayedJudges.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="text-center">
                  <p className="py-4 text-sm text-muted-foreground">
                    No judges found. Try adjusting your filters.
                  </p>
                </TableCell>
              </TableRow>
            )}

            {/* Data rows — onClick on <tr> rather than <Link> wrapper
               because table rows with checkboxes cannot wrap in <a>.
               The name <Link> provides keyboard/right-click/cmd-click access. */}
            {displayedJudges.map((node) => {
              const isSelected = selectedJudges.has(node.id);
              return (
                <TableRow
                  key={node.id}
                  className="cursor-pointer"
                  onClick={() => router.push(`/judges/${node.id}`)}
                >
                  <TableCell className="w-10" onClick={(e) => e.stopPropagation()}>
                    <Checkbox
                      checked={isSelected}
                      onCheckedChange={() => toggleJudgeSelection(node.id, node.canonicalName)}
                      aria-label={`Select ${node.canonicalName} for comparison`}
                    />
                  </TableCell>
                  <TableCell>
                    <Link
                      href={`/judges/${node.id}`}
                      className="rounded-sm font-medium hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {node.canonicalName}
                    </Link>
                    {node.department && (
                      <span className="ml-2 text-sm text-muted-foreground">
                        Dept. {node.department}
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {node.court?.county ?? '\u2014'}
                  </TableCell>
                  <TableCell className="text-right tabular-nums text-muted-foreground">
                    {node.rulingCount}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>

      {/* Count */}
      {!loading && displayedJudges.length > 0 && (
        <p className="text-xs text-muted-foreground">
          {displayedJudges.length} {displayedJudges.length === 1 ? 'judge' : 'judges'}
        </p>
      )}
    </div>
  );
}
