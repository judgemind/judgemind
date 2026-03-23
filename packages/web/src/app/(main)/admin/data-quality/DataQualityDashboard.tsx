'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@apollo/client';
import { useAuth } from '@/providers/AuthProvider';
import { OverviewGrid } from './OverviewGrid';
import { MetricsChart } from './MetricsChart';
import { CountyDetail } from './CountyDetail';
import {
  DATA_QUALITY_OVERVIEW_QUERY,
  DATA_QUALITY_METRICS_QUERY,
  type DataQualityOverviewData,
  type DataQualityMetricsData,
} from '@/lib/data-quality-queries';
import { SECTION_HEADING } from '@/lib/typography';

function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

function SkeletonGrid() {
  return (
    <div>
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="h-24 animate-pulse motion-reduce:animate-none rounded-lg bg-muted" />
        ))}
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
        {Array.from({ length: 10 }).map((_, i) => (
          <div key={i} className="h-20 animate-pulse motion-reduce:animate-none rounded-lg bg-muted" />
        ))}
      </div>
    </div>
  );
}

export function DataQualityDashboard() {
  const { user, loading: authLoading } = useAuth();
  const [selectedCounty, setSelectedCounty] = useState<string | null>(null);

  // Determine date range for overview charts (7 days)
  const overviewDateRange = useMemo(() => {
    const end = new Date().toISOString().slice(0, 10);
    const start = daysAgo(7);
    return { startDate: start, endDate: end };
  }, []);

  const { data: overviewData, loading: overviewLoading, error: overviewError } = useQuery<DataQualityOverviewData>(
    DATA_QUALITY_OVERVIEW_QUERY,
    { skip: !user || user.role !== 'admin' },
  );

  const { data: metricsData, loading: metricsLoading, error: metricsError } = useQuery<DataQualityMetricsData>(
    DATA_QUALITY_METRICS_QUERY,
    {
      variables: {
        startDate: overviewDateRange.startDate,
        endDate: overviewDateRange.endDate,
        // 7 days × 24 hourly snapshots × 8 counties × 8 metrics ≈ 10,752 rows.
        // Use 2000 to get enough data for meaningful charts while staying
        // within the API's page-size cap.
        first: 2000,
      },
      skip: !user || user.role !== 'admin',
    },
  );

  const metrics = useMemo(
    () => metricsData?.dataQualityMetrics.edges.map((e) => e.node) ?? [],
    [metricsData],
  );

  // Auth loading
  if (authLoading) {
    return <SkeletonGrid />;
  }

  // Not authenticated or not admin
  if (!user || user.role !== 'admin') {
    return (
      <div className="rounded-lg border border-border p-8 text-center">
        <h2 className={SECTION_HEADING}>Access Denied</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          This page is restricted to admin users. Please log in with an admin account.
        </p>
      </div>
    );
  }

  // County drill-down view
  if (selectedCounty && overviewData) {
    const countyOverview = overviewData.dataQualityOverview.find(
      (item) => item.county === selectedCounty,
    );
    if (countyOverview) {
      return (
        <CountyDetail
          county={selectedCounty}
          overview={countyOverview}
          onBack={() => setSelectedCounty(null)}
        />
      );
    }
  }

  // Overview loading
  if (overviewLoading) {
    return <SkeletonGrid />;
  }

  // Error state
  if (overviewError || metricsError) {
    return (
      <p className="text-center text-sm text-red-500 dark:text-red-400">
        Failed to load data quality metrics. Please try again.
      </p>
    );
  }

  const overviewItems = overviewData?.dataQualityOverview ?? [];

  return (
    <div className="space-y-8">
      {/* Overview grid */}
      <OverviewGrid
        items={overviewItems}
        onCountyClick={setSelectedCounty}
        selectedCounty={selectedCounty}
      />

      {/* Time series charts */}
      {metricsLoading ? (
        <div className="space-y-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-80 animate-pulse motion-reduce:animate-none rounded-lg bg-muted" />
          ))}
        </div>
      ) : (
        <div className="space-y-6">
          <MetricsChart
            metrics={metrics}
            metricName="ruling_count_24h"
            title="Ruling Ingest Rate (7 days)"
            yAxisLabel="Rulings"
            county={null}
          />
          <MetricsChart
            metrics={metrics}
            metricName="field_completeness_pct"
            title="Field Completeness Trends (7 days)"
            yAxisLabel="%"
            county={null}
          />
          <MetricsChart
            metrics={metrics}
            metricName="scraper_last_success_age_hours"
            title="Scraper Uptime (7 days)"
            yAxisLabel="Hours"
            county={null}
          />
        </div>
      )}
    </div>
  );
}
