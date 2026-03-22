'use client';

import { useMemo } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import type { DataQualityMetricNode } from '@/lib/data-quality-queries';

interface MetricsChartProps {
  metrics: DataQualityMetricNode[];
  metricName: string;
  title: string;
  yAxisLabel: string;
  /** County to highlight; if null, shows all counties */
  county: string | null;
}

/** Distinct colors for up to 12 counties */
const COUNTY_COLORS = [
  '#2563eb', '#dc2626', '#16a34a', '#ca8a04', '#9333ea',
  '#0891b2', '#e11d48', '#65a30d', '#c2410c', '#7c3aed',
  '#0d9488', '#db2777',
];

function formatDateShort(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

export function MetricsChart({ metrics, metricName, title, yAxisLabel, county }: MetricsChartProps) {
  const { chartData, counties } = useMemo(() => {
    // Filter to requested metric
    const filtered = metrics.filter((m) => m.metricName === metricName);
    if (county) {
      const countyFiltered = filtered.filter((m) => m.county === county);
      return buildChartData(countyFiltered);
    }
    return buildChartData(filtered);
  }, [metrics, metricName, county]);

  if (chartData.length === 0) {
    return (
      <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
        <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">{title}</h3>
        <p className="mt-4 text-center text-sm text-slate-400 dark:text-slate-500">
          No data available for this time range.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
      <h3 className="mb-4 text-sm font-semibold text-slate-900 dark:text-slate-100">{title}</h3>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 12, fill: '#94a3b8' }}
            tickFormatter={formatDateShort}
          />
          <YAxis
            tick={{ fontSize: 12, fill: '#94a3b8' }}
            label={{ value: yAxisLabel, angle: -90, position: 'insideLeft', style: { fontSize: 11, fill: '#94a3b8' } }}
          />
          <Tooltip
            contentStyle={{ fontSize: 12 }}
            labelFormatter={(label: unknown) => formatDateShort(String(label))}
          />
          {counties.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}
          {counties.map((c, i) => (
            <Line
              key={c}
              type="monotone"
              dataKey={c}
              name={c}
              stroke={COUNTY_COLORS[i % COUNTY_COLORS.length]}
              strokeWidth={2}
              dot={false}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Build chart data: rows keyed by date, with one column per county */
function buildChartData(metrics: DataQualityMetricNode[]): {
  chartData: Array<Record<string, string | number | null>>;
  counties: string[];
} {
  if (metrics.length === 0) return { chartData: [], counties: [] };

  const countiesSet = new Set<string>();
  const byDate = new Map<string, Map<string, number>>();

  for (const m of metrics) {
    const dateKey = m.recordedAt.slice(0, 10); // ISO date portion
    countiesSet.add(m.county);
    if (!byDate.has(dateKey)) {
      byDate.set(dateKey, new Map());
    }
    byDate.get(dateKey)!.set(m.county, m.metricValue);
  }

  const counties = Array.from(countiesSet).sort();
  const dates = Array.from(byDate.keys()).sort();

  const chartData = dates.map((date) => {
    const row: Record<string, string | number | null> = { date };
    const vals = byDate.get(date)!;
    for (const c of counties) {
      row[c] = vals.get(c) ?? null;
    }
    return row;
  });

  return { chartData, counties };
}
