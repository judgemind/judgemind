'use client';

import type { DataQualityOverviewItem } from '@/lib/data-quality-queries';

const STATUS_COLORS: Record<string, string> = {
  green: 'bg-green-100 border-green-300 text-green-800 dark:bg-green-900/30 dark:border-green-700 dark:text-green-300',
  yellow: 'bg-yellow-100 border-yellow-300 text-yellow-800 dark:bg-yellow-900/30 dark:border-yellow-700 dark:text-yellow-300',
  red: 'bg-red-100 border-red-300 text-red-800 dark:bg-red-900/30 dark:border-red-700 dark:text-red-300',
};

const STATUS_DOT: Record<string, string> = {
  green: 'bg-green-500',
  yellow: 'bg-yellow-500',
  red: 'bg-red-500',
};

interface OverviewGridProps {
  items: DataQualityOverviewItem[];
  onCountyClick: (county: string) => void;
  selectedCounty: string | null;
}

export function OverviewGrid({ items, onCountyClick, selectedCounty }: OverviewGridProps) {
  const greenCount = items.filter((i) => i.healthStatus === 'green').length;
  const redCount = items.filter((i) => i.healthStatus === 'red').length;
  const healthScore = items.length > 0 ? Math.round((greenCount / items.length) * 100) : 0;

  return (
    <div>
      {/* Summary stats */}
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
          <p className="text-sm text-slate-500 dark:text-slate-400">System Health Score</p>
          <p className="mt-1 text-3xl font-bold text-slate-900 dark:text-slate-100">{healthScore}%</p>
          <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
            {greenCount} of {items.length} counties healthy
          </p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
          <p className="text-sm text-slate-500 dark:text-slate-400">Active Alerts</p>
          <p className={`mt-1 text-3xl font-bold ${redCount > 0 ? 'text-red-600 dark:text-red-400' : 'text-slate-900 dark:text-slate-100'}`}>
            {redCount}
          </p>
          <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
            counties in red status
          </p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
          <p className="text-sm text-slate-500 dark:text-slate-400">Total Counties</p>
          <p className="mt-1 text-3xl font-bold text-slate-900 dark:text-slate-100">{items.length}</p>
          <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">monitored</p>
        </div>
      </div>

      {/* Traffic light grid */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
        {items.map((item) => (
          <button
            key={item.county}
            onClick={() => onCountyClick(item.county)}
            className={`rounded-lg border p-3 text-left transition-shadow hover:shadow-md ${
              STATUS_COLORS[item.healthStatus] ?? STATUS_COLORS.red
            } ${selectedCounty === item.county ? 'ring-2 ring-brand-accent-lighter ring-offset-1 dark:ring-offset-stone-900' : ''}`}
          >
            <div className="flex items-center gap-2">
              <span className={`inline-block h-2.5 w-2.5 rounded-full ${STATUS_DOT[item.healthStatus] ?? STATUS_DOT.red}`} />
              <span className="text-sm font-semibold">{item.county}</span>
            </div>
            <div className="mt-2 space-y-0.5 text-xs opacity-80">
              {item.rulingCount24h !== null && (
                <p>{item.rulingCount24h} rulings/24h</p>
              )}
              {item.fieldCompletenessPct !== null && (
                <p>{Math.round(item.fieldCompletenessPct)}% complete</p>
              )}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
