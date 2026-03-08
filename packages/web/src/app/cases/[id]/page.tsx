import { gql } from '@apollo/client';
import { notFound } from 'next/navigation';
import { createApolloClient } from '@/lib/apollo-client';
import { buildCaseHeading } from '@/lib/display-helpers';
import { CaseDetail, formatLabel } from './CaseDetail';

const CASE_QUERY = gql`
  query CaseDetail($id: ID!) {
    case(id: $id) {
      id
      caseNumber
      caseTitle
      caseType
      caseStatus
      court {
        courtName
        county
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
    court: {
      courtName: string;
      county: string;
    } | null;
  } | null;
}

const STATUS_BADGE: Record<string, string> = {
  active: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  closed: 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
  dismissed: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
};

type Props = { params: Promise<{ id: string }> };

export default async function CaseDetailPage({ params }: Props) {
  const { id } = await params;

  let caseData: CaseData['case'] = null;
  try {
    const client = createApolloClient();
    const { data } = await client.query<CaseData>({
      query: CASE_QUERY,
      variables: { id },
    });
    caseData = data?.case ?? null;
  } catch {
    // GraphQL fetch failed — fall through to not found
  }

  if (!caseData) {
    notFound();
  }

  const heading = buildCaseHeading(caseData, id);

  return (
    <div className="mx-auto max-w-4xl">
      <div className="flex flex-wrap items-start gap-3">
        <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">{heading}</h1>
        <div className="flex flex-wrap gap-2 pt-1">
          {caseData.caseType && (
            <span className="inline-flex items-center rounded-full bg-blue-100 px-2.5 py-0.5 text-xs font-medium text-blue-800 dark:bg-blue-900 dark:text-blue-200">
              {formatLabel(caseData.caseType)}
            </span>
          )}
          {caseData.caseStatus && (
            <span
              className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
                STATUS_BADGE[caseData.caseStatus.toLowerCase()] ??
                'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300'
              }`}
            >
              {formatLabel(caseData.caseStatus)}
            </span>
          )}
        </div>
      </div>
      {caseData.caseTitle && (
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          {caseData.caseNumber}
        </p>
      )}
      {caseData.court && (
        <p className="mt-0.5 text-sm text-slate-500 dark:text-slate-400">
          {caseData.court.courtName} &middot; {caseData.court.county}
        </p>
      )}
      <div className="mt-6">
        <CaseDetail caseId={id} />
      </div>
    </div>
  );
}
