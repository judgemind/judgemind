'use client';

import Link from 'next/link';
import { buildDownloadUrl, cleanRulingText, FORMAT_LABELS } from '@/lib/display-helpers';
import { sanitizeRulingHtml } from '@/lib/sanitize-html';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';

interface RulingProps {
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

export function RulingDetail({ ruling }: RulingProps) {
  return (
    <div className="space-y-6">
      {/* Linked case */}
      {ruling.case && (
        <section>
          <h2 className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Case
          </h2>
          <Link
            href={`/cases/${ruling.case.id}`}
            className="mt-1 block text-sm font-medium text-slate-900 hover:text-brand-600 dark:text-slate-100 dark:hover:text-brand-400"
          >
            {ruling.case.caseNumber}
            {ruling.case.caseTitle ? ` \u2014 ${ruling.case.caseTitle}` : ''}
          </Link>
        </section>
      )}

      {/* Judge */}
      {ruling.judge && (
        <section>
          <h2 className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Judge
          </h2>
          <Link
            href={`/judges/${ruling.judge.id}`}
            className="mt-1 block text-sm font-medium text-slate-900 hover:text-brand-600 dark:text-slate-100 dark:hover:text-brand-400"
          >
            {ruling.judge.canonicalName}
          </Link>
        </section>
      )}

      {/* Summary (AI-generated) — highlighted in a Card */}
      {ruling.summary && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Summary
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm leading-relaxed text-slate-700 dark:text-slate-300">
              {ruling.summary}
            </p>
          </CardContent>
        </Card>
      )}

      {/* Ruling text in its own Card */}
      {(ruling.rulingTextHtml || ruling.rulingText) && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Ruling Text
            </CardTitle>
          </CardHeader>
          <CardContent className="prose prose-slate dark:prose-invert max-w-none">
            {ruling.rulingTextHtml ? (
              <div
                className="ruling-content text-sm leading-relaxed text-slate-700 dark:text-slate-300"
                dangerouslySetInnerHTML={{
                  __html: sanitizeRulingHtml(ruling.rulingTextHtml),
                }}
              />
            ) : (
              <div className="space-y-3">
                {cleanRulingText(ruling.rulingText!).map((paragraph, idx) => (
                  <p
                    key={idx}
                    className="text-sm leading-relaxed text-slate-700 dark:text-slate-300"
                  >
                    {paragraph}
                  </p>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Document download */}
      {ruling.documentId && (
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
