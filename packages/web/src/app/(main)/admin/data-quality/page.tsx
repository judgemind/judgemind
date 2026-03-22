import { DataQualityDashboard } from './DataQualityDashboard';

export default function DataQualityPage() {
  return (
    <div className="mx-auto max-w-6xl">
      <h1 className="text-2xl font-bold text-foreground">Data Quality</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Monitor scraper health, ruling ingest rates, and field completeness across California counties.
      </p>
      <div className="mt-6">
        <DataQualityDashboard />
      </div>
    </div>
  );
}
