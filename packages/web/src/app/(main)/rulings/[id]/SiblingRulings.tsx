'use client';

import { useQuery, gql } from '@apollo/client';
import Link from 'next/link';
import { formatDate, formatLabel } from '@/lib/display-helpers';
import { OutcomeBadge } from '@/components/OutcomeBadge';
import { SECTION_HEADING } from '@/lib/typography';
import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

const SIBLING_RULINGS_QUERY = gql`
  query SiblingRulings($caseId: ID!, $first: Int!) {
    rulings(caseId: $caseId, first: $first) {
      edges {
        node {
          id
          hearingDate
          motionType
          outcome
        }
      }
    }
  }
`;

interface SiblingRulingNode {
  id: string;
  hearingDate: string;
  motionType: string | null;
  outcome: string | null;
}

interface SiblingRulingsData {
  rulings: {
    edges: Array<{ node: SiblingRulingNode }>;
  };
}

interface SiblingRulingsProps {
  caseId: string;
  currentRulingId: string;
}

export function SiblingRulings({ caseId, currentRulingId }: SiblingRulingsProps) {
  const { data, loading, error } = useQuery<SiblingRulingsData>(SIBLING_RULINGS_QUERY, {
    variables: { caseId, first: 100 },
  });

  if (loading) {
    return (
      <section data-testid="sibling-rulings-loading">
        <h2 className={`mb-3 ${SECTION_HEADING}`}>Other Rulings on This Case</h2>
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      </section>
    );
  }

  if (error) {
    return null;
  }

  const siblings = (data?.rulings.edges ?? [])
    .map((edge) => edge.node)
    .filter((node) => node.id !== currentRulingId);

  if (siblings.length === 0) {
    return null;
  }

  return (
    <section data-testid="sibling-rulings-section">
      <h2 className={`mb-3 ${SECTION_HEADING}`}>Other Rulings on This Case</h2>
      <div className="space-y-2">
        {siblings.map((ruling) => (
          <Link
            key={ruling.id}
            href={`/rulings/${ruling.id}`}
            className="block"
          >
            <Card className="transition-colors hover:bg-muted/50">
              <CardContent className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
                <span className="text-sm font-medium text-foreground">
                  {formatDate(ruling.hearingDate)}
                </span>
                <span className="flex-1 text-sm text-muted-foreground">
                  {ruling.motionType ? formatLabel(ruling.motionType) : '\u2014'}
                </span>
                <OutcomeBadge outcome={ruling.outcome} />
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
    </section>
  );
}
