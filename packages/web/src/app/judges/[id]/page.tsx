import { gql } from '@apollo/client';
import { createApolloClient } from '@/lib/apollo-client';
import { buildJudgeHeading } from '@/lib/display-helpers';

const JUDGE_QUERY = gql`
  query JudgeDetail($id: ID!) {
    judge(id: $id) {
      id
      canonicalName
      department
      isActive
      court {
        courtName
        county
      }
    }
  }
`;

interface JudgeData {
  judge: {
    id: string;
    canonicalName: string;
    department: string | null;
    isActive: boolean;
    court: {
      courtName: string;
      county: string;
    } | null;
  } | null;
}

type Props = { params: Promise<{ id: string }> };

export default async function JudgeDetailPage({ params }: Props) {
  const { id } = await params;

  let judgeData: JudgeData['judge'] = null;
  try {
    const client = createApolloClient();
    const { data } = await client.query<JudgeData>({
      query: JUDGE_QUERY,
      variables: { id },
    });
    judgeData = data?.judge ?? null;
  } catch {
    // GraphQL fetch failed — fall through to fallback display
  }

  const heading = buildJudgeHeading(judgeData, id);

  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">{heading}</h1>
      {judgeData?.court && (
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          {judgeData.court.courtName} &middot; {judgeData.court.county}
          {judgeData.department ? ` \u00B7 Dept. ${judgeData.department}` : ''}
        </p>
      )}
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
        Judge analytics coming soon.
      </p>
    </div>
  );
}
