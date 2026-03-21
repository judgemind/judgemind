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
  stripMetadataHeaderHtml,
  type RulingMetadata,
} from '../../../lib/display-helpers';
import { sanitizeRulingHtml } from '@/lib/sanitize-html';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

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

const OUTCOME_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  granted: 'default',
  denied: 'destructive',
  granted_in_part: 'secondary',
  denied_in_part: 'secondary',
};

const PAGE_SIZE = 20;

function SkeletonBlock() {
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-24" />
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="space-y-2">
                <Skeleton className="h-3 w-16" />
                <Skeleton className="h-4 w-24" />
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
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
      <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </h3>
      {parties.length === 0 ? (
        <p className="mt-1 text-sm text-muted-foreground">
          None listed
        </p>
      ) : (
        <ul className="mt-1 space-y-1">
          {parties.map((party) => (
            <li key={party.id} className="text-sm text-foreground">
              {party.canonicalName}
              {party.partyType && (
                <span className="ml-2 text-xs text-muted-foreground">
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

  if (caseLoading) {
    return <SkeletonBlock />;
  }

  if (caseError) {
    return (
      <Card className="border-destructive">
        <CardContent className="py-6 text-center">
          <p className="text-sm text-destructive">
            Failed to load case details. Please try again.
          </p>
        </CardContent>
      </Card>
    );
  }

  const caseRecord = caseData?.case;
  if (!caseRecord) {
    return (
      <Card>
        <CardContent className="py-6 text-center">
          <p className="text-sm text-muted-foreground">
            Case not found.
          </p>
        </CardContent>
      </Card>
    );
  }

  const { plaintiffs, defendants, others } = groupParties(caseRecord.parties);

  const judgesFromRulings = (() => {
    if (caseRecord.judges.length > 0) return [];
    const seen = new Set<string>();
    const result: Array<{ id: string; canonicalName: string; department: string | null }> = [];
    for (const { node } of edges) {
      if (node.judge && !seen.has(node.judge.canonicalName)) {
        seen.add(node.judge.canonicalName);
        result.push({
          id: node.judge.canonicalName,
          canonicalName: node.judge.canonicalName,
          department: node.department,
        });
      }
    }
    return result;
  })();

  const displayJudges = caseRecord.judges.length > 0 ? caseRecord.judges : judgesFromRulings;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Case Details</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-3">
            {caseRecord.filedAt && (
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Filed Date
                </dt>
                <dd className="mt-1 text-sm text-foreground">
                  {formatDate(caseRecord.filedAt)}
                </dd>
              </div>
            )}
            <div>
              <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Court
              </dt>
              <dd className="mt-1 text-sm text-foreground">
                {caseRecord.court?.courtName ?? '\u2014'}
              </dd>
            </div>
            <div>
              <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                County
              </dt>
              <dd className="mt-1 text-sm text-foreground">
                {caseRecord.court?.county ?? '\u2014'}
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      {caseRecord.parties.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Parties</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 gap-6 sm:grid-cols-2">
              <PartyColumn label="Plaintiffs" parties={plaintiffs} />
              <PartyColumn label="Defendants" parties={defendants} />
              {others.length > 0 && (
                <PartyColumn label="Other Parties" parties={others} />
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {displayJudges.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Judges</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1">
              {displayJudges.map((judge) => (
                <li key={judge.id} className="text-sm text-foreground">
                  <Link
                    href={`/judges/${judge.id}`}
                    className="hover:text-primary"
                  >
                    {judge.canonicalName}
                  </Link>
                  {judge.department && (
                    <span className="ml-2 text-xs text-muted-foreground">
                      Dept. {judge.department}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      <section>
        <h2 className="mb-3 text-base font-semibold text-foreground">
          Rulings
        </h2>

        {rulingsLoading && edges.length === 0 && (
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Card key={i}>
                <CardContent className="py-4">
                  <div className="flex items-center gap-4">
                    <Skeleton className="h-4 w-20" />
                    <Skeleton className="h-4 w-32" />
                    <Skeleton className="h-5 w-16" />
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        )}

        {rulingsError && (
          <Card className="border-destructive">
            <CardContent className="py-6 text-center">
              <p className="text-sm text-destructive">
                Failed to load rulings. Please try again.
              </p>
            </CardContent>
          </Card>
        )}

        {!rulingsLoading && !rulingsError && edges.length === 0 && (
          <Card>
            <CardContent className="py-6 text-center">
              <p className="text-muted-foreground">
                No rulings captured for this case.
              </p>
            </CardContent>
          </Card>
        )}

        <div className="space-y-3">
          {edges.map(({ node }) => {
            const isExpanded = expandedRulings.has(node.id);
            const hasText = !!(node.rulingTextHtml || node.rulingText);
            const rulingMetadata: RulingMetadata = {
              caseNumber: caseRecord.caseNumber,
              caseTitle: caseRecord.caseTitle ?? undefined,
              judgeName: node.judge?.canonicalName,
              department: node.department ?? undefined,
              hearingDate: node.hearingDate,
              motionType: node.motionType ?? undefined,
            };

            return (
              <Card key={node.id}>
                <button
                  type="button"
                  className="flex w-full flex-wrap items-center gap-x-4 gap-y-2 px-6 py-4 text-left hover:bg-muted/50"
                  onClick={() => toggleRuling(node.id)}
                  aria-expanded={isExpanded}
                  aria-label={`Ruling from ${formatDate(node.hearingDate)}`}
                >
                  <span className="text-sm font-medium text-foreground">
                    {formatDate(node.hearingDate)}
                  </span>
                  <span className="flex-1 text-sm text-muted-foreground">
                    {node.motionType ? formatLabel(node.motionType) : '\u2014'}
                    {node.department && (
                      <span className="ml-2 text-xs">
                        Dept. {node.department}
                      </span>
                    )}
                  </span>
                  <span className="text-sm text-muted-foreground">
                    {node.judge?.canonicalName ?? ''}
                  </span>
                  <Badge
                    variant={
                      node.outcome
                        ? (OUTCOME_VARIANT[node.outcome] ?? 'outline')
                        : 'outline'
                    }
                  >
                    {formatOutcome(node.outcome)}
                  </Badge>
                  <Badge variant={node.isTentative ? 'secondary' : 'outline'}>
                    {node.isTentative ? 'Tentative' : 'Final'}
                  </Badge>
                  <svg
                    className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform ${isExpanded ? 'rotate-180' : ''}`}
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 20 20"
                    fill="currentColor"
                  >
                    <path fillRule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.168l3.71-3.938a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z" clipRule="evenodd" />
                  </svg>
                </button>
                {isExpanded && (
                  <CardContent className="border-t pt-4">
                    {hasText && (
                      <div>
                        {node.rulingTextHtml ? (
                          <div
                            className="ruling-content text-sm leading-relaxed text-muted-foreground"
                            dangerouslySetInnerHTML={{
                              __html: stripMetadataHeaderHtml(
                                sanitizeRulingHtml(node.rulingTextHtml),
                                rulingMetadata,
                              ),
                            }}
                          />
                        ) : (
                          node.rulingText && (
                            <div className="space-y-3">
                              {cleanRulingText(node.rulingText, rulingMetadata).map((paragraph, idx) => (
                                <p key={idx} className="text-sm leading-relaxed text-muted-foreground">
                                  {paragraph}
                                </p>
                              ))}
                            </div>
                          )
                        )}
                      </div>
                    )}
                    {node.documentId && (
                      <div className={hasText ? 'mt-3' : ''}>
                        <a
                          href={buildDownloadUrl(node.documentId)}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:text-primary/80"
                        >
                          Download original
                          {node.documentFormat && (
                            <Badge variant="secondary" className="ml-1 text-[10px]">
                              {FORMAT_LABELS[node.documentFormat] ?? node.documentFormat.toUpperCase()}
                            </Badge>
                          )}
                        </a>
                      </div>
                    )}
                  </CardContent>
                )}
              </Card>
            );
          })}
        </div>

        {pageInfo?.hasNextPage && (
          <div className="flex justify-center py-4">
            <Button
              variant="outline"
              onClick={handleLoadMore}
              disabled={rulingsLoading}
            >
              {rulingsLoading ? 'Loading\u2026' : 'Load more'}
            </Button>
          </div>
        )}
      </section>
    </div>
  );
}
