'use client';

import { useState } from 'react';
import { useQuery, gql } from '@apollo/client';
import Link from 'next/link';

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
    <div className="flex animate-pulse gap-4 border-b border-slate-100 px-4 py-4 dark:border-slate-700">
      <div className="flex-1 space-y-2">
        <div className="h-3 w-1/3 rounded bg-slate-200 dark:bg-slate-700" />
        <div className="h-3 w-1/4 rounded bg-slate-200 dark:bg-slate-700" />
      </div>
      <div className="w-16 shrink-0">
        <div className="h-5 rounded bg-slate-200 dark:bg-slate-700" />
      </div>
    </div>
  );
}

export function JudgesList() {
  const [nameFilter, setNameFilter] = useState('');

  const { data, loading, error, fetchMore } = useQuery<JudgesData>(JUDGES_QUERY, {
    variables: {
      first: PAGE_SIZE,
    },
    notifyOnNetworkStatusChange: true,
  });

  const edges = data?.judges.edges ?? [];
  const pageInfo = data?.judges.pageInfo;

  // Client-side name filter (the API doesn't support text search on judges)
  const filteredEdges = nameFilter
    ? edges.filter(({ node }) =>
        node.canonicalName.toLowerCase().includes(nameFilter.toLowerCase()),
      )
    : edges;

  function handleLoadMore() {
    if (!pageInfo?.endCursor) return;
    fetchMore({
      variables: { after: pageInfo.endCursor },
      updateQuery(prev, { fetchMoreResult }) {
        if (!fetchMoreResult) return prev;
        return {
          judges: {
            ...fetchMoreResult.judges,
            edges: [...prev.judges.edges, ...fetchMoreResult.judges.edges],
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
          placeholder="Judge name"
          value={nameFilter}
          onChange={(e) => setNameFilter(e.target.value)}
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500"
        />
      </div>

      {/* Table */}
      <div className="rounded-lg border border-slate-200 dark:border-slate-700">
        {/* Header */}
        <div className="hidden grid-cols-[1fr_10rem_6rem_5rem] gap-4 border-b border-slate-200 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400 sm:grid">
          <span>Judge</span>
          <span>Court</span>
          <span>Dept.</span>
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
            Failed to load judges. Please try again.
          </p>
        )}

        {/* Empty state */}
        {!loading && !error && filteredEdges.length === 0 && (
          <p className="p-8 text-center text-slate-400 dark:text-slate-500">
            No judges found. Try adjusting your filters.
          </p>
        )}

        {/* Rows */}
        {filteredEdges.map(({ node }) => (
          <div
            key={node.id}
            className="grid grid-cols-1 gap-1 border-b border-slate-100 px-4 py-3 last:border-0 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800/50 sm:grid-cols-[1fr_10rem_6rem_5rem] sm:items-center sm:gap-4"
          >
            {/* Judge name */}
            <div className="min-w-0">
              <Link
                href={`/judges/${node.id}`}
                className="block truncate font-medium text-slate-900 hover:text-brand-600 dark:text-slate-100 dark:hover:text-brand-400"
              >
                {node.canonicalName}
              </Link>
            </div>

            {/* Court / County */}
            <span className="text-sm text-slate-500 dark:text-slate-400">
              {node.court?.county ?? '\u2014'}
            </span>

            {/* Department */}
            <span className="text-sm text-slate-500 dark:text-slate-400">
              {node.department ? `Dept. ${node.department}` : '\u2014'}
            </span>

            {/* Active/Inactive badge */}
            <span
              className={`inline-flex w-fit items-center rounded px-2 py-0.5 text-xs font-medium ${
                node.isActive
                  ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
                  : 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300'
              }`}
            >
              {node.isActive ? 'Active' : 'Inactive'}
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
