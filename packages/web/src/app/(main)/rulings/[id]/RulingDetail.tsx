'use client';

import Link from 'next/link';
import { buildDownloadUrl, cleanRulingText, cleanSummary, FORMAT_LABELS, stripMetadataHeaderHtml, type RulingMetadata } from '@/lib/display-helpers';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';

interface RulingProps {
  /** Pre-sanitized HTML from the server component (already run through DOMPurify). */
  sanitizedRulingTextHtml?: string | null;
  ruling: {
    id: string;
    hearingDate: string;
    outcome: string | null;
    motionType: string | null;
    isTentative: boolean;
    department: string | null;
    rulingText: string | null;
    rulingTextHtml: string | null;
    summary: string | null;
    postedAt: string | null;
    documentId: string | null;
    documentFormat: string | null;
    case: {
      id: string;
      caseNumber: string;
      caseTitle: string | null;
    } | null;
    judge: {
      id: string;
      canonicalName: string;
    } | null;
    court: {
      courtName: string;
      county: string;
    } | null;
  };
}

export function RulingDetail({ ruling, sanitizedRulingTextHtml }: RulingProps) {
  // Build metadata context for stripping redundant header boilerplate
  const metadata: RulingMetadata = {
    caseNumber: ruling.case?.caseNumber,
    caseTitle: ruling.case?.caseTitle ?? undefined,
    judgeName: ruling.judge?.canonicalName,
    department: ruling.department ?? undefined,
    hearingDate: ruling.hearingDate,
    motionType: ruling.motionType ?? undefined,
  };
  const displaySummary = ruling.summary ? cleanSummary(ruling.summary) : null;

  return (
    <div className="space-y-6">
      {/* Linked case */}
      {ruling.case && (
        <section>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Case
          </h2>
          <Link
            href={`/cases/${ruling.case.id}`}
            className="mt-1 block rounded-sm text-sm font-medium text-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {ruling.case.caseNumber}
            {ruling.case.caseTitle ? ` \u2014 ${ruling.case.caseTitle}` : ''}
          </Link>
        </section>
      )}

      {/* Judge */}
      {ruling.judge && (
        <section>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Judge
          </h2>
          <Link
            href={`/judges/${ruling.judge.id}`}
            className="mt-1 block rounded-sm text-sm font-medium text-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {ruling.judge.canonicalName}
          </Link>
        </section>
      )}

      {/* Summary (AI-generated) — highlighted in a Card */}
      {displaySummary && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-lg font-semibold text-foreground">
              Summary
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm leading-relaxed text-foreground">
              {displaySummary}
            </p>
          </CardContent>
        </Card>
      )}

      {/* Ruling text in its own Card */}
      {(sanitizedRulingTextHtml || ruling.rulingText) && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-lg font-semibold text-foreground">
              Ruling Text
            </CardTitle>
            {ruling.documentId && (
              <Button variant="outline" size="sm" asChild>
                <a
                  href={buildDownloadUrl(ruling.documentId)}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Download original document
                  {ruling.documentFormat && (
                    <span className="ml-1.5 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                      {FORMAT_LABELS[ruling.documentFormat] ??
                        ruling.documentFormat.toUpperCase()}
                    </span>
                  )}
                </a>
              </Button>
            )}
          </CardHeader>
          <CardContent className="prose prose-slate dark:prose-invert max-w-none">
            {sanitizedRulingTextHtml ? (
              <div
                className="ruling-content text-sm leading-relaxed text-foreground"
                dangerouslySetInnerHTML={{
                  __html: stripMetadataHeaderHtml(sanitizedRulingTextHtml, metadata),
                }}
              />
            ) : (
              <div className="space-y-4">
                {cleanRulingText(ruling.rulingText!, metadata).map((paragraph, idx) => (
                  <p
                    key={idx}
                    className="text-sm leading-relaxed text-foreground"
                  >
                    {paragraph}
                  </p>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Document download — shown standalone when there is no ruling text */}
      {ruling.documentId && !sanitizedRulingTextHtml && !ruling.rulingText && (
        <section>
          <Button variant="outline" size="sm" asChild>
            <a
              href={buildDownloadUrl(ruling.documentId)}
              target="_blank"
              rel="noopener noreferrer"
            >
              Download original document
              {ruling.documentFormat && (
                <span className="ml-1.5 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                  {FORMAT_LABELS[ruling.documentFormat] ??
                    ruling.documentFormat.toUpperCase()}
                </span>
              )}
            </a>
          </Button>
        </section>
      )}
    </div>
  );
}
