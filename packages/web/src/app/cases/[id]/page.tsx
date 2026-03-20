import { gql } from '@apollo/client';
import { notFound } from 'next/navigation';
import Link from 'next/link';
import { createApolloClient } from '@/lib/apollo-client';
import { buildCaseHeading, formatLabel } from '@/lib/display-helpers';
import { Badge } from '@/components/ui/badge';
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

const STATUS_VARIANT: Record<string, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  active: 'default',
  closed: 'secondary',
  dismissed: 'destructive',
};

type Props = { params: { id: string } };

export default async function CaseDetailPage({ params }: Props) {
  const { id } = params;

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
      <nav className="mb-4 text-sm text-muted-foreground">
        <Link href="/cases" className="hover:text-primary">
          Cases
        </Link>
        <span className="mx-2">/</span>
        <span className="text-foreground">{caseData.caseNumber}</span>
      </nav>

      <div className="flex flex-wrap items-start gap-3">
        <h1 className="text-2xl font-bold text-foreground">{heading}</h1>
        <div className="flex flex-wrap gap-2 pt-1">
          {caseData.caseType && (
            <Badge variant="outline">
              {formatLabel(caseData.caseType)}
            </Badge>
          )}
          {caseData.caseStatus && (
            <Badge
              variant={
                STATUS_VARIANT[caseData.caseStatus.toLowerCase()] ?? 'outline'
              }
            >
              {formatLabel(caseData.caseStatus)}
            </Badge>
          )}
        </div>
      </div>
      {caseData.caseTitle && (
        <p className="mt-1 text-base text-muted-foreground">
          {caseData.caseTitle}
        </p>
      )}
      {caseData.court && (
        <p className="mt-0.5 text-sm text-muted-foreground">
          {caseData.court.courtName} &middot; {caseData.court.county}
        </p>
      )}
      <div className="mt-6">
        <CaseDetail caseId={id} />
      </div>
    </div>
  );
}
