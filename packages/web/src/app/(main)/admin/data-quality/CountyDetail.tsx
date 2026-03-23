'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@apollo/client';
import { MetricsChart } from './MetricsChart';
import {
  DATA_QUALITY_METRICS_QUERY,
  type DataQualityMetricsData,
  type DataQualityOverviewItem,
} from '@/lib/data-quality-queries';

interface CountyDetailProps {
  county: string;
  overview: DataQualityOverviewItem;
  onBack: () => void;
}

const STATUS_BADGE: Record<string, string> = {
  green: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  yellow: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
  red: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
};

function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

export function CountyDetail({ county, overview, onBack }: CountyDetailProps) {
  const [daysRange, setDaysRange] = useState(30);

  const dateRange = useMemo(() => {
    const end = new Date().toISOString().slice(0, 10);
    const start = daysAgo(daysRange);
    return { startDate: start, endDate: end };
  }, [daysRange]);

  // Choose resolution based on time range to keep response size manageable.
  // Single county: 8 metrics × buckets.
  //   7d  hourly   → 8 × 168 = 1,344 rows (fits in 5000 cap)
  //   14d four_hour → 8 × 84  =   672 rows
  //   30d four_hour → 8 × 180 = 1,440 rows
  //   90d daily     → 8 × 90  =   720 rows
  const resolution = useMemo(() => {
    if (daysRange <= 7) return 'hourly';
    if (daysRange <= 30) return 'four_hour';
    return 'daily';
  }, [daysRange]);

  const { data, loading, error } = useQuery<DataQualityMetricsData>(
    DATA_QUALITY_METRICS_QUERY,
    {
      variables: {
        county,
        startDate: dateRange.startDate,
        endDate: dateRange.endDate,
        resolution,
        first: 5000,
      },
    },
  );

  const metrics = useMemo(
    () => data?.dataQualityMetrics.edges.map((e) => e.node) ?? [],
    [data],
  );

  // Parse field-level completeness from metadata if available
  const fieldBreakdown = useMemo(() => {
    const completenessMetrics = metrics.filter(
      (m) => m.metricName === 'field_completeness_pct' && m.metadata,
    );
    if (completenessMetrics.length === 0) return null;
    // Use the most recent metric with metadata
    const latest = completenessMetrics[0]; // already sorted by recorded_at DESC
    try {
      return JSON.parse(latest.metadata!) as Record<string, number>;
    } catch {
      return null;
    }
  }, [metrics]);

  return (
    <div>
      {/* Header with back button */}
      <div className="mb-6 flex items-center gap-4">
        <button
          onClick={onBack}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted"
        >
          Back to overview
        </button>
        <h2 className="text-xl font-bold text-foreground">{county}</h2>
        <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${STATUS_BADGE[overview.healthStatus] ?? ''}`}>
          {overview.healthStatus.toUpperCase()}
        </span>
      </div>

      {/* Current metrics summary */}
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-sm text-muted-foreground">Rulings (24h)</p>
          <p className="mt-1 text-2xl font-bold text-foreground">
            {overview.rulingCount24h !== null ? overview.rulingCount24h : '\u2014'}
          </p>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-sm text-muted-foreground">Field Completeness</p>
          <p className="mt-1 text-2xl font-bold text-foreground">
            {overview.fieldCompletenessPct !== null ? `${Math.round(overview.fieldCompletenessPct)}%` : '\u2014'}
          </p>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-sm text-muted-foreground">Scraper Age</p>
          <p className="mt-1 text-2xl font-bold text-foreground">
            {overview.scraperLastSuccessAgeHours !== null
              ? `${Math.round(overview.scraperLastSuccessAgeHours)}h`
              : '\u2014'}
          </p>
        </div>
      </div>

      {/* Date range selector */}
      <div className="mb-4 flex gap-2">
        {[7, 14, 30, 90].map((d) => (
          <button
            key={d}
            onClick={() => setDaysRange(d)}
            className={`rounded-md px-3 py-1.5 text-sm ${
              daysRange === d
                ? 'bg-brand-accent text-white'
                : 'border border-border text-foreground hover:bg-muted'
            }`}
          >
            {d}d
          </button>
        ))}
      </div>

      {/* Loading and error states */}
      {loading && (
        <div className="space-y-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-80 animate-pulse motion-reduce:animate-none rounded-lg bg-muted" />
          ))}
        </div>
      )}

      {error && (
        <p className="text-center text-sm text-red-500 dark:text-red-400">
          Failed to load metrics. Please try again.
        </p>
      )}

      {/* Charts */}
      {!loading && !error && (
        <div className="space-y-6">
          <MetricsChart
            metrics={metrics}
            metricName="ruling_count_24h"
            title="Ruling Ingest Rate"
            yAxisLabel="Rulings"
            county={county}
          />
          <MetricsChart
            metrics={metrics}
            metricName="field_completeness_pct"
            title="Field Completeness"
            yAxisLabel="%"
            county={county}
          />
          <MetricsChart
            metrics={metrics}
            metricName="scraper_last_success_age_hours"
            title="Scraper Uptime (Hours Since Last Success)"
            yAxisLabel="Hours"
            county={county}
          />
        </div>
      )}

      {/* Field-level breakdown */}
      {fieldBreakdown && (
        <div className="mt-6 rounded-lg border border-border bg-card p-4">
          <h3 className="mb-3 text-sm font-semibold text-foreground">
            Field-Level Completeness
          </h3>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4">
            {Object.entries(fieldBreakdown)
              .sort(([, a], [, b]) => a - b)
              .map(([field, pct]) => (
                <div key={field} className="flex items-center gap-2">
                  <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                    <div
                      className={`h-full rounded-full ${
                        pct >= 90 ? 'bg-green-500' : pct >= 70 ? 'bg-yellow-500' : 'bg-red-500'
                      }`}
                      style={{ width: `${Math.min(100, pct)}%` }}
                    />
                  </div>
                  <span className="w-20 text-xs text-muted-foreground">
                    {field.replace(/_/g, ' ')}
                  </span>
                  <span className="w-10 text-right text-xs font-medium text-foreground">
                    {Math.round(pct)}%
                  </span>
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}
