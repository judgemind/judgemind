'use client';

import { useState } from 'react';
import { useQuery, gql } from '@apollo/client';
import Link from 'next/link';

const RULINGS_QUERY = gql`
  query Rulings(
    $county: String
    $dateFrom: String
    $dateTo: String
    $first: Int!
    $after: String
  ) {
    rulings(
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
          hearingDate
          outcome
          motionType
          department
          case {
            id
            caseNumber
            caseTitle
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

/** Format an ISO 8601 date string as a short readable date (e.g. "Mar 5, 2026"). */
export function formatDate(iso: string): string {
  const d = new Date(iso + 'T00:00:00Z');
  return d.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

/** Convert a snake_case outcome code to a human-readable label. */
export function formatOutcome(outcome: string | null): string {
  if (!outcome) return 'Not classified';
  return outcome
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Known full motion type mappings (lowercased key -> display label).
 * Checked before the generic title-case logic so compound terms like
 * "anti_slapp" render correctly.
 */
const MOTION_TYPE_MAP: Record<string, string> = {
  anti_slapp: 'Anti-SLAPP',
};

/** Abbreviations that should stay fully uppercase. */
const UPPERCASE_MOTION_WORDS = new Set(['msj', 'mtd', 'mil']);

/** Small words that stay lowercase unless they are the first word. */
const LOWERCASE_WORDS = new Set(['to', 'for', 'of', 'in', 'on', 'the', 'a', 'an']);

/** Format a motion type for display, returning a placeholder for null values. */
export function formatMotionType(motionType: string | null): string {
  if (!motionType) return 'Not classified';
  const key = motionType.toLowerCase();
  if (MOTION_TYPE_MAP[key]) return MOTION_TYPE_MAP[key];
  return key
    .replace(/_/g, ' ')
    .split(' ')
    .map((word, i) => {
      if (UPPERCASE_MOTION_WORDS.has(word)) return word.toUpperCase();
      if (i > 0 && LOWERCASE_WORDS.has(word)) return word;
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(' ');
}

/** Format a judge name for display, returning a placeholder for null values. */
export function formatJudgeName(
  judge: { canonicalName: string } | null,
): string {
  if (!judge) return 'Unknown judge';
  return judge.canonicalName;
}

const PAGE_SIZE = 20;

const OUTCOME_BADGE: Record<string, string> = {
  granted: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  denied: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
  granted_in_part: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
  denied_in_part: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
};

function SkeletonRow() {
  return (
    <div className="flex animate-pulse gap-4 border-b border-slate-100 px-4 py-4 dark:border-slate-700">
      <div className="w-24 shrink-0 space-y-1">
        <div className="h-3 w-16 rounded bg-slate-200 dark:bg-slate-700" />
      </div>
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

export function RulingsFeed() {
  const [county, setCounty] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');

  const { data, loading, error, fetchMore } = useQuery<RulingsData>(RULINGS_QUERY, {
    variables: {
      county: county || undefined,
      dateFrom: dateFrom || undefined,
      dateTo: dateTo || undefined,
      first: PAGE_SIZE,
    },
    notifyOnNetworkStatusChange: true,
  });

  const edges = data?.rulings.edges ?? [];
  const pageInfo = data?.rulings.pageInfo;

  function handleLoadMore() {
    if (!pageInfo?.endCursor) return;
    fetchMore({
      variables: { after: pageInfo.endCursor },
      updateQuery(prev, { fetchMoreResult }) {
        if (!fetchMoreResult) return prev;
        return {
          rulings: {
            ...fetchMoreResult.rulings,
            edges: [...prev.rulings.edges, ...fetchMoreResult.rulings.edges],
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
          placeholder="County (e.g. Los Angeles)"
          value={county}
          onChange={(e) => setCounty(e.target.value)}
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:placeholder-slate-500"
        />
        <input
          type="date"
          value={dateFrom}
          onChange={(e) => setDateFrom(e.target.value)}
          title="Hearings from"
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
        />
        <input
          type="date"
          value={dateTo}
          onChange={(e) => setDateTo(e.target.value)}
          title="Hearings to"
          className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
        />
      </div>

      {/* Table */}
      <div className="rounded-lg border border-slate-200 dark:border-slate-700">
        {/* Header */}
        <div className="hidden grid-cols-[6rem_1fr_10rem_10rem_6rem] gap-4 border-b border-slate-200 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400 sm:grid">
          <span>Date</span>
          <span>Case / Court</span>
          <span>Judge</span>
          <span>Motion</span>
          <span>Outcome</span>
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
            Failed to load rulings. Please try again.
          </p>
        )}

        {/* Empty state */}
        {!loading && !error && edges.length === 0 && (
          <p className="p-8 text-center text-slate-400 dark:text-slate-500">
            No rulings found. Try adjusting your filters, or check back after scrapers have run.
          </p>
        )}

        {/* Rows */}
        {edges.map(({ node }) => (
          <div
            key={node.id}
            className="grid grid-cols-1 gap-1 border-b border-slate-100 px-4 py-3 last:border-0 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800/50 sm:grid-cols-[6rem_1fr_10rem_10rem_6rem] sm:items-center sm:gap-4"
          >
            {/* Date */}
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {formatDate(node.hearingDate)}
            </span>

            {/* Case / Court */}
            <div className="min-w-0">
              {node.case ? (
                <Link
                  href={`/cases/${node.case.id}`}
                  className="block truncate font-medium text-slate-900 hover:text-brand-600 dark:text-slate-100 dark:hover:text-brand-400"
                >
                  {node.case.caseNumber}
                  {node.case.caseTitle ? ` — ${node.case.caseTitle}` : ''}
                </Link>
              ) : (
                <span className="text-slate-400">—</span>
              )}
              {node.case?.court && (
                <p className="truncate text-xs text-slate-500 dark:text-slate-400">
                  {node.case.court.county} · {node.department ?? 'Dept unknown'}
                </p>
              )}
            </div>

            {/* Judge */}
            <span className="text-sm text-slate-700 dark:text-slate-300">
              {formatJudgeName(node.judge)}
            </span>

            {/* Motion type */}
            <span className="text-sm text-slate-500 dark:text-slate-400">
              {formatMotionType(node.motionType)}
            </span>

            {/* Outcome badge */}
            <span
              className={`inline-flex w-fit items-center rounded px-2 py-0.5 text-xs font-medium ${
                node.outcome && OUTCOME_BADGE[node.outcome]
                  ? OUTCOME_BADGE[node.outcome]
                  : 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300'
              }`}
            >
              {formatOutcome(node.outcome)}
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
              {loading ? 'Loading…' : 'Load more'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
