import { gql } from '@apollo/client';
import { notFound } from 'next/navigation';
import Link from 'next/link';
import { createApolloClient } from '@/lib/apollo-client';
import { formatDate, formatLabel, formatOutcome, getOutcomeBadgeClass } from '@/lib/display-helpers';
import { PAGE_TITLE, SECTION_LABEL } from '@/lib/typography';
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
    <div className="mx-auto max-w-4xl space-y-6">
      {/* Breadcrumb */}
      <nav className="text-sm text-muted-foreground" aria-label="Breadcrumb">
        <Link href="/rulings" className="hover:text-primary">
          Rulings
        </Link>
        <span className="mx-2">/</span>
        <span className="text-foreground">{motionLabel}</span>
      </nav>

      {/* Heading + subtitle */}
      <div>
        <div className="flex flex-wrap items-start gap-3">
          <h1 className={PAGE_TITLE}>
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

        {/* Subtitle line 1: Judge · Court (combined with county) */}
        {(rulingData.judge || rulingData.court) && (
          <p className="mt-1 text-sm text-muted-foreground">
            {rulingData.judge && (
              <Link
                href={`/judges/${rulingData.judge.id}`}
                className="hover:text-primary hover:underline"
              >
                {rulingData.judge.canonicalName}
              </Link>
            )}
            {rulingData.judge && rulingData.court && (
              <span aria-hidden="true"> &middot; </span>
            )}
            {rulingData.court && (
              <Link
                href={`/rulings?county=${encodeURIComponent(rulingData.court.county)}`}
                className="hover:text-primary hover:underline"
              >
                {rulingData.court.courtName}
                {!rulingData.court.courtName.toLowerCase().includes(rulingData.court.county.toLowerCase()) && (
                  <>, {rulingData.court.county}</>
                )}
              </Link>
            )}
          </p>
        )}

        {/* Subtitle line 2: Case number · Hearing date */}
        {(rulingData.case || rulingData.hearingDate) && (
          <p className="mt-0.5 text-sm text-muted-foreground">
            {rulingData.case && (
              <Link
                href={`/cases/${rulingData.case.id}`}
                className="hover:text-primary hover:underline"
              >
                Case {rulingData.case.caseNumber}
              </Link>
            )}
            {rulingData.case && rulingData.hearingDate && (
              <span aria-hidden="true"> &middot; </span>
            )}
            {rulingData.hearingDate && formatDate(rulingData.hearingDate)}
          </p>
        )}
      </div>

      {/* Structured metadata Card — only rendered when department is present.
          Motion Type is already shown in the page heading (motionLabel), so it
          is not duplicated here. */}
      {rulingData.department && (
        <Card>
          <CardContent className="p-6">
            <div className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-3">
              <div>
                <dt className={SECTION_LABEL}>
                  Department
                </dt>
                <dd className="mt-1 text-sm text-foreground">
                  {rulingData.department}
                </dd>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Client component handles ruling text, case link, judge link, document download */}
      <RulingDetail ruling={rulingData} sanitizedRulingTextHtml={sanitizedHtml} />
    </div>
  );
}
