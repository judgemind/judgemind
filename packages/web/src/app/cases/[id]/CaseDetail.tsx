'use client';

import { useState } from 'react';
import { useQuery, gql } from '@apollo/client';
import Link from 'next/link';
import { formatDate, formatOutcome } from '../../rulings/RulingsFeed';

const CASE_QUERY = gql`
  query CaseDetail($id: ID!) {
    case(id: $id) {
      id
      caseNumber
      caseTitle
      caseType
      caseStatus
      filedAt
      court {
        courtName
        county
      }
      judges {
        id
        canonicalName
        department
      }
      parties {
        id
        canonicalName
        partyType
      }
    }
  }
`;

const CASE_RULINGS_QUERY = gql`
  query CaseRulings($caseId: ID!, $first: Int!, $after: String) {
    rulings(caseId: $caseId, first: $first, after: $after) {
      edges {
        cursor
        node {
          id
          hearingDate
          motionType
          outcome
          department
          judge {
            canonicalName
          }
          rulingText
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
`;

interface CaseData {
  case: {
    id: string;
    caseNumber: string;
    caseTitle: string | null;
    caseType: string | null;
    caseStatus: string | null;
    filedAt: string | null;
    court: {
      courtName: string;
      county: string;
    } | null;
    judges: Array<{
      id: string;
      canonicalName: string;
      department: string | null;
    }>;
    parties: Array<{
      id: string;
      canonicalName: string;
      partyType: string | null;
    }>;
  } | null;
}

interface RulingNode {
  id: string;
  hearingDate: string;
  motionType: string | null;
  outcome: string | null;
  department: string | null;
  judge: {
    canonicalName: string;
  } | null;
  rulingText: string | null;
}

interface RulingsData {
  rulings: {
    edges: Array<{ cursor: string; node: RulingNode }>;
    pageInfo: { hasNextPage: boolean; endCursor: string | null };
  };
}

const OUTCOME_BADGE: Record<string, string> = {
  granted: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  denied: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
  granted_in_part:
    'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
  denied_in_part:
    'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
};

const PAGE_SIZE = 20;

/** Number of characters shown before truncation. */
export const RULING_TEXT_TRUNCATE_LENGTH = 500;

/**
 * Truncate text to `maxLen` characters, breaking at the last whitespace
 * boundary before the limit. Returns the original string if it fits.
 */
export function truncateText(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  const truncated = text.slice(0, maxLen);
  const lastSpace = truncated.lastIndexOf(' ');
  // If there's a space in the first half, break there; otherwise hard-cut.
  if (lastSpace > maxLen / 2) {
    return truncated.slice(0, lastSpace) + '\u2026';
  }
  return truncated + '\u2026';
}

/**
 * Known full label mappings (lowercased key -> display label).
 * Checked before generic title-case logic so compound terms render correctly.
 */
const LABEL_MAP: Record<string, string> = {
  anti_slapp: 'Anti-SLAPP',
};

/** Abbreviations that should stay fully uppercase. */
const UPPERCASE_LABEL_WORDS = new Set(['msj', 'mtd', 'mil']);

/** Format a snake_case string to Title Case, preserving known abbreviations. */
export function formatLabel(value: string | null): string {
  if (!value) return '\u2014';
  const key = value.toLowerCase();
  if (LABEL_MAP[key]) return LABEL_MAP[key];
  return key
    .replace(/_/g, ' ')
    .split(' ')
    .map((word) => UPPERCASE_LABEL_WORDS.has(word) ? word.toUpperCase() : (word.charAt(0).toUpperCase() + word.slice(1)))
    .join(' ');
}

function SkeletonBlock() {
  return (
    <div className="animate-pulse space-y-4">
      <div className="h-6 w-2/3 rounded bg-slate-200 dark:bg-slate-700" />
      <div className="h-4 w-1/2 rounded bg-slate-200 dark:bg-slate-700" />
      <div className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="space-y-2">
            <div className="h-3 w-16 rounded bg-slate-200 dark:bg-slate-700" />
            <div className="h-4 w-24 rounded bg-slate-200 dark:bg-slate-700" />
          </div>
        ))}
      </div>
    </div>
  );
}

function SkeletonRow() {
  return (
    <div className="flex animate-pulse gap-4 border-b border-slate-100 px-4 py-4 dark:border-slate-700">
      <div className="w-24 shrink-0">
        <div className="h-3 w-16 rounded bg-slate-200 dark:bg-slate-700" />
      </div>
      <div className="flex-1 space-y-2">
        <div className="h-3 w-1/3 rounded bg-slate-200 dark:bg-slate-700" />
      </div>
      <div className="w-20 shrink-0">
        <div className="h-5 rounded bg-slate-200 dark:bg-slate-700" />
      </div>
    </div>
  );
}

export function CaseDetail({ caseId }: { caseId: string }) {
  const {
    data: caseData,
    loading: caseLoading,
    error: caseError,
  } = useQuery<CaseData>(CASE_QUERY, {
    variables: { id: caseId },
  });

  const {
    data: rulingsData,
    loading: rulingsLoading,
    error: rulingsError,
    fetchMore,
  } = useQuery<RulingsData>(CASE_RULINGS_QUERY, {
    variables: { caseId, first: PAGE_SIZE },
    notifyOnNetworkStatusChange: true,
  });

  const [expandedRulings, setExpandedRulings] = useState<Set<string>>(
    new Set(),
  );

  function toggleRuling(id: string) {
    setExpandedRulings((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  const edges = rulingsData?.rulings.edges ?? [];
  const pageInfo = rulingsData?.rulings.pageInfo;

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

  // Loading state
  if (caseLoading) {
    return <SkeletonBlock />;
  }

  // Error state
  if (caseError) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-6 text-center dark:border-red-800 dark:bg-red-900/20">
        <p className="text-sm text-red-600 dark:text-red-400">
          Failed to load case details. Please try again.
        </p>
      </div>
    );
  }

  // Not found
  const caseRecord = caseData?.case;
  if (!caseRecord) {
    return (
      <div className="rounded-lg border border-slate-200 p-6 text-center dark:border-slate-700">
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Case not found.
        </p>
      </div>
    );
  }

  return (
    <div>
      {/* Case metadata */}
      <div className="mt-6 grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-3">
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Case Type
          </dt>
          <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
            {formatLabel(caseRecord.caseType)}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Status
          </dt>
          <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
            {formatLabel(caseRecord.caseStatus)}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Filed Date
          </dt>
          <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
            {caseRecord.filedAt ? formatDate(caseRecord.filedAt) : '\u2014'}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Court
          </dt>
          <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
            {caseRecord.court?.courtName ?? '\u2014'}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            County
          </dt>
          <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
            {caseRecord.court?.county ?? '\u2014'}
          </dd>
        </div>
      </div>

      {/* Judges */}
      <section className="mt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Judges
        </h2>
        {caseRecord.judges.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400 dark:text-slate-500">
            No judges assigned.
          </p>
        ) : (
          <ul className="mt-2 space-y-1">
            {caseRecord.judges.map((judge) => (
              <li key={judge.id} className="text-sm text-slate-900 dark:text-slate-100">
                <Link
                  href={`/judges/${judge.id}`}
                  className="hover:text-brand-600 dark:hover:text-brand-400"
                >
                  {judge.canonicalName}
                </Link>
                {judge.department && (
                  <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
                    Dept. {judge.department}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Parties */}
      <section className="mt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Parties
        </h2>
        {caseRecord.parties.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400 dark:text-slate-500">
            No parties listed.
          </p>
        ) : (
          <ul className="mt-2 space-y-1">
            {caseRecord.parties.map((party) => (
              <li key={party.id} className="text-sm text-slate-900 dark:text-slate-100">
                {party.canonicalName}
                {party.partyType && (
                  <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
                    ({formatLabel(party.partyType)})
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Rulings */}
      <section className="mt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Rulings
        </h2>

        <div className="mt-3 rounded-lg border border-slate-200 dark:border-slate-700">
          {/* Header row */}
          <div className="hidden grid-cols-[6rem_1fr_10rem_6rem] gap-4 border-b border-slate-200 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400 sm:grid">
            <span>Date</span>
            <span>Motion</span>
            <span>Judge</span>
            <span>Outcome</span>
          </div>

          {/* Skeleton */}
          {rulingsLoading && edges.length === 0 && (
            <>
              {Array.from({ length: 5 }).map((_, i) => (
                <SkeletonRow key={i} />
              ))}
            </>
          )}

          {/* Error */}
          {rulingsError && (
            <p className="p-8 text-center text-sm text-red-500 dark:text-red-400">
              Failed to load rulings. Please try again.
            </p>
          )}

          {/* Empty state */}
          {!rulingsLoading && !rulingsError && edges.length === 0 && (
            <p className="p-8 text-center text-slate-400 dark:text-slate-500">
              No rulings found for this case.
            </p>
          )}

          {/* Rows */}
          {edges.map(({ node }) => {
            const isExpanded = expandedRulings.has(node.id);
            const isLong =
              !!node.rulingText &&
              node.rulingText.length > RULING_TEXT_TRUNCATE_LENGTH;

            return (
              <div
                key={node.id}
                className="border-b border-slate-100 last:border-0 dark:border-slate-700"
              >
                {/* Metadata row */}
                <div
                  className="grid grid-cols-1 gap-1 px-4 py-3 hover:bg-slate-50 dark:hover:bg-slate-800/50 sm:grid-cols-[6rem_1fr_10rem_6rem] sm:items-center sm:gap-4"
                >
                  {/* Date */}
                  <span className="text-xs text-slate-500 dark:text-slate-400">
                    {formatDate(node.hearingDate)}
                  </span>

                  {/* Motion type */}
                  <span className="text-sm text-slate-900 dark:text-slate-100">
                    {node.motionType ? formatLabel(node.motionType) : '\u2014'}
                    {node.department && (
                      <span className="ml-2 text-xs text-slate-500 dark:text-slate-400">
                        Dept. {node.department}
                      </span>
                    )}
                  </span>

                  {/* Judge */}
                  <span className="text-sm text-slate-700 dark:text-slate-300">
                    {node.judge?.canonicalName ?? '\u2014'}
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

                {/* Ruling text */}
                {node.rulingText && (
                  <div className="px-4 pb-3">
                    <pre className="whitespace-pre-wrap text-sm leading-relaxed text-slate-700 dark:text-slate-300">
                      {isLong && !isExpanded
                        ? truncateText(
                            node.rulingText,
                            RULING_TEXT_TRUNCATE_LENGTH,
                          )
                        : node.rulingText}
                    </pre>
                    {isLong && (
                      <button
                        onClick={() => toggleRuling(node.id)}
                        className="mt-1 text-xs font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300"
                      >
                        {isExpanded ? 'Show less' : 'Show more'}
                      </button>
                    )}
                  </div>
                )}
              </div>
            );
          })}

          {/* Load more */}
          {pageInfo?.hasNextPage && (
            <div className="flex justify-center py-4">
              <button
                onClick={handleLoadMore}
                disabled={rulingsLoading}
                className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
              >
                {rulingsLoading ? 'Loading\u2026' : 'Load more'}
              </button>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
