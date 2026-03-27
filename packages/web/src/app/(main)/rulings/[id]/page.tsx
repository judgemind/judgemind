import { gql } from '@apollo/client';
import { notFound } from 'next/navigation';
import Link from 'next/link';
import { createApolloClient } from '@/lib/apollo-client';
import { formatDate, formatLabel, formatOutcome, getOutcomeBadgeClass, groupParties } from '@/lib/display-helpers';
import { PAGE_TITLE, SECTION_LABEL } from '@/lib/typography';
import { sanitizeRulingHtml } from '@/lib/sanitize-html';
import { RulingDetail } from './RulingDetail';
import { SiblingRulings } from './SiblingRulings';
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
        caseType
        parties {
          id
          canonicalName
          partyType
          role
        }
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
      caseType: string | null;
      parties: Array<{
        id: string;
        canonicalName: string;
        partyType: string | null;
        role: string | null;
      }>;
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


/** Compact party list for a single role group (plaintiffs, defendants, etc.). */
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

/** Parties header for the ruling detail page, matching the CaseDetail pattern. */
function PartiesSection({
  parties,
}: {
  parties: Array<{ id: string; canonicalName: string; partyType: string | null; role: string | null }>;
}) {
  const { plaintiffs, defendants, others } = groupParties(parties);
  const hasParties = plaintiffs.length > 0 || defendants.length > 0 || others.length > 0;

  if (!hasParties) return null;

  return (
    <div className="flex flex-wrap gap-x-8 gap-y-4" data-testid="parties-section">
      <PartyList label="Plaintiffs" parties={plaintiffs} />
      <PartyList label="Defendants" parties={defendants} />
      {others.length > 0 && (
        <PartyList label="Other Parties" parties={others} />
      )}
    </div>
  );
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

        {/* Subtitle line 1: Judge · Court (combined with county) · Dept. */}
        {(rulingData.judge || rulingData.court || rulingData.department) && (
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
            {rulingData.department && (
              <>
                <span aria-hidden="true"> &middot; </span>
                <span>Dept. {rulingData.department}</span>
              </>
            )}
          </p>
        )}

        {/* Subtitle line 2: Case number · Case type · Hearing date */}
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
            {rulingData.case?.caseType && (
              <>
                <span aria-hidden="true"> &middot; </span>
                <Badge variant="outline" className="text-xs" data-testid="case-type-badge">
                  {formatLabel(rulingData.case.caseType)}
                </Badge>
              </>
            )}
            {rulingData.case && rulingData.hearingDate && (
              <span aria-hidden="true"> &middot; </span>
            )}
            {rulingData.hearingDate && formatDate(rulingData.hearingDate)}
          </p>
        )}
      </div>

      {/* Parties section — context before content per web-patterns.md */}
      {rulingData.case && rulingData.case.parties.length > 0 && (
        <PartiesSection parties={rulingData.case.parties} />
      )}

      {/* Client component handles ruling text, case link, judge link, document download */}
      <RulingDetail ruling={rulingData} sanitizedRulingTextHtml={sanitizedHtml} />

      {/* Sibling rulings on the same case */}
      {rulingData.case && (
        <SiblingRulings caseId={rulingData.case.id} currentRulingId={rulingData.id} />
      )}
    </div>
  );
}
