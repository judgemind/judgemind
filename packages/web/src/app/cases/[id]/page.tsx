import { gql } from '@apollo/client';
import { createApolloClient } from '@/lib/apollo-client';
import { buildCaseHeading } from '@/lib/display-helpers';
import { CaseDetail } from './CaseDetail';

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
    // GraphQL fetch failed — fall through to fallback display
  }

  const heading = buildCaseHeading(caseData, id);

  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">{heading}</h1>
      {caseData?.court && (
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          {caseData.court.courtName} &middot; {caseData.court.county}
        </p>
      )}
      <div className="mt-6">
        <CaseDetail caseId={id} />
      </div>
    </div>
  );
}
