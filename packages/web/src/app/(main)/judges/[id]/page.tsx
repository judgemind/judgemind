import { gql } from '@apollo/client';
import { notFound } from 'next/navigation';
import { createApolloClient } from '@/lib/apollo-client';
import { buildJudgeHeading } from '@/lib/display-helpers';
import { PAGE_TITLE } from '@/lib/typography';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
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
    <div className="mx-auto max-w-4xl space-y-6">
      {/* Profile header card */}
      <Card>
        <CardContent className="p-6">
          <div className="flex flex-wrap items-start gap-3">
            <h1 className={PAGE_TITLE}>{heading}</h1>
            {!judgeData.isActive && (
              <Badge variant="secondary">Inactive</Badge>
            )}
          </div>
          {judgeData.court && (
            <p className="mt-2 text-sm text-muted-foreground">
              {judgeData.court.courtName} &middot; {judgeData.court.county}
              {judgeData.department ? ` \u00B7 Dept. ${judgeData.department}` : ''}
            </p>
          )}
        </CardContent>
      </Card>

      <JudgeProfile judgeId={id} />
    </div>
  );
}
