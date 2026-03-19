import { gql } from '@apollo/client';
import { notFound } from 'next/navigation';
import { createApolloClient } from '@/lib/apollo-client';
import { formatDate, formatLabel, formatOutcome } from '@/lib/display-helpers';
import { RulingDetail } from './RulingDetail';

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

const OUTCOME_BADGE: Record<string, string> = {
  granted: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  denied: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
  granted_in_part:
    'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
  denied_in_part:
    'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
};

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

  return (
    <div className="mx-auto max-w-4xl">
      {/* Heading */}
      <div className="flex flex-wrap items-start gap-3">
        <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">
          {motionLabel}
        </h1>
        <div className="flex flex-wrap gap-2 pt-1">
          {/* Outcome badge */}
          <span
            className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
              rulingData.outcome && OUTCOME_BADGE[rulingData.outcome]
                ? OUTCOME_BADGE[rulingData.outcome]
                : 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300'
            }`}
          >
            {formatOutcome(rulingData.outcome)}
          </span>
          {/* Tentative / Final badge */}
          <span
            className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
              rulingData.isTentative
                ? 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200'
                : 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-200'
            }`}
          >
            {rulingData.isTentative ? 'Tentative' : 'Final'}
          </span>
        </div>
      </div>

      {/* Hearing date */}
      <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
        Hearing: {formatDate(rulingData.hearingDate)}
        {rulingData.department ? ` \u00B7 Dept. ${rulingData.department}` : ''}
      </p>

      {/* Court info */}
      {rulingData.court && (
        <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
          {rulingData.court.courtName} &middot; {rulingData.court.county}
        </p>
      )}

      {/* Client component handles ruling text, case link, judge link, document download */}
      <div className="mt-6">
        <RulingDetail ruling={rulingData} />
      </div>
    </div>
  );
}
