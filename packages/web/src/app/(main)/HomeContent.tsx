'use client';

import Link from 'next/link';
import { useQuery, gql } from '@apollo/client';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  formatDate,
  formatOutcome,
  formatMotionType,
  formatJudgeName,
  getOutcomeBadgeVariant,
  getOutcomeBadgeListClass,
} from '@/lib/display-helpers';
import { PAGE_TITLE, SECTION_HEADING } from '@/lib/typography';

const PLATFORM_STATS_QUERY = gql`
  query PlatformStats {
    platformStats {
      countiesCount
      rulingsCount
      judgesCount
    }
  }
`;

const RECENT_RULINGS_QUERY = gql`
  query RecentRulings {
    rulings(first: 5) {
      edges {
        node {
          id
          hearingDate
          outcome
          motionType
          department
          case {
            id
            caseNumber
            caseTitle
            court {
              county
              courtName
            }
          }
          judge {
            canonicalName
          }
        }
      }
    }
  }
`;

interface StatsData {
  platformStats: {
    countiesCount: number;
    rulingsCount: number;
    judgesCount: number;
  };
}

interface RulingNode {
  id: string;
  hearingDate: string;
  outcome: string | null;
  motionType: string | null;
  department: string | null;
  case: {
    id: string;
    caseNumber: string;
    caseTitle: string | null;
    court: {
      county: string;
      courtName: string;
    };
  } | null;
  judge: {
    canonicalName: string;
  } | null;
}

interface RecentRulingsData {
  rulings: {
    edges: Array<{ node: RulingNode }>;
  };
}

function formatStatNumber(n: number): string {
  if (n >= 1000) {
    return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
  }
  return n.toLocaleString();
}

function StatsBar() {
  const { data, loading } = useQuery<StatsData>(PLATFORM_STATS_QUERY);

  return (
    <div className="grid grid-cols-3 gap-4" data-testid="stats-bar">
      {loading || !data ? (
        <>
          <Card>
            <CardContent className="p-4 text-center">
              <Skeleton className="mx-auto mb-1 h-7 w-12" />
              <Skeleton className="mx-auto h-4 w-20" />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <Skeleton className="mx-auto mb-1 h-7 w-12" />
              <Skeleton className="mx-auto h-4 w-20" />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <Skeleton className="mx-auto mb-1 h-7 w-12" />
              <Skeleton className="mx-auto h-4 w-20" />
            </CardContent>
          </Card>
        </>
      ) : (
        <>
          <Card>
            <CardContent className="p-4 text-center">
              <p className="text-2xl font-bold text-foreground">
                {formatStatNumber(data.platformStats.countiesCount)}
              </p>
              <p className="text-sm text-muted-foreground">Counties covered</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <p className="text-2xl font-bold text-foreground">
                {formatStatNumber(data.platformStats.rulingsCount)}
              </p>
              <p className="text-sm text-muted-foreground">Rulings captured</p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4 text-center">
              <p className="text-2xl font-bold text-foreground">
                {formatStatNumber(data.platformStats.judgesCount)}
              </p>
              <p className="text-sm text-muted-foreground">Judges tracked</p>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

function RecentRulings() {
  const { data, loading, error } = useQuery<RecentRulingsData>(RECENT_RULINGS_QUERY);

  const edges = data?.rulings.edges ?? [];

  return (
    <section data-testid="recent-rulings">
      <div className="mb-3 flex items-center justify-between">
        <h2 className={SECTION_HEADING}>Recent rulings</h2>
        <Link
          href="/rulings"
          className="text-sm font-medium text-primary hover:underline"
        >
          View all
        </Link>
      </div>

      {loading && edges.length === 0 && (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Card key={i}>
              <CardContent className="p-4">
                <div className="space-y-2">
                  <Skeleton className="h-4 w-2/3" />
                  <Skeleton className="h-3 w-1/2" />
                  <div className="flex gap-2 pt-1">
                    <Skeleton className="h-5 w-16 rounded-full" />
                    <Skeleton className="h-5 w-20 rounded-full" />
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {error && (
        <p className="p-4 text-center text-sm text-muted-foreground">
          Could not load recent rulings.
        </p>
      )}

      {!loading && !error && edges.length === 0 && (
        <p className="p-4 text-center text-sm text-muted-foreground">
          No rulings yet. Check back after scrapers have run.
        </p>
      )}

      {edges.length > 0 && (
        <div className="space-y-3">
          {edges.map(({ node }) => (
            <Card key={node.id} className="transition-colors hover:bg-accent/50">
              <CardContent className="p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    {node.case ? (
                      <Link
                        href={`/rulings/${node.id}`}
                        className="block truncate rounded-sm font-medium text-foreground hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        {node.case.caseNumber}
                        {node.case.caseTitle ? ` \u2014 ${node.case.caseTitle}` : ''}
                      </Link>
                    ) : (
                      <span className="text-muted-foreground">{'\u2014'}</span>
                    )}

                    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                      {node.case?.court && (
                        <span>{node.case.court.county} {node.department ? `\u00B7 Dept. ${node.department}` : ''}</span>
                      )}
                      <span>{formatJudgeName(node.judge)}</span>
                    </div>

                    <div className="mt-2 flex flex-wrap gap-2">
                      <Badge
                        variant={getOutcomeBadgeVariant(node.outcome)}
                        className={getOutcomeBadgeListClass(node.outcome)}
                      >
                        {formatOutcome(node.outcome)}
                      </Badge>
                      <Badge variant="outline" className="text-muted-foreground">
                        {formatMotionType(node.motionType)}
                      </Badge>
                    </div>
                  </div>

                  <span className="shrink-0 text-xs text-muted-foreground">
                    {formatDate(node.hearingDate)}
                  </span>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </section>
  );
}

const HOW_IT_WORKS_STEPS = [
  {
    step: '1',
    title: 'We capture tentative rulings daily',
    description:
      'Automated scrapers collect tentative rulings from California Superior Courts before they expire.',
  },
  {
    step: '2',
    title: 'Search by keyword, judge, or case',
    description:
      'Full-text search across all captured rulings. Filter by county, judge, date, motion type, or outcome.',
  },
  {
    step: '3',
    title: 'Track judge analytics over time',
    description:
      'See grant/deny rates by motion type for any judge. Understand tendencies before your next hearing.',
  },
] as const;

function HowItWorks() {
  return (
    <section data-testid="how-it-works">
      <h2 className={`mb-3 ${SECTION_HEADING}`}>How it works</h2>
      <div className="grid gap-4 sm:grid-cols-3">
        {HOW_IT_WORKS_STEPS.map((item) => (
          <Card key={item.step}>
            <CardContent className="p-4">
              <p className="mb-1 text-sm font-semibold text-primary">{`Step ${item.step}`}</p>
              <p className="font-medium text-foreground">{item.title}</p>
              <p className="mt-1 text-sm text-muted-foreground">{item.description}</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}

export function HomeContent() {
  return (
    <div className="mx-auto max-w-3xl space-y-8">
      <div>
        <h1 className={PAGE_TITLE}>
          Free legal research for everyone
        </h1>
        <p className="mt-4 text-lg text-muted-foreground">
          Judgemind captures California tentative rulings and judicial analytics — open source,
          free forever.
        </p>
      </div>

      <div className="flex gap-3">
        <Button asChild>
          <Link href="/search">Search rulings</Link>
        </Button>
        <Button variant="outline" asChild>
          <Link href="/rulings">Latest rulings</Link>
        </Button>
      </div>

      <StatsBar />

      <RecentRulings />

      <HowItWorks />
    </div>
  );
}
