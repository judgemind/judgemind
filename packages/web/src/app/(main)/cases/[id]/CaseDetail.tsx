'use client';

import { useState, useCallback } from 'react';
import { useQuery, gql } from '@apollo/client';
import { ChevronDown } from 'lucide-react';
import {
  buildDocumentContentUrl,
  buildDownloadUrl,
  cleanRulingText,
  FORMAT_LABELS,
  formatDate,
  formatLabel,
  formatOutcome,
  getOutcomeBadgeVariant,
  getOutcomeBadgeListClass,
  groupParties,
  stripMetadataHeaderHtml,
  type RulingMetadata,
} from '@/lib/display-helpers';
import { sanitizeRulingHtml } from '@/lib/sanitize-html';
import { SECTION_HEADING, SECTION_LABEL } from '@/lib/typography';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
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
      parties {
        id
        canonicalName
        partyType
        role
      }
      judges {
        id
        canonicalName
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
    parties: Array<{
      id: string;
      canonicalName: string;
      partyType: string | null;
      role: string | null;
    }>;
    judges: Array<{
      id: string;
      canonicalName: string;
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

/** State for the HTML document viewer, per ruling. */
type ViewerState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'loaded'; html: string }
  | { status: 'error'; message: string };

const PAGE_SIZE = 20;

function SkeletonBlock() {
  return (
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
  );
}

function PartyList({
  label,
  parties,
}: {
  label: string;
  parties: Array<{ id: string; canonicalName: string; partyType: string | null; role: string | null }>;
}) {
  return (
    <div>
      <h3 className={SECTION_LABEL}>
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

/**
 * Parties header: displays plaintiffs, defendants, other parties, and filed date
 * in the case header area (above rulings), not in a sidebar.
 */
function PartiesHeader({
  plaintiffs,
  defendants,
  others,
  filedAt,
}: {
  plaintiffs: Array<{ id: string; canonicalName: string; partyType: string | null; role: string | null }>;
  defendants: Array<{ id: string; canonicalName: string; partyType: string | null; role: string | null }>;
  others: Array<{ id: string; canonicalName: string; partyType: string | null; role: string | null }>;
  filedAt: string | null;
}) {
  const hasParties = plaintiffs.length > 0 || defendants.length > 0 || others.length > 0;

  if (!hasParties && !filedAt) return null;

  return (
    <div className="flex flex-wrap gap-x-8 gap-y-4" data-testid="parties-header">
      {hasParties && (
        <>
          <PartyList label="Plaintiffs" parties={plaintiffs} />
          <PartyList label="Defendants" parties={defendants} />
          {others.length > 0 && (
            <PartyList label="Other Parties" parties={others} />
          )}
        </>
      )}
      {filedAt && (
        <div>
          <h3 className={SECTION_LABEL}>Filed Date</h3>
          <p className="mt-1 text-sm text-foreground">
            {formatDate(filedAt)}
          </p>
        </div>
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

  const [viewerStates, setViewerStates] = useState<Record<string, ViewerState>>(
    {},
  );

  const handleViewOriginal = useCallback(async (rulingId: string, documentId: string) => {
    const current = viewerStates[rulingId];

    if (current?.status === 'loaded') {
      // Toggle off — hide the viewer
      setViewerStates((prev) => ({ ...prev, [rulingId]: { status: 'idle' } }));
      return;
    }

    setViewerStates((prev) => ({ ...prev, [rulingId]: { status: 'loading' } }));

    try {
      const url = buildDocumentContentUrl(documentId);
      const response = await fetch(url);

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        const errorMessage = errorData?.error ?? `Failed to load document (HTTP ${response.status})`;
        setViewerStates((prev) => ({ ...prev, [rulingId]: { status: 'error', message: errorMessage } }));
        return;
      }

      const html = await response.text();
      setViewerStates((prev) => ({ ...prev, [rulingId]: { status: 'loaded', html } }));
    } catch {
      setViewerStates((prev) => ({
        ...prev,
        [rulingId]: { status: 'error', message: 'Failed to load document. Please try again.' },
      }));
    }
  }, [viewerStates]);

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
  const caseJudgeNames = new Set((caseRecord.judges ?? []).map((j) => j.canonicalName));

  const rulingsSection = (
    <section>
      <h2 className={`mb-3 ${SECTION_HEADING}`}>
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
        <p className="py-6 text-center text-muted-foreground">
          No rulings captured for this case.
        </p>
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
                className="flex w-full flex-wrap items-center gap-x-4 gap-y-2 rounded-md px-6 py-4 text-left hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                onClick={() => toggleRuling(node.id)}
                aria-expanded={isExpanded}
                aria-label={`Ruling from ${formatDate(node.hearingDate)}`}
              >
                <span className="text-sm font-medium text-foreground">
                  {formatDate(node.hearingDate)}
                </span>
                <span className="flex-1 text-sm text-muted-foreground">
                  {node.motionType ? formatLabel(node.motionType) : '\u2014'}
                </span>
                {node.judge?.canonicalName && !caseJudgeNames.has(node.judge.canonicalName) && (
                  <span className="text-sm text-muted-foreground" data-testid="ruling-judge">
                    {node.judge.canonicalName}
                  </span>
                )}
                <Badge
                  variant={getOutcomeBadgeVariant(node.outcome)}
                  className={getOutcomeBadgeListClass(node.outcome)}
                >
                  {formatOutcome(node.outcome)}
                </Badge>
                <Badge variant={node.isTentative ? 'secondary' : 'outline'}>
                  {node.isTentative ? 'Tentative' : 'Final'}
                </Badge>
                <ChevronDown
                  className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none ${isExpanded ? 'rotate-180' : ''}`}
                  aria-hidden="true"
                />
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
                          <div className="space-y-4">
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
                  {node.documentId && (() => {
                    const viewerState = viewerStates[node.id] ?? { status: 'idle' };
                    const isHtmlDocument = node.documentFormat === 'html';
                    return (
                      <div className={hasText ? 'mt-3' : ''}>
                        <div className="flex items-center gap-3">
                          {isHtmlDocument && (
                            <button
                              type="button"
                              className="inline-flex items-center gap-1 rounded-sm text-xs font-medium text-primary hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              onClick={() => handleViewOriginal(node.id, node.documentId!)}
                              disabled={viewerState.status === 'loading'}
                              data-testid={`view-original-button-${node.id}`}
                            >
                              {viewerState.status === 'loading'
                                ? 'Loading\u2026'
                                : viewerState.status === 'loaded'
                                  ? 'Hide original'
                                  : 'View original'}
                            </button>
                          )}
                          <a
                            href={buildDownloadUrl(node.documentId)}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 rounded-sm text-xs font-medium text-primary hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            Download original
                            {node.documentFormat && (
                              <Badge variant="secondary" className="ml-1 text-[10px]">
                                {FORMAT_LABELS[node.documentFormat] ?? node.documentFormat.toUpperCase()}
                              </Badge>
                            )}
                          </a>
                        </div>

                        {/* Original HTML document viewer (sandboxed iframe) */}
                        {viewerState.status === 'loaded' && (
                          <div className="mt-3">
                            <iframe
                              srcDoc={viewerState.html}
                              sandbox=""
                              className="w-full rounded border"
                              style={{ height: '600px' }}
                              title="Original court document"
                              data-testid={`original-document-iframe-${node.id}`}
                            />
                          </div>
                        )}

                        {/* Error state */}
                        {viewerState.status === 'error' && (
                          <p className="mt-3 text-sm text-destructive" data-testid={`viewer-error-${node.id}`}>
                            {viewerState.message}
                          </p>
                        )}
                      </div>
                    );
                  })()}
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
  );

  return (
    <div className="space-y-6">
      <PartiesHeader
        plaintiffs={plaintiffs}
        defendants={defendants}
        others={others}
        filedAt={caseRecord.filedAt}
      />
      {rulingsSection}
    </div>
  );
}
