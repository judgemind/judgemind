'use client';

import { useState } from 'react';
import { useQuery, gql } from '@apollo/client';
import Link from 'next/link';
import { formatLabel } from '@/lib/display-helpers';

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

const STATUS_BADGE: Record<string, string> = {
  active: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  closed: 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
  dismissed: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
};

function SkeletonRow() {
  return (
    <div className="flex animate-pulse gap-4 border-b border-slate-100 px-4 py-4 dark:border-slate-700">
      <div className="flex-1 space-y-2">
        <div className="h-3 w-1/3 rounded bg-slate-200 dark:bg-slate-700" />
        <div className="h-3 w-1/2 rounded bg-slate-200 dark:bg-slate-700" />
      </div>
      <div className="w-20 shrink-0">
        <div className="h-5 rounded bg-slate-200 dark:bg-slate-700" />
      </div>
    </div>
  );
}

export function CasesList() {
  const [caseNumberFilter, setCaseNumberFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');

  const { data, loading, error, fetchMore } = useQuery<CasesData>(CASES_QUERY, {
    variables: {
      caseStatus: statusFilter || undefined,
      first: PAGE_SIZE,
    },
    notifyOnNetworkStatusChange: true,
  });

  const edges = data?.cases.edges ?? [];
  const pageInfo = data?.cases.pageInfo;

  // Client-side case number filter (the API doesn't support text search on cases)
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
      {/* Filters */}
      <div className="mb-4 flex flex-wrap gap-3">
        <input
          type="text"
          placeholder="Case number or title"
          value={caseNumberFilter}
          onChange={(e) => setCaseNumberFilter(e.target.value)}
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500"
        />
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label="Case status"
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
        >
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="closed">Closed</option>
          <option value="dismissed">Dismissed</option>
        </select>
      </div>

      {/* Table */}
      <div className="rounded-lg border border-slate-200 dark:border-slate-700">
        {/* Header */}
        <div className="hidden grid-cols-[1fr_10rem_8rem_6rem] gap-4 border-b border-slate-200 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400 sm:grid">
          <span>Case</span>
          <span>Court</span>
          <span>Type</span>
          <span>Status</span>
        </div>

        {/* Skeleton */}
        {loading && edges.length === 0 && (
          <>
            {Array.from({ length: 8 }).map((_, i) => (
              <SkeletonRow key={i} />
            ))}
          </>
        )}

        {/* Error */}
        {error && (
          <p className="p-8 text-center text-sm text-red-500 dark:text-red-400">
            Failed to load cases. Please try again.
          </p>
        )}

        {/* Empty state */}
        {!loading && !error && filteredEdges.length === 0 && (
          <p className="p-8 text-center text-slate-400 dark:text-slate-500">
            No cases found. Try adjusting your filters.
          </p>
        )}

        {/* Rows */}
        {filteredEdges.map(({ node }) => (
          <div
            key={node.id}
            className="grid grid-cols-1 gap-1 border-b border-slate-100 px-4 py-3 last:border-0 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800/50 sm:grid-cols-[1fr_10rem_8rem_6rem] sm:items-center sm:gap-4"
          >
            {/* Case number / title */}
            <div className="min-w-0">
              <Link
                href={`/cases/${node.id}`}
                className="block truncate font-medium text-slate-900 hover:text-brand-600 dark:text-slate-100 dark:hover:text-brand-400"
              >
                {node.caseNumber}
              </Link>
              {node.caseTitle && (
                <p className="truncate text-sm text-slate-500 dark:text-slate-400">
                  {node.caseTitle}
                </p>
              )}
            </div>

            {/* Court / County */}
            <span className="text-sm text-slate-500 dark:text-slate-400">
              {node.court?.county ?? '\u2014'}
            </span>

            {/* Case type */}
            <span className="text-sm text-slate-500 dark:text-slate-400">
              {formatLabel(node.caseType)}
            </span>

            {/* Status badge */}
            <span
              className={`inline-flex w-fit items-center rounded px-2 py-0.5 text-xs font-medium ${
                node.caseStatus && STATUS_BADGE[node.caseStatus.toLowerCase()]
                  ? STATUS_BADGE[node.caseStatus.toLowerCase()]
                  : 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300'
              }`}
            >
              {formatLabel(node.caseStatus)}
            </span>
          </div>
        ))}

        {/* Load more */}
        {pageInfo?.hasNextPage && (
          <div className="flex justify-center py-4">
            <button
              onClick={handleLoadMore}
              disabled={loading}
              className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
            >
              {loading ? 'Loading\u2026' : 'Load more'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
