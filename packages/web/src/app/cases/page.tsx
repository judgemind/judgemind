import { Suspense } from 'react';
import { CasesList } from './CasesList';

export default function CasesPage() {
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-foreground">Cases</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Browse court cases across California.
        </p>
      </div>
      <div>
        <Suspense>
          <CasesList />
        </Suspense>
      </div>
    </div>
  );
}
