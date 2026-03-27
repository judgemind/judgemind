'use client';

import { useMemo } from 'react';
import { useQuery, gql } from '@apollo/client';
import { useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { ArrowLeft, BarChart3 } from 'lucide-react';
import { ErrorBanner } from '@/components/ErrorBanner';
import {
  formatMotionType,
  computeOverallGrantRate,
  formatDateRange,
} from '@/lib/display-helpers';
import { SECTION_LABEL } from '@/lib/typography';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

// ---------------------------------------------------------------------------
// GraphQL queries
// ---------------------------------------------------------------------------

const COMPARE_JUDGES_QUERY = gql`
  query CompareJudges($judgeIds: [ID!]!) {
    compareJudges(judgeIds: $judgeIds) {
      judgeId
      totalRulings
      rulingsByOutcome {
        outcome
        count
      }
      rulingsByMotionType {
        motionType
        total
        granted
        denied
        grantedInPart
        other
        grantRate
      }
      earliestRuling
      latestRuling
    }
  }
`;

/**
 * Build a dynamic GraphQL query that fetches basic info for N judges
 * using aliased fields. This avoids the hooks-in-a-loop problem.
 */
function buildJudgesInfoQuery(ids: string[]): ReturnType<typeof gql> {
  const params = ids.map((_, i) => `$id${i}: ID!`).join(', ');
  const fields = ids.map((_, i) =>
    `j${i}: judge(id: $id${i}) { id canonicalName department court { county courtName } }`,
  ).join('\n    ');
  return gql`
    query JudgesInfo(${params}) {
      ${fields}
    }
  `;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface MotionStats {
  motionType: string;
  total: number;
  granted: number;
  denied: number;
  grantedInPart: number;
  other: number;
  grantRate: number;
}

interface OutcomeCount {
  outcome: string;
  count: number;
}

interface JudgeAnalyticsData {
  judgeId: string;
  totalRulings: number;
  rulingsByOutcome: OutcomeCount[];
  rulingsByMotionType: MotionStats[];
  earliestRuling: string | null;
  latestRuling: string | null;
}

interface JudgeInfo {
  id: string;
  canonicalName: string;
  department: string | null;
  court: {
    county: string;
    courtName: string;
  } | null;
}

interface CompareData {
  compareJudges: JudgeAnalyticsData[];
}

// ---------------------------------------------------------------------------
// Skeleton component
// ---------------------------------------------------------------------------

function ComparisonSkeleton() {
  return (
    <div className="space-y-4" data-testid="comparison-skeleton">
      <Skeleton className="h-8 w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function JudgeComparison() {
  const searchParams = useSearchParams();
  const idsParam = searchParams.get('ids') ?? '';
  const judgeIds = useMemo(() => {
    const raw = idsParam.split(',').filter((id) => id.trim().length > 0);
    // Deduplicate while preserving order
    return [...new Set(raw)].slice(0, 10);
  }, [idsParam]);

  // Build the dynamic judge info query based on the IDs
  const judgesInfoQuery = useMemo(
    () => (judgeIds.length > 0 ? buildJudgesInfoQuery(judgeIds) : null),
    [judgeIds],
  );

  const judgesInfoVariables = useMemo(() => {
    const vars: Record<string, string> = {};
    judgeIds.forEach((id, i) => {
      vars[`id${i}`] = id;
    });
    return vars;
  }, [judgeIds]);

  // Fetch analytics for all judges in one request
  const {
    data: analyticsData,
    loading: analyticsLoading,
    error: analyticsError,
  } = useQuery<CompareData>(COMPARE_JUDGES_QUERY, {
    variables: { judgeIds },
    skip: judgeIds.length < 2,
  });

  // Fetch basic info for all judges (dynamic aliased query)
  const {
    data: infoData,
    loading: infoLoading,
    error: infoError,
  } = useQuery(judgesInfoQuery ?? COMPARE_JUDGES_QUERY, {
    variables: judgesInfoQuery ? judgesInfoVariables : { judgeIds: [] },
    skip: !judgesInfoQuery || judgeIds.length < 2,
  });

  // Parse judge info from aliased response
  const judgeInfoMap = useMemo(() => {
    const map = new Map<string, JudgeInfo>();
    if (!infoData) return map;
    for (let i = 0; i < judgeIds.length; i++) {
      const judge = infoData[`j${i}`] as JudgeInfo | null;
      if (judge) {
        map.set(judge.id, judge);
      }
    }
    return map;
  }, [infoData, judgeIds]);

  // Build analytics map keyed by judgeId
  const analyticsMap = useMemo(() => {
    const map = new Map<string, JudgeAnalyticsData>();
    if (!analyticsData?.compareJudges) return map;
    for (const a of analyticsData.compareJudges) {
      map.set(a.judgeId, a);
    }
    return map;
  }, [analyticsData]);

  // Collect all unique motion types across all judges for comparison rows
  const allMotionTypes = useMemo(() => {
    const types = new Set<string>();
    for (const analytics of analyticsMap.values()) {
      for (const m of analytics.rulingsByMotionType) {
        types.add(m.motionType);
      }
    }
    // Sort by most common first (sum of totals across judges)
    return [...types].sort((a, b) => {
      let totalA = 0;
      let totalB = 0;
      for (const analytics of analyticsMap.values()) {
        const ma = analytics.rulingsByMotionType.find((m) => m.motionType === a);
        const mb = analytics.rulingsByMotionType.find((m) => m.motionType === b);
        if (ma) totalA += ma.total;
        if (mb) totalB += mb.total;
      }
      return totalB - totalA;
    });
  }, [analyticsMap]);

  // The ordered list of judges to display as columns (preserving URL order)
  const orderedJudgeIds = judgeIds.filter(
    (id) => judgeInfoMap.has(id) || analyticsMap.has(id),
  );

  const loading = analyticsLoading || infoLoading;
  const error = analyticsError || infoError;

  // ---------------------------------------------------------------------------
  // Empty / invalid states
  // ---------------------------------------------------------------------------

  if (judgeIds.length < 2) {
    return (
      <div className="space-y-4">
        <p className="py-8 text-center text-sm text-muted-foreground">
          Select at least 2 judges to compare. Go to the{' '}
          <Link
            href="/judges"
            className="font-medium text-foreground underline underline-offset-2 hover:text-foreground/80"
          >
            judges list
          </Link>{' '}
          to select judges.
        </p>
      </div>
    );
  }

  if (loading) {
    return <ComparisonSkeleton />;
  }

  if (error) {
    return <ErrorBanner message="Failed to load judge comparison data." />;
  }

  if (orderedJudgeIds.length < 2) {
    return (
      <div className="space-y-4">
        <p className="py-8 text-center text-sm text-muted-foreground">
          Could not find enough judges to compare. Some of the selected judges may no longer exist.
        </p>
        <div className="flex justify-center">
          <Link href="/judges">
            <Button variant="outline" size="sm">
              <ArrowLeft className="mr-2 h-4 w-4" aria-hidden="true" />
              Back to judges
            </Button>
          </Link>
        </div>
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // Render comparison
  // ---------------------------------------------------------------------------

  return (
    <div className="space-y-6">
      {/* Back link */}
      <div>
        <Link href="/judges">
          <Button variant="ghost" size="sm">
            <ArrowLeft className="mr-2 h-4 w-4" aria-hidden="true" />
            Back to judges
          </Button>
        </Link>
      </div>

      {/* Overview comparison table */}
      <section>
        <h2 className={`mb-3 flex items-center gap-2 ${SECTION_LABEL}`}>
          <BarChart3 className="h-4 w-4" aria-hidden="true" />
          Overview
        </h2>
        <div className="overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="min-w-[140px]">Metric</TableHead>
                {orderedJudgeIds.map((id) => {
                  const info = judgeInfoMap.get(id);
                  return (
                    <TableHead key={id} className="min-w-[160px] text-center">
                      <Link
                        href={`/judges/${id}`}
                        className="hover:text-primary hover:underline"
                      >
                        {info?.canonicalName ?? 'Unknown'}
                      </Link>
                    </TableHead>
                  );
                })}
              </TableRow>
            </TableHeader>
            <TableBody>
              {/* County */}
              <TableRow>
                <TableCell className="font-medium">County</TableCell>
                {orderedJudgeIds.map((id) => {
                  const info = judgeInfoMap.get(id);
                  return (
                    <TableCell key={id} className="text-center">
                      {info?.court?.county ?? '\u2014'}
                    </TableCell>
                  );
                })}
              </TableRow>

              {/* Department */}
              <TableRow>
                <TableCell className="font-medium">Department</TableCell>
                {orderedJudgeIds.map((id) => {
                  const info = judgeInfoMap.get(id);
                  return (
                    <TableCell key={id} className="text-center">
                      {info?.department ?? '\u2014'}
                    </TableCell>
                  );
                })}
              </TableRow>

              {/* Overall Grant Rate */}
              <TableRow>
                <TableCell className="font-medium">Overall Grant Rate</TableCell>
                {orderedJudgeIds.map((id) => {
                  const analytics = analyticsMap.get(id);
                  const rate = analytics
                    ? computeOverallGrantRate(analytics.rulingsByOutcome)
                    : null;
                  return (
                    <TableCell key={id} className="text-center tabular-nums font-semibold">
                      {rate !== null ? `${Math.round(rate * 100)}%` : '\u2014'}
                    </TableCell>
                  );
                })}
              </TableRow>

              {/* Total Rulings */}
              <TableRow>
                <TableCell className="font-medium">Total Rulings</TableCell>
                {orderedJudgeIds.map((id) => {
                  const analytics = analyticsMap.get(id);
                  return (
                    <TableCell key={id} className="text-center tabular-nums">
                      {analytics ? analytics.totalRulings.toLocaleString() : '\u2014'}
                    </TableCell>
                  );
                })}
              </TableRow>

              {/* Date Range */}
              <TableRow>
                <TableCell className="font-medium">Date Range</TableCell>
                {orderedJudgeIds.map((id) => {
                  const analytics = analyticsMap.get(id);
                  const range = analytics
                    ? formatDateRange(analytics.earliestRuling, analytics.latestRuling)
                    : '';
                  return (
                    <TableCell key={id} className="text-center text-sm">
                      {range || '\u2014'}
                    </TableCell>
                  );
                })}
              </TableRow>
            </TableBody>
          </Table>
        </div>
      </section>

      {/* Motion type comparison table */}
      {allMotionTypes.length > 0 && (
        <section>
          <h2 className={`mb-3 flex items-center gap-2 ${SECTION_LABEL}`}>
            <BarChart3 className="h-4 w-4" aria-hidden="true" />
            Grant Rate by Motion Type
          </h2>
          <div className="overflow-x-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="min-w-[140px]">Motion Type</TableHead>
                  {orderedJudgeIds.map((id) => {
                    const info = judgeInfoMap.get(id);
                    return (
                      <TableHead key={id} className="min-w-[160px] text-center">
                        {info?.canonicalName ?? 'Unknown'}
                      </TableHead>
                    );
                  })}
                </TableRow>
              </TableHeader>
              <TableBody>
                {allMotionTypes.map((motionType) => (
                  <TableRow key={motionType}>
                    <TableCell className="font-medium">
                      {formatMotionType(motionType)}
                    </TableCell>
                    {orderedJudgeIds.map((id) => {
                      const analytics = analyticsMap.get(id);
                      const motion = analytics?.rulingsByMotionType.find(
                        (m) => m.motionType === motionType,
                      );
                      if (!motion) {
                        return (
                          <TableCell key={id} className="text-center text-muted-foreground">
                            {'\u2014'}
                          </TableCell>
                        );
                      }
                      return (
                        <TableCell key={id} className="text-center">
                          <span className="tabular-nums font-semibold">
                            {Math.round(motion.grantRate * 100)}%
                          </span>
                          <span className="ml-1 text-xs text-muted-foreground">
                            ({motion.total})
                          </span>
                        </TableCell>
                      );
                    })}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </section>
      )}
    </div>
  );
}
