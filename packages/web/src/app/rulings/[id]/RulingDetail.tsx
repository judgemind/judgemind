'use client';

import Link from 'next/link';
import { buildDownloadUrl, cleanRulingText, FORMAT_LABELS } from '@/lib/display-helpers';
import { sanitizeRulingHtml } from '@/lib/sanitize-html';

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
    <div>
      {/* Linked case */}
      {ruling.case && (
        <section className="mb-6">
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
        <section className="mb-6">
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

      {/* Summary (AI-generated) */}
      {ruling.summary && (
        <section className="mb-6">
          <h2 className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Summary
          </h2>
          <p className="mt-1 text-sm leading-relaxed text-slate-700 dark:text-slate-300">
            {ruling.summary}
          </p>
        </section>
      )}

      {/* Ruling text — prefer formatted HTML, fall back to raw text */}
      {(ruling.rulingTextHtml || ruling.rulingText) && (
        <section className="mb-6">
          <h2 className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Ruling Text
          </h2>
          {ruling.rulingTextHtml ? (
            <div
              className="ruling-content mt-2 text-sm leading-relaxed text-slate-700 dark:text-slate-300"
              dangerouslySetInnerHTML={{
                __html: sanitizeRulingHtml(ruling.rulingTextHtml),
              }}
            />
          ) : (
            <div className="mt-2 space-y-3">
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
        </section>
      )}

      {/* Document download */}
      {ruling.documentId && (
        <section className="mb-6">
          <a
            href={buildDownloadUrl(ruling.documentId)}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-sm font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300"
          >
            Download original document
            {ruling.documentFormat && (
              <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                {FORMAT_LABELS[ruling.documentFormat] ??
                  ruling.documentFormat.toUpperCase()}
              </span>
            )}
          </a>
        </section>
      )}
    </div>
  );
}
