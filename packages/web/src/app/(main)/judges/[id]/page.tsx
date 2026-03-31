import { gql } from '@apollo/client';
import { notFound } from 'next/navigation';
import Link from 'next/link';
import { createApolloClient } from '@/lib/apollo-client';
import { buildJudgeHeading, formatLabel, formatAssignmentDateRange } from '@/lib/display-helpers';
import { PAGE_TITLE } from '@/lib/typography';
import { Badge } from '@/components/ui/badge';
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
      assignments {
        department
        caseType
        courthouse
        firstSeen
        lastSeen
      }
    }
  }
`;

interface JudgeAssignment {
  department: string;
  caseType: string | null;
  courthouse: string | null;
  firstSeen: string;
  lastSeen: string;
}

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
    assignments: JudgeAssignment[];
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
  const assignments = judgeData.assignments ?? [];
  const currentAssignment = assignments[0] ?? null;
  const previousAssignments = assignments.slice(1);

  // Build the metadata line for the current assignment
  const metadataParts: string[] = [];
  if (currentAssignment) {
    metadataParts.push(`Department ${currentAssignment.department}`);
    if (currentAssignment.caseType) {
      metadataParts.push(formatLabel(currentAssignment.caseType));
    }
    if (currentAssignment.courthouse) {
      metadataParts.push(currentAssignment.courthouse);
    }
  } else if (judgeData.department) {
    metadataParts.push(`Dept. ${judgeData.department}`);
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      {/* Breadcrumb */}
      <nav className="text-sm text-muted-foreground" aria-label="Breadcrumb">
        <Link href="/judges" className="hover:text-primary">
          Judges
        </Link>
        <span className="mx-2">/</span>
        <span className="text-foreground">{judgeData.canonicalName}</span>
      </nav>

      {/* Profile header */}
      <div>
        <div className="flex flex-wrap items-start gap-3">
          <h1 className={PAGE_TITLE}>{heading}</h1>
          {!judgeData.isActive && (
            <Badge variant="secondary">Inactive</Badge>
          )}
        </div>
        {metadataParts.length > 0 && (
          <p className="mt-1 text-sm text-muted-foreground" data-testid="assignment-metadata">
            {metadataParts.join(' \u00B7 ')}
          </p>
        )}
        {judgeData.court && (
          <p className="mt-1 text-sm text-muted-foreground">
            {judgeData.court.courtName} &middot; {judgeData.court.county}
          </p>
        )}
      </div>

      {/* Previously section — only shown for judges with department changes */}
      {previousAssignments.length > 0 && (
        <section data-testid="previously-section">
          <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Previously
          </h2>
          <div className="space-y-1">
            {previousAssignments.map((assignment) => {
              const parts: string[] = [`Dept ${assignment.department}`];
              if (assignment.caseType) {
                parts.push(formatLabel(assignment.caseType));
              }
              if (assignment.courthouse) {
                parts.push(assignment.courthouse);
              }
              const dateRange = formatAssignmentDateRange(assignment.firstSeen, assignment.lastSeen);
              return (
                <p key={`${assignment.department}-${assignment.firstSeen}`} className="text-sm text-muted-foreground">
                  {parts.join(' \u00B7 ')}
                  {dateRange && (
                    <span className="ml-2 text-xs">({dateRange})</span>
                  )}
                </p>
              );
            })}
          </div>
        </section>
      )}

      <JudgeProfile judgeId={id} />
    </div>
  );
}
