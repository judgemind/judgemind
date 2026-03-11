import { gql } from '@apollo/client';
import { notFound } from 'next/navigation';
import { createApolloClient } from '@/lib/apollo-client';
import { buildJudgeHeading } from '@/lib/display-helpers';
import { JudgeProfile } from './JudgeProfile';

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

type Props = { params: { id: string } };

export default async function JudgeDetailPage({ params }: Props) {
  const { id } = params;

  let judgeData: JudgeData['judge'] = null;
  try {
    const client = createApolloClient();
    const { data } = await client.query<JudgeData>({
      query: JUDGE_QUERY,
      variables: { id },
    });
    judgeData = data?.judge ?? null;
  } catch {
    // GraphQL fetch failed — fall through to not found
  }

  if (!judgeData) {
    notFound();
  }

  const heading = buildJudgeHeading(judgeData, id);

  return (
    <div className="mx-auto max-w-4xl">
      <div className="flex flex-wrap items-start gap-3">
        <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">{heading}</h1>
        <span
          className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
            judgeData.isActive
              ? 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
              : 'bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-300'
          }`}
        >
          {judgeData.isActive ? 'Active' : 'Inactive'}
        </span>
      </div>
      {judgeData.court && (
        <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          {judgeData.court.courtName} &middot; {judgeData.court.county}
          {judgeData.department ? ` \u00B7 Dept. ${judgeData.department}` : ''}
        </p>
      )}
      <div className="mt-6">
        <JudgeProfile judgeId={id} />
      </div>
    </div>
  );
}
