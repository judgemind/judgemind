import { DataQualityDashboard } from './DataQualityDashboard';

export default function DataQualityPage() {
  return (
    <div className="mx-auto max-w-6xl">
      <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">Data Quality</h1>
      <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
        Monitor scraper health, ruling ingest rates, and field completeness across California counties.
      </p>
      <div className="mt-6">
        <DataQualityDashboard />
      </div>
    </div>
  );
}
