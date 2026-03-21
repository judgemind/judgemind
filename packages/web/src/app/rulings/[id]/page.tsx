import { gql } from '@apollo/client';
import { notFound } from 'next/navigation';
import Link from 'next/link';
import { createApolloClient } from '@/lib/apollo-client';
import { formatDate, formatLabel, formatOutcome, getOutcomeBadgeClass } from '@/lib/display-helpers';
import { sanitizeRulingHtml } from '@/lib/sanitize-html';
import { RulingDetail } from './RulingDetail';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';

const RULING_QUERY = gql`
  query RulingDetail($id: ID!) {
    ruling(id: $id) {
      id
      hearingDate
      outcome
      motionType
      isTentative
      department
      rulingText
      rulingTextHtml
      summary
      postedAt
      documentId
      documentFormat
      case {
        id
        caseNumber
        caseTitle
      }
      judge {
        id
        canonicalName
      }
      court {
        courtName
        county
      }
    }
  }
`;

interface RulingData {
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
  } | null;
}


type Props = { params: { id: string } };

export default async function RulingDetailPage({ params }: Props) {
  const { id } = params;

  let rulingData: RulingData['ruling'] = null;
  try {
    const client = createApolloClient();
    const { data } = await client.query<RulingData>({
      query: RULING_QUERY,
      variables: { id },
    });
    rulingData = data?.ruling ?? null;
  } catch {
    // GraphQL fetch failed — fall through to not found
  }

  if (!rulingData) {
    notFound();
  }

  const motionLabel = rulingData.motionType
    ? formatLabel(rulingData.motionType)
    : 'Ruling';

  // Sanitize ruling HTML in the server component where isomorphic-dompurify
  // can use jsdom natively, then pass the safe HTML to the client component.
  // This avoids bundling jsdom into the client SSR bundle (which causes 500s).
  // Wrapped in try/catch so sanitization failures degrade gracefully to plain
  // text instead of causing HTTP 500 — the HTML comes from our own DB so
  // skipping sanitization is acceptable as a fallback.
  let sanitizedHtml: string | null = null;
  if (rulingData.rulingTextHtml) {
    try {
      sanitizedHtml = sanitizeRulingHtml(rulingData.rulingTextHtml);
    } catch {
      // Sanitization failed (e.g. jsdom not available in serverless env).
      // Fall back to null so the client component uses rulingText instead.
    }
  }

  return (
    <div className="mx-auto max-w-4xl">
      {/* Breadcrumb */}
      <nav className="mb-4 text-sm text-slate-500 dark:text-slate-400" aria-label="Breadcrumb">
        <Link href="/rulings" className="hover:text-brand-600 dark:hover:text-brand-400">
          Rulings
        </Link>
        <span className="mx-2">/</span>
        <span className="text-slate-700 dark:text-slate-200">{motionLabel}</span>
      </nav>

      {/* Heading */}
      <div className="flex flex-wrap items-start gap-3">
        <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">
          {motionLabel}
        </h1>
        <div className="flex flex-wrap gap-2 pt-1">
          {/* Outcome badge */}
          <Badge
            className={getOutcomeBadgeClass(rulingData.outcome)}
          >
            {formatOutcome(rulingData.outcome)}
          </Badge>
          {/* Tentative / Final badge */}
          <Badge
            className={
              rulingData.isTentative
                ? 'bg-amber-100 text-amber-800 border-amber-200 dark:bg-amber-900 dark:text-amber-200 dark:border-amber-800'
                : 'bg-indigo-100 text-indigo-800 border-indigo-200 dark:bg-indigo-900 dark:text-indigo-200 dark:border-indigo-800'
            }
          >
            {rulingData.isTentative ? 'Tentative' : 'Final'}
          </Badge>
        </div>
      </div>

      {/* Structured metadata Card */}
      <Card className="mt-6">
        <CardContent className="p-6">
          <div className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-3">
            {/* Judge */}
            {rulingData.judge && (
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  Judge
                </dt>
                <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
                  {rulingData.judge.canonicalName}
                </dd>
              </div>
            )}

            {/* Court */}
            {rulingData.court && (
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  Court
                </dt>
                <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
                  {rulingData.court.courtName}
                </dd>
              </div>
            )}

            {/* County */}
            {rulingData.court && (
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  County
                </dt>
                <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
                  {rulingData.court.county}
                </dd>
              </div>
            )}

            {/* Department */}
            {rulingData.department && (
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  Department
                </dt>
                <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
                  {rulingData.department}
                </dd>
              </div>
            )}

            {/* Hearing date */}
            <div>
              <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Hearing Date
              </dt>
              <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
                {formatDate(rulingData.hearingDate)}
              </dd>
            </div>

            {/* Motion type */}
            {rulingData.motionType && (
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  Motion Type
                </dt>
                <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
                  {formatLabel(rulingData.motionType)}
                </dd>
              </div>
            )}

            {/* Case number */}
            {rulingData.case && (
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  Case Number
                </dt>
                <dd className="mt-1 text-sm text-slate-900 dark:text-slate-100">
                  {rulingData.case.caseNumber}
                </dd>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Client component handles ruling text, case link, judge link, document download */}
      <div className="mt-6">
        <RulingDetail ruling={rulingData} sanitizedRulingTextHtml={sanitizedHtml} />
      </div>
    </div>
  );
}
