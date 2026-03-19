'use client';

import { useState } from 'react';
import { useQuery, gql } from '@apollo/client';
import Link from 'next/link';
import {
  buildDownloadUrl,
  cleanRulingText,
  FORMAT_LABELS,
  formatDate,
  formatLabel,
  formatOutcome,
  groupParties,
  RULING_TEXT_TRUNCATE_LENGTH,
  truncateText,
} from '../../../lib/display-helpers';
import { sanitizeRulingHtml } from '../../rulings/[id]/RulingDetail';

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
        role
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
          isTentative
          department
          judge {
            canonicalName
          }
          rulingText
          rulingTextHtml
          documentId
          documentFormat
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
      role: string | null;
    }>;
  } | null;
}

interface RulingNode {
  id: string;
  hearingDate: string;
  motionType: string | null;
  outcome: string | null;
  isTentative: boolean;
  department: string | null;
  judge: {
    canonicalName: string;
  } | null;
  rulingText: string | null;
  rulingTextHtml: string | null;
  documentId: string | null;
  documentFormat: string | null;
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

function PartyColumn({
  label,
  parties,
}: {
  label: string;
  parties: Array<{ id: string; canonicalName: string; partyType: string | null; role: string | null }>;
}) {
  return (
    <div>
      <h3 className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </h3>
      {parties.length === 0 ? (
        <p className="mt-1 text-sm text-slate-400 dark:text-slate-500">
          None listed
        </p>
      ) : (
        <ul className="mt-1 space-y-1">
          {parties.map((party) => (
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

  const { plaintiffs, defendants, others } = groupParties(caseRecord.parties);

  // Derive unique judges from rulings when the case-level judges list is empty.
  const judgesFromRulings = (() => {
    if (caseRecord.judges.length > 0) return [];
    const seen = new Set<string>();
    const result: Array<{ id: string; canonicalName: string; department: string | null }> = [];
    for (const { node } of edges) {
      if (node.judge && !seen.has(node.judge.canonicalName)) {
        seen.add(node.judge.canonicalName);
        result.push({
          id: node.judge.canonicalName, // no judge id from rulings query
          canonicalName: node.judge.canonicalName,
          department: node.department,
        });
      }
    }
    return result;
  })();

  const displayJudges = caseRecord.judges.length > 0 ? caseRecord.judges : judgesFromRulings;

  return (
    <div>
      {/* Case metadata */}
      <div className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-3">
        {caseRecord.filedAt && (
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Filed Date
            </dt>
            <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
              {formatDate(caseRecord.filedAt)}
            </dd>
          </div>
        )}
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

      {/* Parties — two-column layout (hidden when empty) */}
      {caseRecord.parties.length > 0 && (
        <section className="mt-8">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Parties
          </h2>
          <div className="mt-2 grid grid-cols-1 gap-6 sm:grid-cols-2">
            <PartyColumn label="Plaintiffs" parties={plaintiffs} />
            <PartyColumn label="Defendants" parties={defendants} />
            {others.length > 0 && (
              <PartyColumn label="Other Parties" parties={others} />
            )}
          </div>
        </section>
      )}

      {/* Judges (hidden when empty) */}
      {displayJudges.length > 0 && (
        <section className="mt-8">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Judges
          </h2>
          <ul className="mt-2 space-y-1">
            {displayJudges.map((judge) => (
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
        </section>
      )}

      {/* Rulings */}
      <section className="mt-8">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Rulings
        </h2>

        <div className="mt-3 rounded-lg border border-slate-200 dark:border-slate-700">
          {/* Header row */}
          <div className="hidden grid-cols-[6rem_1fr_10rem_6rem_5.5rem] gap-4 border-b border-slate-200 px-4 py-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:border-slate-700 dark:text-slate-400 sm:grid">
            <span>Date</span>
            <span>Motion</span>
            <span>Judge</span>
            <span>Outcome</span>
            <span>Type</span>
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
              No rulings captured for this case.
            </p>
          )}

          {/* Rows */}
          {edges.map(({ node }) => {
            const isExpanded = expandedRulings.has(node.id);
            const hasText = !!(node.rulingTextHtml || node.rulingText);
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
                  className="grid grid-cols-1 gap-1 px-4 py-3 hover:bg-slate-50 dark:hover:bg-slate-800/50 sm:grid-cols-[6rem_1fr_10rem_6rem_5.5rem] sm:items-center sm:gap-4"
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

                  {/* Tentative / Final badge */}
                  <span
                    className={`inline-flex w-fit items-center rounded px-2 py-0.5 text-xs font-medium ${
                      node.isTentative
                        ? 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200'
                        : 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-200'
                    }`}
                  >
                    {node.isTentative ? 'Tentative' : 'Final'}
                  </span>
                </div>

                {/* Ruling text — prefer formatted HTML, fall back to plain text */}
                {hasText && (
                  <div className="px-4 pb-3">
                    {node.rulingTextHtml && (!isLong || isExpanded) ? (
                      <div
                        className="ruling-content text-sm leading-relaxed text-slate-700 dark:text-slate-300"
                        dangerouslySetInnerHTML={{
                          __html: sanitizeRulingHtml(node.rulingTextHtml),
                        }}
                      />
                    ) : (
                      node.rulingText && (
                        <div className="space-y-3">
                          {(() => {
                            const displayText = isLong && !isExpanded
                              ? truncateText(node.rulingText, RULING_TEXT_TRUNCATE_LENGTH)
                              : node.rulingText;
                            const paragraphs = cleanRulingText(displayText);
                            return paragraphs.map((paragraph, idx) => (
                              <p key={idx} className="text-sm leading-relaxed text-slate-700 dark:text-slate-300">
                                {paragraph}
                              </p>
                            ));
                          })()}
                        </div>
                      )
                    )}
                    <div className="mt-1 flex items-center gap-3">
                      {isLong && (
                        <button
                          onClick={() => toggleRuling(node.id)}
                          className="text-xs font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300"
                        >
                          {isExpanded ? 'Show less' : 'Show more'}
                        </button>
                      )}
                      {node.documentId && (
                        <a
                          href={buildDownloadUrl(node.documentId)}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-xs font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300"
                        >
                          Download original
                          {node.documentFormat && (
                            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                              {FORMAT_LABELS[node.documentFormat] ?? node.documentFormat.toUpperCase()}
                            </span>
                          )}
                        </a>
                      )}
                    </div>
                  </div>
                )}

                {/* Download button when there is no ruling text but document exists */}
                {!hasText && node.documentId && (
                  <div className="px-4 pb-3">
                    <a
                      href={buildDownloadUrl(node.documentId)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-xs font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300"
                    >
                      Download original
                      {node.documentFormat && (
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                          {FORMAT_LABELS[node.documentFormat] ?? node.documentFormat.toUpperCase()}
                        </span>
                      )}
                    </a>
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
