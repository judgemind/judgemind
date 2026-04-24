'use client';

import { useQuery } from '@apollo/client';
import { SECTION_HEADING } from '@/lib/typography';
import {
  WEEKLY_DIAGNOSER_REPORT_QUERY,
  type WeeklyDiagnoserReportData,
} from '@/lib/dispatcher-queries';

/**
 * Diagnoser effectiveness panel (issue #2800, spec §8).
 *
 * Renders a 7-day rollup table of diagnoser recommendations vs. observed
 * outcomes, grouped by (recommendedAction × observedOutcome × calendar day).
 * Data is sourced from the `weeklyDiagnoserReport` GraphQL query which
 * aggregates `dispatcher.diagnoses` rows with resolved outcomes.
 *
 * No polling — the 7-day aggregate is stable; mount-time fetch is sufficient.
 * Operators can hard-refresh if they need the latest data.
 */
export function DiagnoserEffectivenessPanel() {
  const { data, loading } = useQuery<WeeklyDiagnoserReportData>(WEEKLY_DIAGNOSER_REPORT_QUERY);
  const rows = data?.weeklyDiagnoserReport ?? [];

  return (
    <section aria-labelledby="diagnoser-effectiveness-heading">
      <div className="flex items-center justify-between border-b border-border pb-2 mb-2">
        <h2 id="diagnoser-effectiveness-heading" className={SECTION_HEADING}>
          Diagnoser effectiveness (last 7 days)
        </h2>
      </div>
      {loading && rows.length === 0 ? (
        <div className="space-y-2">
          {[1, 2, 3].map((n) => (
            <div
              key={n}
              className="h-6 animate-pulse rounded bg-muted motion-reduce:animate-none"
              data-testid="diagnoser-row-skeleton"
            />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <p className="py-2 text-sm text-muted-foreground">
          No diagnoses with resolved outcomes yet.
        </p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="py-2 font-medium">Day</th>
              <th scope="col" className="py-2 font-medium">Recommended action</th>
              <th scope="col" className="py-2 font-medium">Observed outcome</th>
              <th scope="col" className="py-2 font-medium text-right">Count</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => (
              <tr
                key={idx}
                className="border-t border-border hover:bg-muted/50"
                data-testid="diagnoser-effectiveness-row"
              >
                <td className="py-2 font-mono text-xs text-muted-foreground">
                  {row.day.slice(0, 10)}
                </td>
                <td className="py-2">
                  <span className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                    {row.recommendedAction}
                  </span>
                </td>
                <td className="py-2">
                  <span className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                    {row.observedOutcome}
                  </span>
                </td>
                <td className="py-2 font-mono text-right text-foreground">{row.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
